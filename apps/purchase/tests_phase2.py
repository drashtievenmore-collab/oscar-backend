"""Phase 2.2 Grey Purchase & Lot/Taka Capture tests.

Run with::

    python manage.py test apps.purchase.tests_phase2
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.exceptions import ValidationFailed
from apps.inventory import services as stock
from apps.inventory.models import LotTaka, StockMovement
from apps.masters.models import FabricQuality, Item, Location, Party
from apps.purchase import services
from apps.purchase.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseBill,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
)


class Phase2PurchaseTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Oscar Phase 2 Client", slug="oscar-p2")
        self.user = User.objects.create_user(
            email="p2purchaser@example.com",
            password="secretpassword",
            client=self.tenant,
            is_superuser=True,
        )
        from apps.accounting.services import seed_chart_of_accounts
        seed_chart_of_accounts(self.tenant)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.quality = FabricQuality.objects.create(
            client=self.tenant,
            name="Cotton Cambric 60s",
            code="CC60",
        )
        self.fabric = Item.objects.create(
            client=self.tenant,
            sku="GREY-CC60",
            name="Grey Cotton Cambric 60s",
            uom="Mtr",
            item_kind="Standalone",
            fabric_quality=self.quality,
            cost_price=Decimal("45.00"),
        )
        self.supplier = Party.objects.create(
            client=self.tenant,
            name="Apex Weaving Mills",
            code="V-APEX",
            type="Vendor",
            gstin="27AAPCA1234F1Z5",
        )
        self.warehouse = Location.objects.create(
            client=self.tenant,
            name="Main Textile Godown",
            code="WH-MAIN",
            type="Warehouse",
        )
        self.qc_hold = Location.objects.create(
            client=self.tenant,
            name="QC Hold Area",
            code="QC-HOLD",
            type="Warehouse",
        )

        po_res = self.api.post(
            "/api/v1/purchase/orders/",
            {
                "vendorId": str(self.supplier.id),
                "date": "2026-10-01",
                "lineItems": [
                    {
                        "itemId": str(self.fabric.id),
                        "qty": 1000,
                        "rate": 45,
                        "tax": 5,
                        "uom": "Mtr",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(po_res.status_code, 201, po_res.content)
        self.po = PurchaseOrder.objects.get(pk=po_res.json()["id"])
        self.po_line = self.po.line_items.first()

    def test_standalone_grn_creation_with_takas(self):
        takas = [
            {"taka_number": "TK-01", "actual_meter": 250, "weight": 28.5, "width": 44},
            {"taka_number": "TK-02", "actual_meter": 250, "weight": 28.0, "width": 44},
            {"taka_number": "TK-03", "actual_meter": 250, "weight": 29.0, "width": 44},
            {"taka_number": "TK-04", "actual_meter": 250, "weight": 28.5, "width": 44},
        ]
        grn = services.create_standalone_grn(
            client=self.tenant,
            user=self.user,
            party=self.supplier,
            supplier_challan_no="CH-9876",
            supplier_challan_date=date(2026, 10, 14),
            purchase_order=self.po,
            lines_data=[
                {
                    "item_id": str(self.fabric.id),
                    "po_line_id": str(self.po_line.id),
                    "received_qty": 1000,
                    "batch_number": "LOT-2026-OCT-01",
                    "takas": takas,
                }
            ],
        )

        self.assertIsNotNone(grn.id)
        self.assertEqual(grn.supplier_challan_no, "CH-9876")
        self.assertEqual(grn.supplier_challan_date, date(2026, 10, 14))
        self.assertEqual(grn.lines.count(), 1)

        line = grn.lines.first()
        self.assertEqual(line.received_qty, Decimal("1000.0000"))
        self.assertEqual(line.roll_count, 4)

        # Check LotTaka rows created in QC_HOLD
        takas_created = LotTaka.objects.filter(client=self.tenant, lot_number="LOT-2026-OCT-01")
        self.assertEqual(takas_created.count(), 4)
        for taka in takas_created:
            self.assertEqual(taka.received_quantity, Decimal("250.0000"))
            self.assertEqual(taka.location.code, "SYS-QC-HOLD")
            self.assertEqual(taka.qc_status, "Pending")
            self.assertIsNotNone(taka.stock_movement)
            self.assertEqual(taka.stock_movement.type, "PURCHASE")
            self.assertEqual(taka.stock_movement.quantity, Decimal("250.0000"))

        # Verify PO line received_qty updated
        self.po_line.refresh_from_db()
        self.assertEqual(self.po_line.received_qty, Decimal("1000.0000"))

    def test_taka_sum_validation_failure(self):
        takas = [
            {"taka_number": "TK-01", "actual_meter": 200},
            {"taka_number": "TK-02", "actual_meter": 200},
        ]  # Sum is 400, but line received_qty is 500
        with self.assertRaises(ValidationFailed):
            services.create_standalone_grn(
                client=self.tenant,
                user=self.user,
                party=self.supplier,
                purchase_order=self.po,
                lines_data=[
                    {
                        "item_id": str(self.fabric.id),
                        "po_line_id": str(self.po_line.id),
                        "received_qty": 500,
                        "batch_number": "LOT-INVALID",
                        "takas": takas,
                    }
                ],
            )

    def test_shortage_excess_recording(self):
        # PO has 1000 m. Receive only 950 m.
        takas = [
            {"taka_number": "TK-A", "actual_meter": 475},
            {"taka_number": "TK-B", "actual_meter": 475},
        ]
        grn = services.create_standalone_grn(
            client=self.tenant,
            user=self.user,
            party=self.supplier,
            purchase_order=self.po,
            lines_data=[
                {
                    "item_id": str(self.fabric.id),
                    "po_line_id": str(self.po_line.id),
                    "received_qty": 950,
                    "batch_number": "LOT-SHORT",
                    "takas": takas,
                }
            ],
        )
        line = grn.lines.first()
        self.assertEqual(line.shortage_qty, Decimal("50.0000"))
        self.assertEqual(line.excess_qty, Decimal("0.0000"))

    def test_supplier_reconciliation_api(self):
        # Receive 1000 m via GRN
        takas = [{"taka_number": f"TK-{i}", "actual_meter": 200} for i in range(5)]
        services.create_standalone_grn(
            client=self.tenant,
            user=self.user,
            party=self.supplier,
            purchase_order=self.po,
            lines_data=[
                {
                    "item_id": str(self.fabric.id),
                    "po_line_id": str(self.po_line.id),
                    "received_qty": 1000,
                    "batch_number": "LOT-RECON",
                    "takas": takas,
                }
            ],
        )

        res = self.api.get("/api/v1/purchase/receipts/reconciliation/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("summary", data)
        self.assertIn("rows", data)
        self.assertEqual(Decimal(str(data["summary"]["totalOrderedQty"])), Decimal("1000.0000"))
        self.assertEqual(Decimal(str(data["summary"]["totalReceivedQty"])), Decimal("1000.0000"))

    def test_supplier_ledger_api(self):
        res = self.api.get(
            "/api/v1/purchase/receipts/supplier-ledger/",
            {"vendorId": str(self.supplier.id)},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["vendor"]["name"], self.supplier.name)
        self.assertIn("entries", data)
        self.assertIn("closingBalance", data)

    def test_purchase_return_with_lot_taka(self):
        bill_res = self.api.post(f"/api/v1/purchase/orders/{self.po.id}/convert-to-bill/", {})
        self.assertEqual(bill_res.status_code, 201)
        bill_data = bill_res.json()
        bill_line_id = bill_data["lineItems"][0]["id"]

        # Create GRN and taka
        grn = services.create_standalone_grn(
            client=self.tenant,
            user=self.user,
            party=self.supplier,
            lines_data=[
                {
                    "item_id": str(self.fabric.id),
                    "received_qty": 100,
                    "batch_number": "LOT-RETURN",
                    "takas": [{"taka_number": "TK-RET", "actual_meter": 100}],
                }
            ],
        )
        taka = LotTaka.objects.get(lot_number="LOT-RETURN", taka_number__contains="TK-RET")
        taka.available_quantity = Decimal("100.0000")
        taka.qc_status = "Passed"
        taka.save()

        # Submit purchase return with lot/taka
        res = self.api.post(
            "/api/v1/purchase/returns/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseBillId": bill_data["id"],
                "date": "2026-10-15",
                "locationId": str(taka.location_id),
                "reason": "Quality failure",
                "takaIds": [str(taka.id)],
                "lineItems": [
                    {
                        "itemId": str(self.fabric.id),
                        "purchaseBillLineId": bill_line_id,
                        "returnedQty": 100,
                        "rate": 45,
                        "tax": 5,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

        taka.refresh_from_db()
        self.assertEqual(taka.status, "Returned")
        self.assertEqual(taka.available_quantity, Decimal("0.0000"))
