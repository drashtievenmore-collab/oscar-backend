"""UOM master rules, conversion, and how units flow through documents and stock."""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.exceptions import ValidationFailed
from apps.inventory import services as stock
from apps.inventory.models import StockMovement

from . import uom
from .models import Item, Location, Party, Unit


class UomTestBase(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="uom-test-tenant", defaults={"name": "UOM Test Tenant"}
        )
        self.user = User.objects.create_user(
            email="uom-tests@example.com", password="pass", client=self.client_obj,
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        unit = lambda **kw: Unit.objects.create(client=self.client_obj, **kw)  # noqa: E731
        self.meter = unit(code="Meter", label="Meter", uom_type="Length", is_base_unit=True,
                          decimal_places=2, conversion_factor=1)
        self.cm = unit(code="CM", label="Centimeter", uom_type="Length", decimal_places=2,
                       conversion_factor=Decimal("0.01"))
        self.kg = unit(code="Kg", label="Kilogram", uom_type="Weight", is_base_unit=True,
                       decimal_places=3, conversion_factor=1)
        self.gram = unit(code="GRAM", label="Gram", uom_type="Weight", decimal_places=3,
                         conversion_factor=Decimal("0.001"))
        self.roll = unit(code="Roll", label="Roll", uom_type="Roll", is_base_unit=True,
                         decimal_places=0, conversion_factor=1)
        self.taka = unit(code="Taka", label="Taka", uom_type="Roll", decimal_places=0,
                         conversion_factor=1)

        # Existing data says "Mtr" while the master row is "Meter".
        self.fabric = Item.objects.create(
            client=self.client_obj, sku="FAB-UOM-1", name="Grey Cotton", item_kind="Fabric",
            uom="Mtr", cost_price=Decimal("50"),
        )
        self.location = Location.objects.create(
            client=self.client_obj, code="WH-UOM", name="UOM Warehouse", type="Warehouse"
        )
        self.vendor = Party.objects.create(
            client=self.client_obj, code="V-UOM", name="UOM Vendor", type="Vendor"
        )
        self.customer = Party.objects.create(
            client=self.client_obj, code="C-UOM", name="UOM Customer", type="Customer"
        )

    def api_post(self, path, data):
        return self.api.post(f"/api/v1{path}", data, format="json")


class ConversionRuleTests(UomTestBase):
    def test_aliases_resolve_to_the_master_row(self):
        book = uom.UnitBook(self.client_obj.id)
        self.assertEqual(book.resolve("Mtr"), self.meter)
        self.assertEqual(book.resolve("metres"), self.meter)
        self.assertEqual(book.resolve("cm"), self.cm)
        self.assertEqual(book.resolve("KGS"), self.kg)

    def test_meter_to_centimeter_and_kilogram_to_gram(self):
        self.assertEqual(uom.convert(1, self.meter, self.cm), Decimal("100"))
        self.assertEqual(uom.convert(1, self.kg, self.gram), Decimal("1000"))
        self.assertEqual(uom.convert(250, self.cm, self.meter), Decimal("2.5"))

    def test_same_unit_preserves_quantity(self):
        self.assertEqual(uom.convert(Decimal("12.3456"), self.meter, self.meter), Decimal("12.3456"))

    def test_length_never_converts_to_weight(self):
        self.assertFalse(uom.compatible(self.meter, self.kg))
        with self.assertRaises(ValueError):
            uom.convert(1, self.meter, self.kg)

    def test_roll_and_taka_never_convert(self):
        # Same type, but a roll or taka has no fixed size.
        self.assertFalse(uom.compatible(self.taka, self.roll))
        self.assertFalse(uom.compatible(self.roll, self.meter))

    def test_line_factor(self):
        cid = self.client_obj.id
        self.assertEqual(uom.line_factor(cid, self.fabric, "Mtr"), 1)
        self.assertEqual(uom.line_factor(cid, self.fabric, "Meter"), 1)
        self.assertEqual(uom.line_factor(cid, self.fabric, ""), 1)
        self.assertEqual(uom.line_factor(cid, self.fabric, "CM"), Decimal("0.01"))
        for bad in ("Kg", "Roll", "Furlong"):
            with self.assertRaises(ValidationFailed, msg=bad):
                uom.line_factor(cid, self.fabric, bad)

    def test_inactive_unit_is_rejected_on_lines(self):
        self.cm.is_active = False
        self.cm.save()
        with self.assertRaises(ValidationFailed):
            uom.line_factor(self.client_obj.id, self.fabric, "CM")


class DocumentLineUomTests(UomTestBase):
    def _po(self, line_uom, qty=500):
        return self.api_post("/purchase/orders/", {
            "vendorId": str(self.vendor.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": 1, "tax": 0,
                           "uom": line_uom}],
        })

    def test_compatible_unit_is_accepted_with_frozen_factor(self):
        response = self._po("CM")
        self.assertEqual(response.status_code, 201, response.content[:600])
        line = response.json()["lineItems"][0]
        self.assertEqual(line["uom"], "CM")
        self.assertEqual(Decimal(str(line["uomFactor"])), Decimal("0.01"))

    def test_blank_unit_defaults_to_item_unit(self):
        response = self._po(None)
        self.assertEqual(response.status_code, 201, response.content[:600])
        line = response.json()["lineItems"][0]
        self.assertEqual(line["uom"], "Mtr")
        self.assertEqual(Decimal(str(line["uomFactor"])), Decimal("1"))

    def test_incompatible_unit_is_rejected(self):
        for bad in ("Kg", "Roll"):
            response = self._po(bad)
            self.assertEqual(response.status_code, 400, (bad, response.content[:600]))


