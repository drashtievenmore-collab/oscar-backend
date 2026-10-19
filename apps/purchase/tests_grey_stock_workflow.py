"""Ready grey stock purchase alongside Vendor A production.

Run with::

    python manage.py test apps.purchase.tests_grey_stock_workflow
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.exceptions import BusinessRuleViolation, Conflict
from apps.core.models import AuditLog
from apps.inventory import allocation_services as allocations
from apps.inventory import qc as qc_service
from apps.inventory.models import GreyStockAllocation, LotTaka
from apps.masters.models import FabricQuality, Item, Location, Party
from apps.pms.models import ProductionInstruction, Project
from apps.purchase import services
from apps.purchase.models import GoodsReceipt, PurchaseBill, PurchaseOrder, VendorBill


class GreyStockWorkflowTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Grey Flow Client", slug="grey-flow")
        self.user = User.objects.create_user(
            email="buyer@example.com", password="secretpassword",
            client=self.tenant, is_superuser=True,
        )
        from apps.accounting.services import seed_chart_of_accounts
        seed_chart_of_accounts(self.tenant)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.quality = FabricQuality.objects.create(client=self.tenant, name="Rayon 60x60", code="RY60")
        self.fabric = Item.objects.create(
            client=self.tenant, sku="GREY-RY60", name="Grey Rayon 60x60", uom="Mtr",
            item_kind="Standalone", fabric_quality=self.quality, cost_price=Decimal("50.00"),
        )
        self.supplier = Party.objects.create(
            client=self.tenant, name="Shree Weaving", code="V-SHREE", type="Vendor",
        )
        self.agency = Party.objects.create(
            client=self.tenant, name="Vendor A Looms", code="V-A", type="Vendor",
        )
        self.godown = Location.objects.create(
            client=self.tenant, name="Main Godown", code="WH-MAIN", type="Warehouse",
        )
        self.project_a = Project.objects.create(client=self.tenant, code="PRJ-A", customer_name="Alpha Prints")
        self.project_b = Project.objects.create(client=self.tenant, code="PRJ-B", customer_name="Beta Fashions")

    # -- helpers -----------------------------------------------------------
    def _create_po(self, *, qty=1000, rate=50, tax=5, purchase_type="Grey Stock Purchase", project=None, vendor=None):
        payload = {
            "vendorId": str((vendor or self.supplier).id),
            "date": "2026-10-19",
            "purchaseType": purchase_type,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": tax, "uom": "Mtr"}],
        }
        if project is not None:
            payload["projectId"] = str(project.id)
        res = self.api.post("/api/v1/purchase/orders/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return PurchaseOrder.objects.get(pk=res.json()["id"])

    def _approve(self, po):
        for step in ("submit-for-approval", "approve"):
            res = self.api.post(f"/api/v1/purchase/orders/{po.id}/{step}/", {}, format="json")
            self.assertEqual(res.status_code, 200, res.content)
        po.refresh_from_db()
        self.assertEqual(po.status, "Approved")
        return po

    def _grn(self, po, qty, challan, takas=None, headers=None):
        line = po.line_items.first()
        body = {
            "vendorId": str(po.party_id),
            "purchaseOrderId": str(po.id),
            "locationId": str(self.godown.id),
            "receiptDate": "2026-10-20",
            "supplierChallanNo": challan,
            "lines": [{
                "itemId": str(self.fabric.id),
                "poLineId": str(line.id),
                "receivedQty": qty,
                "takas": takas or [{"taka_number": "1", "qty": qty}],
            }],
        }
        return self.api.post("/api/v1/purchase/receipts/", body, format="json", **(headers or {}))

    def _qc(self, grn_id, **quantities):
        receipt = GoodsReceipt.objects.get(pk=grn_id)
        qc_service.dispose_receipt(
            receipt,
            quantities={k: Decimal(str(v)) for k, v in quantities.items()},
            user=self.user,
            require_job_work_order=False,
        )

    def _vendor_bill(self, po, number, qty, rate, gst=5):
        res = self.api.post("/api/v1/purchase/vendor-bills/", {
            "vendorBillNumber": number,
            "billDate": "2026-10-21",
            "vendorId": str(po.party_id),
            "purchaseOrderId": str(po.id),
            "gstPct": gst,
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return VendorBill.objects.get(pk=res.json()["id"])

    # -- 1. project-linked purchase ------------------------------------------
    def test_project_linked_grey_purchase(self):
        po = self._create_po(project=self.project_a)
        self.assertEqual(po.purchase_type, "Grey Stock Purchase")
        self.assertEqual(po.project_id, self.project_a.id)
        # Older project screens find their POs by reference_number.
        self.assertEqual(po.reference_number, "PRJ-A")
        data = self.api.get(f"/api/v1/purchase/orders/{po.id}/").json()
        self.assertEqual(data["purchaseType"], "Grey Stock Purchase")
        self.assertEqual(data["projectCode"], "PRJ-A")
        # A ready-fabric PO never enters the production cycle.
        self._approve(po)
        res = self.api.post(f"/api/v1/purchase/orders/{po.id}/start-production/")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(ProductionInstruction.objects.filter(order_reference=po.po_number).exists())

    # -- 2. general stock purchase, GRN -> QC -> stock -----------------------
    def test_general_stock_purchase_flow(self):
        po = self._create_po()
        self.assertIsNone(po.project_id)
        res = self._grn(po, 1000, "CH-1")
        self.assertEqual(res.status_code, 409, "Draft PO must not be receivable")
        self._approve(po)
        res = self._grn(po, 1000, "CH-1")
        self.assertEqual(res.status_code, 201, res.content)
        lot = LotTaka.objects.get(source_document_id=res.json()["id"])
        # Received stock waits at QC Hold and is not usable yet.
        self.assertEqual(lot.qc_status, "Pending")
        self.assertEqual(lot.location.code, "SYS-QC-HOLD")
        self.assertEqual(allocations.allocatable_lots(self.tenant.id), [])
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Received")

        self._qc(res.json()["id"], good=1000)
        rows = allocations.allocatable_lots(self.tenant.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], Decimal("1000.0000"))

    # -- 3. later allocation to several projects -----------------------------
    def test_allocate_general_stock_to_multiple_projects(self):
        po = self._approve(self._create_po())
        grn_id = self._grn(po, 1000, "CH-2").json()["id"]
        self._qc(grn_id, good=1000)
        lot_id = allocations.allocatable_lots(self.tenant.id)[0][0].id

        res = self.api.post(
            f"/api/v1/pms/projects/{self.project_a.code}/grey-stock-allocation/",
            {"allocations": [{"lotTakaId": str(lot_id), "allocateQty": 600}]}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["allocatedMeters"], 600.0)

        allocations.allocate_to_project(
            client_id=self.tenant.id, project=self.project_b, lot_taka_id=lot_id,
            quantity=400, user=self.user,
        )
        lot = LotTaka.objects.get(pk=lot_id)
        # Reservation only: the ledger quantity is untouched, the lot is fully earmarked.
        self.assertEqual(lot.available_quantity, Decimal("1000.0000"))
        self.assertEqual(lot.status, "Reserved")
        with self.assertRaises(Conflict):
            allocations.allocate_to_project(
                client_id=self.tenant.id, project=self.project_b, lot_taka_id=lot_id,
                quantity=1, user=self.user,
            )
        # Project A sees 600 as its own and 400 as reserved by others.
        lots = self.api.get(f"/api/v1/pms/projects/{self.project_a.code}/grey-stock-allocation/").json()["lots"]
        row = next(r for r in lots if r["lotTakaId"] == str(lot_id))
        self.assertEqual((row["allocateQty"], row["reserved"], row["balance"]), (600.0, 400.0, 0.0))

        # Reducing A's allocation frees stock again.
        res = self.api.post(
            f"/api/v1/pms/projects/{self.project_a.code}/grey-stock-allocation/",
            {"allocations": [{"lotTakaId": str(lot_id), "allocateQty": 250}]}, format="json",
        )
        self.assertEqual(res.json()["allocatedMeters"], 250.0)
        lot.refresh_from_db()
        self.assertEqual(lot.status, "Available")
        self.assertEqual(allocations.allocatable_lots(self.tenant.id)[0][2], Decimal("350.0000"))
        self.assertTrue(AuditLog.objects.filter(entity_type="GreyStockAllocation", action="release").exists())

    # -- 4. partial delivery, several GRNs ------------------------------------
    def test_partial_delivery_and_multiple_grns(self):
        po = self._approve(self._create_po())
        self.assertEqual(self._grn(po, 400, "CH-3A").status_code, 201)
        po.refresh_from_db()
        self.assertEqual(po.status, "Partially Received")
        self.assertEqual(self._grn(po, 600, "CH-3B").status_code, 201)
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Received")
        self.assertEqual(po.line_items.first().received_qty, Decimal("1000.0000"))

        bill = self._vendor_bill(po, "INV-3", 1000, 50)
        result = services.compute_vendor_bill_matching(self.tenant.id, bill)
        self.assertEqual(result["summary"]["grnQty"], 1000.0)
        self.assertEqual(len(result["grnNumbers"]), 2)
        self.assertEqual(result["overallStatus"], "Match", result["reasons"])

    # -- 5. QC pending / approved / partial / rejected ------------------------
    def test_qc_outcomes_control_usable_stock(self):
        po = self._approve(self._create_po(qty=1000))
        grn_id = self._grn(po, 1000, "CH-5").json()["id"]
        pending = LotTaka.objects.get(source_document_id=grn_id)
        with self.assertRaises(BusinessRuleViolation):
            allocations.allocate_to_project(
                client_id=self.tenant.id, project=self.project_a, lot_taka_id=pending.id,
                quantity=10, user=self.user,
            )
        # Partially approved: 700 good, 300 rejected.
        self._qc(grn_id, good=700, reject=300)
        rows = allocations.allocatable_lots(self.tenant.id)
        self.assertEqual(sum(r[2] for r in rows), Decimal("700.0000"))
        rejected = LotTaka.objects.filter(client=self.tenant, qc_status="Rejected")
        self.assertTrue(rejected.exists())
        for lot in rejected:
            with self.assertRaises(BusinessRuleViolation):
                allocations.allocate_to_project(
                    client_id=self.tenant.id, project=self.project_a, lot_taka_id=lot.id,
                    quantity=1, user=self.user,
                )

    # -- 6. bill matching ------------------------------------------------------
    def test_bill_matching_detects_qty_rate_and_tax_variance(self):
        po = self._approve(self._create_po(qty=1000, rate=50, tax=5))
        self._grn(po, 900, "CH-6")
        ok = self._vendor_bill(po, "INV-6-OK", 900, 50, gst=5)
        self.assertEqual(services.compute_vendor_bill_matching(self.tenant.id, ok)["overallStatus"], "Match")
        ok.status = "Rejected"
        ok.save(update_fields=["status"])

        over = self._vendor_bill(po, "INV-6-QTY", 950, 50, gst=5)
        result = services.compute_vendor_bill_matching(self.tenant.id, over)
        self.assertEqual(result["overallStatus"], "Mismatch")
        self.assertFalse(result["lines"][0]["qtyMatch"])
        over.status = "Rejected"
        over.save(update_fields=["status"])

        rate = self._vendor_bill(po, "INV-6-RATE", 900, 52, gst=12)
        result = services.compute_vendor_bill_matching(self.tenant.id, rate)
        self.assertFalse(result["lines"][0]["rateMatch"])
        self.assertFalse(result["lines"][0]["taxMatch"])
        self.assertTrue(any("rate" in r for r in result["reasons"]))

    def test_second_bill_cannot_rebill_received_qty(self):
        po = self._approve(self._create_po(qty=1000))
        self._grn(po, 500, "CH-6B")
        self._vendor_bill(po, "INV-6B-1", 500, 50)
        second = self._vendor_bill(po, "INV-6B-2", 500, 50)
        result = services.compute_vendor_bill_matching(self.tenant.id, second)
        self.assertEqual(result["overallStatus"], "Mismatch")
        self.assertEqual(result["lines"][0]["previouslyBilledQty"], 500.0)

    # -- 7. mismatch approval and payable release -------------------------------
    def test_mismatch_needs_remarks_then_releases_payable(self):
        po = self._approve(self._create_po(qty=1000, rate=50))
        grn_id = self._grn(po, 1000, "CH-7").json()["id"]
        self._qc(grn_id, good=1000)
        bill = self._vendor_bill(po, "INV-7", 1000, 51)

        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/send-for-matching/", {}, format="json")
        self.assertEqual(res.status_code, 409, res.content)
        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 400, res.content)

        res = self.api.post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/approve/",
            {"remarks": "Rate revised by Re 1 per supplier email"}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        bill.refresh_from_db()
        self.assertEqual(bill.approval_history[-1]["action"], "Approved with variance")
        self.assertTrue(bill.approval_history[-1]["variances"])
        self.assertTrue(AuditLog.objects.filter(entity_type="VendorBill", action="approve").exists())

        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/send-for-matching/", {}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(PurchaseBill.objects.filter(pk=res.json()["purchaseBillId"]).exists())
        # The mismatch never touched the QC-approved stock or the source documents.
        self.assertEqual(sum(r[2] for r in allocations.allocatable_lots(self.tenant.id)), Decimal("1000.0000"))
        self.assertEqual(po.line_items.first().rate, Decimal("50.0000"))

    # -- 8. Vendor A production keeps its own cycle -----------------------------
    def test_vendor_a_production_flow_is_kept(self):
        from datetime import date
        from apps.hrms.models import Employee
        # The production instruction needs the salaried employee who checks it.
        Employee.objects.create(
            client=self.tenant, employee_code="EMP-1", name="Daily Checker",
            status="Active", joining_date=date(2025, 1, 1),
        )
        po = self._approve(self._create_po(purchase_type="Vendor A Production", vendor=self.agency))
        res = self.api.post(f"/api/v1/purchase/orders/{po.id}/start-production/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(ProductionInstruction.objects.filter(order_reference=po.po_number).exists())
        po.refresh_from_db()
        self.assertEqual(po.status, "In Production")
        # Its fabric is not received through a grey stock GRN.
        self.assertEqual(self._grn(po, 100, "CH-8").status_code, 409)

    def test_final_job_work_type_cannot_be_typed_in(self):
        res = self.api.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.agency.id), "date": "2026-10-19", "purchaseType": "Final Job Work",
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 1, "rate": 1, "uom": "Mtr"}],
        }, format="json")
        self.assertEqual(res.status_code, 400, res.content)

    def test_purchase_type_locked_after_draft(self):
        po = self._approve(self._create_po())
        res = self.api.patch(f"/api/v1/purchase/orders/{po.id}/", {"purchaseType": "Vendor A Production"}, format="json")
        self.assertIn(res.status_code, (400, 409), res.content)
        po.refresh_from_db()
        self.assertEqual(po.purchase_type, "Grey Stock Purchase")

    # -- 9. duplicates and insufficient stock ------------------------------------
    def test_duplicate_grn_and_idempotent_replay(self):
        po = self._approve(self._create_po())
        headers = {"HTTP_IDEMPOTENCY_KEY": "grn-dup-1"}
        first = self._grn(po, 500, "CH-9", headers=headers)
        self.assertEqual(first.status_code, 201, first.content)
        replay = self._grn(po, 500, "CH-9", headers=headers)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json()["id"], first.json()["id"])
        # Same supplier challan without the key is refused, not posted twice.
        self.assertEqual(self._grn(po, 500, "CH-9").status_code, 409)
        self.assertEqual(GoodsReceipt.objects.filter(purchase_order=po).count(), 1)
        self.assertEqual(po.line_items.first().received_qty, Decimal("500.0000"))

    def test_cannot_allocate_more_than_available(self):
        po = self._approve(self._create_po(qty=300))
        grn_id = self._grn(po, 300, "CH-9B").json()["id"]
        self._qc(grn_id, good=300)
        lot_id = allocations.allocatable_lots(self.tenant.id)[0][0].id
        res = self.api.post(
            f"/api/v1/pms/projects/{self.project_a.code}/grey-stock-allocation/",
            {"allocations": [{"lotTakaId": str(lot_id), "allocateQty": 301}]}, format="json",
        )
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(GreyStockAllocation.objects.filter(project=self.project_a).exists())
