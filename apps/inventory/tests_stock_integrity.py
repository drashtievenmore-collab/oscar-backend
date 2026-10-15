"""Stock integrity (Phase 1.4): lot/taka quantities, QC Hold, condition routing.

Run from ``oscar-backend`` with a private test database::

    DB_TEST_NAME=test_oscar_inv python manage.py test apps.inventory --noinput
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounting import services as ledger
from apps.accounts.models import Client, User
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.masters.models import Item, Location, Party
from apps.purchase import services as purchase_services
from apps.purchase.models import GoodsReceipt, PurchaseBill, PurchaseBillLine

from apps.inventory import qc as qc_service
from apps.inventory import services as stock
from apps.inventory.models import LotTaka, StockMovement
from apps.inventory.views import InventoryDashboardView, StockTransferViewSet

D = Decimal


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


class StockBase(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(slug=uid("inv"), name="Inventory Test Tenant")
        self.user = User.objects.create(
            client=self.client_obj, name="Stock Keeper", email=f"{uid('u')}@t.co",
            status="Active", is_superuser=True,
        )
        ledger.seed_chart_of_accounts(self.client_obj)
        self.godown = Location.objects.create(client=self.client_obj, code="GD-1", name="Godown 1")
        self.godown2 = Location.objects.create(client=self.client_obj, code="GD-2", name="Godown 2")
        self.vendor = Party.objects.create(
            client=self.client_obj, code=uid("V"), type="Vendor", name="Grey Supplier"
        )
        self.customer = Party.objects.create(
            client=self.client_obj, code=uid("C"), type="Customer", name="Buyer"
        )
        self.fabric = Item.objects.create(
            client=self.client_obj, sku=uid("FAB"), name="Grey Cotton 58in", uom="Mtr",
            item_kind="Standalone", tracking_type="TAKA", cost_price=D("50"),
            fabric_quality="Cotton 60x60",
        )
        self.plain = Item.objects.create(
            client=self.client_obj, sku=uid("TRM"), name="Packing Tape", uom="Nos",
            item_kind="Standalone", cost_price=D("10"),
        )
        self.factory = APIRequestFactory()

    # -- helpers -------------------------------------------------------------
    def on_hand(self, item, location):
        row = stock.balances_for(self.client_obj.id, [item.id], location.id).get(item.id, {})
        return D(row.get("on_hand") or 0)

    def make_lot(self, qty, location=None, taka="1"):
        location = location or self.godown
        lot = LotTaka.objects.create(
            client=self.client_obj, taka_id=uid("TAK"), lot_number=uid("LOT"),
            taka_number=taka, item=self.fabric, location=location,
            received_quantity=D(qty), available_quantity=D(0), uom="Mtr",
            qc_status="Passed", rate=D("50"),
        )
        stock.post_movement(
            client_id=self.client_obj.id, item=self.fabric, location=location,
            type="ADJUSTMENT", quantity=D(qty), lot_taka=lot, notes="opening",
            user=self.user,
        )
        lot.refresh_from_db()
        return lot

    def make_bill(self):
        bill = PurchaseBill.objects.create(
            client=self.client_obj, party=self.vendor, doc_date=date.today(),
            location=self.godown, status="Draft",
        )
        PurchaseBillLine.objects.create(
            client=self.client_obj, purchase_bill=bill, line_no=1, item=self.fabric,
            sku=self.fabric.sku, item_name=self.fabric.name, uom="Mtr",
            qty=D("100"), rate=D("50"), amount=D("5000"), tax_pct=D("0"), line_total=D("5000"),
        )
        PurchaseBillLine.objects.create(
            client=self.client_obj, purchase_bill=bill, line_no=2, item=self.plain,
            sku=self.plain.sku, item_name=self.plain.name, uom="Nos",
            qty=D("20"), rate=D("10"), amount=D("200"), tax_pct=D("0"), line_total=D("200"),
        )
        return bill

    def receive(self, bill, qc_status=None):
        line = bill.line_items.get(line_no=1)
        return purchase_services.receive_bill_goods(
            bill,
            lines_payload=[{
                "lineId": str(line.id),
                "takas": [{"takaRoll": "1", "qty": "60"}, {"takaRoll": "2", "qty": "40"}],
            }],
            qc_status=qc_status,
            user=self.user,
        )

    def call(self, viewset, method, url, data=None, actions=None, **kwargs):
        request = getattr(self.factory, method)(url, data or {}, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        return viewset.as_view(actions)(request, **kwargs)


class LotQuantityTests(StockBase):
    def test_movement_updates_lot_available_quantity(self):
        lot = self.make_lot("100")
        self.assertEqual(lot.available_quantity, D("100"))
        stock.post_movement(
            client_id=self.client_obj.id, item=self.fabric, location=self.godown,
            type="ADJUSTMENT", quantity=D("-30"), lot_taka=lot, notes="cut", user=self.user,
        )
        lot.refresh_from_db()
        self.assertEqual(lot.available_quantity, D("70"))
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("70"))

    def test_negative_lot_quantity_raises_instead_of_clamping(self):
        lot = self.make_lot("10")
        with self.assertRaises(BusinessRuleViolation):
            stock.post_movement(
                client_id=self.client_obj.id, item=self.fabric, location=self.godown,
                type="ADJUSTMENT", quantity=D("-11"), lot_taka=lot, notes="too much",
                user=self.user,
            )
        lot.refresh_from_db()
        self.assertEqual(lot.available_quantity, D("10"))

    def test_taka_tracked_item_requires_lot(self):
        with self.assertRaises(ValidationFailed):
            stock.post_movement(
                client_id=self.client_obj.id, item=self.fabric, location=self.godown,
                type="ADJUSTMENT", quantity=D("5"), notes="no lot", user=self.user,
            )

    def test_outward_from_wrong_location_raises(self):
        lot = self.make_lot("10")
        with self.assertRaises(ValidationFailed):
            stock.post_movement(
                client_id=self.client_obj.id, item=self.fabric, location=self.godown2,
                type="ADJUSTMENT", quantity=D("-1"), lot_taka=lot, notes="x", user=self.user,
            )


class PurchaseQcTests(StockBase):
    def test_receipt_goes_to_qc_hold_and_is_not_available(self):
        result = self.receive(self.make_bill())
        qc_hold = stock.system_location(self.client_obj.id, "QC_HOLD")
        self.assertEqual(result["receipt"].qc_status, "Pending")
        self.assertEqual(self.on_hand(self.fabric, qc_hold), D("100"))
        self.assertEqual(self.on_hand(self.plain, qc_hold), D("20"))
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("0"))
        # Physically on hand but blocked at QC Hold.
        self.assertEqual(
            stock.calculate_item_stock(self.client_obj.id, self.fabric)["available"], D("0")
        )
        takas = LotTaka.objects.filter(source_document_id=result["receipt"].id).order_by("taka_number")
        self.assertEqual([t.available_quantity for t in takas], [D("60"), D("40")])
        self.assertTrue(all(t.location_id == qc_hold.id for t in takas))

    def test_multi_line_disposition_moves_every_line_and_taka(self):
        receipt = self.receive(self.make_bill())["receipt"]
        # 120 at QC Hold: 100 m fabric on two takas + 20 Nos tape.
        result = qc_service.dispose_receipt(
            receipt,
            quantities={"good": D("85"), "second": D("0"), "reprocess": D("0"),
                        "reject": D("25"), "hold": D("10")},
            user=self.user,
            remarks="shade variation",
        )
        self.assertEqual(result["verdict"], "On Hold")
        reject = stock.system_location(self.client_obj.id, "REJECT")
        qc_hold = stock.system_location(self.client_obj.id, "QC_HOLD")
        # Takas first (fabric 60 + 25 good, 15 reject), then tape (10 reject, 10 hold).
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("85"))
        self.assertEqual(self.on_hand(self.fabric, reject), D("15"))
        self.assertEqual(self.on_hand(self.plain, reject), D("10"))
        self.assertEqual(self.on_hand(self.plain, qc_hold), D("10"))
        self.assertEqual(self.on_hand(self.fabric, qc_hold), D("0"))
        # Lot quantities equal the ledger at every location.
        for location in (self.godown, reject):
            lots = LotTaka.objects.filter(
                client_id=self.client_obj.id, item=self.fabric, location=location,
                deleted_at__isnull=True,
            )
            self.assertEqual(
                sum((l.available_quantity for l in lots), D("0")),
                self.on_hand(self.fabric, location),
            )
        receipt.refresh_from_db()
        bill = PurchaseBill.objects.get(pk=receipt.purchase_bill_id)
        self.assertEqual(receipt.qc_status, "On Hold")
        self.assertEqual(bill.qc_status, "Pending Approval")
        self.assertEqual(
            sorted(l.rejected_qty for l in receipt.lines.all()), [D("10"), D("15")]
        )

        # Releasing the held remainder completes QC as a partial approval.
        result = qc_service.dispose_receipt(
            receipt,
            quantities={"good": D("10"), "second": D("0"), "reprocess": D("0"),
                        "reject": D("0"), "hold": D("0")},
            user=self.user,
        )
        self.assertEqual(result["verdict"], "Partially Approved")
        self.assertEqual(self.on_hand(self.plain, self.godown), D("10"))

    def test_disposition_total_must_match_qc_hold(self):
        receipt = self.receive(self.make_bill())["receipt"]
        with self.assertRaises(ValidationFailed):
            qc_service.dispose_receipt(
                receipt,
                quantities={"good": D("50"), "second": D("0"), "reprocess": D("0"),
                            "reject": D("0"), "hold": D("0")},
                user=self.user,
            )

    def test_bill_level_approval_releases_qc_hold(self):
        bill = self.make_bill()
        self.receive(bill)
        purchase_services.update_qc_status(bill, "Approved", user=self.user)
        bill.refresh_from_db()
        qc_hold = stock.system_location(self.client_obj.id, "QC_HOLD")
        self.assertEqual(bill.qc_status, "Approved")
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("100"))
        self.assertEqual(self.on_hand(self.fabric, qc_hold), D("0"))
        self.assertEqual(
            GoodsReceipt.objects.get(purchase_bill=bill).qc_status, "Approved"
        )

    def test_cancelling_bill_after_qc_reverses_all_movements(self):
        bill = self.make_bill()
        self.receive(bill, qc_status="Approved")
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("100"))
        purchase_services.cancel_bill(bill, reason="test", user=self.user)
        qc_hold = stock.system_location(self.client_obj.id, "QC_HOLD")
        for location in (self.godown, qc_hold):
            self.assertEqual(self.on_hand(self.fabric, location), D("0"))
            self.assertEqual(self.on_hand(self.plain, location), D("0"))
        self.assertFalse(
            LotTaka.objects.filter(item=self.fabric, available_quantity__gt=0).exists()
        )


class SampleReturnRoutingTests(StockBase):
    def post_sample(self, data):
        view = InventoryDashboardView.as_view()
        request = self.factory.post("/inventory/stock/", data, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.user)
        return view(request)

    def test_sample_issue_and_condition_routed_returns(self):
        lot = self.make_lot("50")
        res = self.post_sample({
            "action": "issue_sample", "itemId": str(self.fabric.id), "lotTakaId": str(lot.id),
            "qty": "6", "customer": "Buyer", "locationId": str(self.godown.id),
        })
        self.assertEqual(res.status_code, 200, res.data)
        lot.refresh_from_db()
        self.assertEqual(lot.available_quantity, D("44"))
        self.assertTrue(res.data["issueNo"].startswith("SMPL-"))
        issue_id = res.data["movementId"]

        res = self.post_sample({"action": "return_sample", "movementId": issue_id,
                                "returnQty": "2", "condition": "Good"})
        self.assertEqual(res.status_code, 200, res.data)
        lot.refresh_from_db()
        self.assertEqual(lot.available_quantity, D("46"))
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("46"))

        res = self.post_sample({"action": "return_sample", "movementId": issue_id,
                                "returnQty": "3", "condition": "Reject"})
        self.assertEqual(res.status_code, 200, res.data)
        reject = stock.system_location(self.client_obj.id, "REJECT")
        self.assertEqual(self.on_hand(self.fabric, reject), D("3"))
        # The rejected part is a child taka at Reject; the parent stays put.
        child = LotTaka.objects.get(location=reject, item=self.fabric)
        self.assertEqual(child.available_quantity, D("3"))
        self.assertEqual(child.qc_status, "Rejected")

        res = self.post_sample({"action": "return_sample", "movementId": issue_id,
                                "returnQty": "1", "condition": "Hold"})
        self.assertEqual(self.on_hand(self.fabric, stock.system_location(self.client_obj.id, "QC_HOLD")), D("1"))

        # Nothing left outstanding: an over-return is refused.
        res = self.post_sample({"action": "return_sample", "movementId": issue_id,
                                "returnQty": "1", "condition": "Good"})
        self.assertEqual(res.status_code, 400)


class SalesReturnRoutingTests(StockBase):
    def test_sales_return_routes_each_line_by_condition(self):
        from apps.sales.models import SalesInvoice, SalesInvoiceLine, SalesReturn, SalesReturnLine
        from apps.sales.views import SalesReturnViewSet

        invoice = SalesInvoice.objects.create(
            client=self.client_obj, party=self.customer, doc_date=date.today(),
            location=self.godown, status="Unpaid",
        )
        inv_line = SalesInvoiceLine.objects.create(
            client=self.client_obj, sales_invoice=invoice, line_no=1, item=self.plain,
            item_name=self.plain.name, uom="Nos", qty=D("10"), rate=D("20"),
            amount=D("200"), tax_pct=D("0"), line_total=D("200"),
        )
        sales_return = SalesReturn.objects.create(
            client=self.client_obj, party=self.customer, doc_date=date.today(),
            sales_invoice=invoice, location=self.godown, return_number=uid("SR"),
        )
        for no, (qty, condition) in enumerate(
            [(D("3"), "Good"), (D("2"), "Second Quality"), (D("1"), "Reprocess"), (D("4"), "Reject")],
            start=1,
        ):
            SalesReturnLine.objects.create(
                client=self.client_obj, sales_return=sales_return, sales_invoice_line=inv_line,
                line_no=no, item=self.plain, item_name=self.plain.name, uom="Nos",
                returned_qty=qty, rate=D("20"), condition=condition,
            )
        viewset = SalesReturnViewSet()
        viewset.request = type("R", (), {"user": self.user, "client_id": self.client_obj.id})()
        viewset._validate_and_post_return(sales_return, invoice)

        loc = lambda role: stock.system_location(self.client_obj.id, role)  # noqa: E731
        self.assertEqual(self.on_hand(self.plain, self.godown), D("3"))
        self.assertEqual(self.on_hand(self.plain, loc("SECOND_QUALITY")), D("2"))
        self.assertEqual(self.on_hand(self.plain, loc("REPROCESS")), D("1"))
        self.assertEqual(self.on_hand(self.plain, loc("REJECT")), D("4"))
        types = set(
            StockMovement.objects.filter(reference_id=sales_return.id).values_list("type", flat=True)
        )
        self.assertEqual(types, {
            "SALES_RETURN_GOOD", "SALES_RETURN_SECOND_QUALITY",
            "SALES_RETURN_REPROCESS", "SALES_RETURN_REJECT",
        })
        # Only the Good part is available to sell again.
        self.assertEqual(
            stock.calculate_item_stock(self.client_obj.id, self.plain)["available"], D("3")
        )


class TransferTests(StockBase):
    def test_partial_taka_transfer_and_cancel(self):
        lot_a = self.make_lot("60", taka="A")
        lot_b = self.make_lot("40", taka="B")
        res = self.call(
            StockTransferViewSet, "post", "/inventory/transfers/",
            {
                "sourceLocationId": str(self.godown.id),
                "destLocationId": str(self.godown2.id),
                "date": date.today().isoformat(),
                "items": [{
                    "itemId": str(self.fabric.id), "qty": "75",
                    "takas": [{"lotTakaId": str(lot_a.id), "qty": "60"},
                              {"lotTakaId": str(lot_b.id), "qty": "15"}],
                }],
            },
            actions={"post": "create"},
        )
        self.assertEqual(res.status_code, 201, res.data)
        transfer_id = res.data["id"]
        self.assertEqual(res.data["status"], "Requested")

        res = self.call(
            StockTransferViewSet, "post", f"/inventory/transfers/{transfer_id}/dispatch/",
            actions={"post": "dispatch_action"}, pk=transfer_id,
        )
        self.assertEqual(res.status_code, 200, res.data)
        lot_a.refresh_from_db()
        lot_b.refresh_from_db()
        self.assertEqual(lot_a.location_id, self.godown2.id)  # whole taka moved
        self.assertEqual(lot_a.available_quantity, D("60"))
        self.assertEqual(lot_b.location_id, self.godown.id)   # partial: split
        self.assertEqual(lot_b.available_quantity, D("25"))
        child = LotTaka.objects.get(source_document_id=transfer_id, deleted_at__isnull=True)
        self.assertEqual(child.available_quantity, D("15"))
        self.assertEqual(child.location_id, self.godown2.id)
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("25"))
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("75"))

        # An unrelated taka already at the destination must not move back.
        bystander = self.make_lot("5", location=self.godown2, taka="Z")

        res = self.call(
            StockTransferViewSet, "post", f"/inventory/transfers/{transfer_id}/cancel/",
            actions={"post": "cancel"}, pk=transfer_id,
        )
        self.assertEqual(res.status_code, 200, res.data)
        lot_a.refresh_from_db()
        lot_b.refresh_from_db()
        bystander.refresh_from_db()
        self.assertEqual((lot_a.location_id, lot_a.available_quantity), (self.godown.id, D("60")))
        self.assertEqual((lot_b.location_id, lot_b.available_quantity), (self.godown.id, D("40")))
        self.assertEqual(bystander.location_id, self.godown2.id)
        self.assertFalse(
            LotTaka.objects.filter(source_document_id=transfer_id, deleted_at__isnull=True).exists()
        )
        self.assertEqual(self.on_hand(self.fabric, self.godown), D("100"))
        self.assertEqual(self.on_hand(self.fabric, self.godown2), D("5"))

    def test_transfer_rejects_taka_quantity_mismatch(self):
        lot = self.make_lot("10")
        res = self.call(
            StockTransferViewSet, "post", "/inventory/transfers/",
            {
                "sourceLocationId": str(self.godown.id),
                "destLocationId": str(self.godown2.id),
                "date": date.today().isoformat(),
                "items": [{"itemId": str(self.fabric.id), "qty": "8",
                           "takas": [{"lotTakaId": str(lot.id), "qty": "5"}]}],
            },
            actions={"post": "create"},
        )
        self.assertEqual(res.status_code, 400)