class StockPostingUomTests(UomTestBase):
    def setUp(self):
        super().setUp()
        from apps.accounting.services import seed_chart_of_accounts

        seed_chart_of_accounts(self.client_obj)  # receipts post to the ledger too

    def _received_bill(self, line_uom, qty, rate):
        order = self.api_post("/purchase/orders/", {
            "vendorId": str(self.vendor.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 0,
                           "uom": line_uom}],
        })
        self.assertEqual(order.status_code, 201, order.content[:600])
        bill = self.api_post(f"/purchase/orders/{order.json()['id']}/convert-to-bill/", {})
        self.assertEqual(bill.status_code, 201, bill.content[:600])
        bill_id = bill.json()["id"]
        received = self.api_post(f"/purchase/bills/{bill_id}/receive-goods/", {
            "locationId": str(self.location.id), "qcStatus": "Approved", "lines": [],
        })
        self.assertIn(received.status_code, (200, 201), received.content[:800])
        return order.json(), bill_id

    def test_receipt_in_cm_posts_meters(self):
        # 500 CM at Rs 0.50/CM = 5 Meter at Rs 50/Meter.
        order, _ = self._received_bill("CM", 500, Decimal("0.5"))
        movement = StockMovement.objects.get(item=self.fabric, type="PURCHASE")
        self.assertEqual(movement.quantity, Decimal("5.0000"))
        self.assertEqual(movement.uom, "Mtr")
        self.assertEqual(movement.unit_cost, Decimal("50.0000"))

        from apps.purchase.models import GoodsReceiptLine, PurchaseOrderLine

        grn_line = GoodsReceiptLine.objects.get(item=self.fabric)
        self.assertEqual(grn_line.uom, "CM")
        self.assertEqual(grn_line.received_qty, Decimal("500.0000"))
        self.assertEqual(grn_line.stock_qty, Decimal("5.0000"))
        self.assertEqual(grn_line.stock_uom, "Mtr")

        # The order roll-up stays in the order's own unit.
        po_line = PurchaseOrderLine.objects.get(purchase_order_id=order["id"])
        self.assertEqual(po_line.received_qty, Decimal("500.0000"))

        stock_now = stock.calculate_item_stock(self.client_obj.id, self.fabric)
        self.assertEqual(stock_now["onHand"], Decimal("5.0000"))

    def test_receipt_cannot_be_posted_twice(self):
        _, bill_id = self._received_bill("Mtr", 10, 50)
        again = self.api_post(f"/purchase/bills/{bill_id}/receive-goods/", {
            "locationId": str(self.location.id), "qcStatus": "Approved", "lines": [],
        })
        self.assertEqual(again.status_code, 409, again.content[:400])
        self.assertEqual(StockMovement.objects.filter(item=self.fabric).count(), 1)

    def test_reservation_is_counted_in_stock_units(self):
        self._received_bill("Mtr", 10, 50)
        order = self.api_post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 250, "rate": 1, "tax": 0,
                           "uom": "CM"}],
        })
        self.assertEqual(order.status_code, 201, order.content[:600])
        reserved = stock.reserved_quantity(self.client_obj.id, [self.fabric.id])
        self.assertEqual(reserved[self.fabric.id], Decimal("2.5"))
        stock_now = stock.calculate_item_stock(self.client_obj.id, self.fabric)
        self.assertEqual(stock_now["available"], Decimal("7.5"))

    def test_dispatch_in_cm_depletes_meters_and_cancel_reverses_once(self):
        self._received_bill("Mtr", 10, 50)
        order = self.api_post("/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 250, "rate": 1, "tax": 0,
                           "uom": "CM"}],
        }).json()
        challan = self.api_post(f"/sales/orders/{order['id']}/convert-to-challan/", {})
        self.assertEqual(challan.status_code, 201, challan.content[:600])
        challan_id = challan.json()["id"]
        self.api.patch(f"/api/v1/sales/challans/{challan_id}/",
                       {"locationId": str(self.location.id)}, format="json")
        dispatched = self.api_post(f"/sales/challans/{challan_id}/dispatch/", {})
        self.assertIn(dispatched.status_code, (200, 201), dispatched.content[:600])

        sale = StockMovement.objects.get(item=self.fabric, type="SALE")
        self.assertEqual(sale.quantity, Decimal("-2.5000"))
        self.assertEqual(sale.uom, "Mtr")
        on_hand = stock.calculate_item_stock(self.client_obj.id, self.fabric)["onHand"]
        self.assertEqual(on_hand, Decimal("7.5000"))

        from apps.sales.models import SalesOrderLine

        # Dispatched is tracked in the order's unit (CM); nothing left reserved.
        self.assertEqual(SalesOrderLine.objects.get(sales_order_id=order["id"]).dispatched_qty,
                         Decimal("250.0000"))
        self.assertEqual(stock.reserved_quantity(self.client_obj.id, [self.fabric.id])
                         .get(self.fabric.id, 0), 0)

        # Repeating dispatch is refused; cancelling twice reverses only once.
        self.assertEqual(self.api_post(f"/sales/challans/{challan_id}/dispatch/", {}).status_code, 409)
        self.api_post(f"/sales/challans/{challan_id}/cancel/", {"reason": "test"})
        self.api_post(f"/sales/challans/{challan_id}/cancel/", {"reason": "test"})
        self.assertEqual(StockMovement.objects.filter(item=self.fabric, type="SALE_REVERSAL").count(), 1)
        on_hand = stock.calculate_item_stock(self.client_obj.id, self.fabric)["onHand"]
        self.assertEqual(on_hand, Decimal("10.0000"))


class UnitProtectionTests(UomTestBase):
    def test_unit_used_by_items_cannot_be_deleted(self):
        response = self.api.delete(f"/api/v1/inventory/units/{self.meter.id}/")
        self.assertEqual(response.status_code, 409, response.content[:400])
        self.assertIn("Inactive", response.json()["message"])

    def test_unused_unit_can_be_deleted(self):
        response = self.api.delete(f"/api/v1/inventory/units/{self.taka.id}/")
        self.assertEqual(response.status_code, 204, response.content[:400])

    def test_type_of_unit_in_use_cannot_change(self):
        response = self.api.patch(
            f"/api/v1/inventory/units/{self.meter.id}/", {"uomType": "Other"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:400])

    def test_item_unit_must_exist_and_be_active(self):
        base = {"name": "Thing", "item_kind": "Consumable"}
        bad = self.api_post("/inventory/items/", {**base, "sku": "T-1", "uom": "Furlong"})
        self.assertEqual(bad.status_code, 400, bad.content[:400])
        good = self.api_post("/inventory/items/", {**base, "sku": "T-2", "uom": "Kg"})
        self.assertEqual(good.status_code, 201, good.content[:400])

    def test_item_unit_is_fixed_once_stock_moved(self):
        stock.post_movement(
            client_id=self.client_obj.id, item=self.fabric, location=self.location,
            type="ADJUSTMENT", quantity=5, notes="opening",
        )
        response = self.api.patch(
            f"/api/v1/inventory/items/{self.fabric.id}/", {"uom": "Kg"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:400])
        # A different spelling of the same unit is not a change.
        same = self.api.patch(
            f"/api/v1/inventory/items/{self.fabric.id}/", {"uom": "Meter"}, format="json"
        )
        self.assertEqual(same.status_code, 200, same.content[:400])
