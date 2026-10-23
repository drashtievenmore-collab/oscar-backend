"""Unit & integration tests for Lot / Taka Register & Inward Pipeline (PDF §07, §09)."""
from datetime import date
from decimal import Decimal
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.models import CompanyProfile
from apps.inventory.models import LotTaka
from apps.masters.models import Item, Location, Party
from apps.purchase.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine


class LotPipelineTests(TestCase):
    """Test Suite verifying PO -> GRN -> Lot / Taka Register splitting and traceability."""

    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="lot-test-tenant", defaults={"name": "Lot Test Tenant"}
        )
        self.user = User.objects.create_user(
            email="lot-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="Lot Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        CompanyProfile.objects.get_or_create(
            client=self.client_obj,
            defaults={
                "legal_name": "Lot Pipeline Textiles Pvt Ltd",
                "trade_name": "Lot Pipeline",
                "gstin": "24AABCL1234E1Z1",
                "state": "Gujarat",
                "state_code": "24",
                "address": {"city": "Surat", "line1": "Ring Road", "pincode": 395002},
            },
        )

        self.vendor = Party.objects.create(
            client=self.client_obj,
            name="Test Mills Pvt Ltd",
            code="VEND-TMPL",
            type="Vendor",
            gstin="24AABCT9999M1Z8",
            place_of_supply="24-Gujarat",
        )

        self.item = Item.objects.create(
            client=self.client_obj,
            name="60s Cambric Pure Cotton",
            sku="FAB-CAMB-60S",
            item_kind="Fabric",
            uom="Meter",
            cost_price=Decimal("120.00"),
        )

        self.location = Location.objects.create(
            client=self.client_obj,
            name="Main Godown",
            code="GODOWN-01",
            type="Godown",
            is_active=True,
        )

        self.qc_hold_location = Location.objects.create(
            client=self.client_obj,
            name="QC Hold Area",
            code="SYS-QC-HOLD",
            type="Godown",
            is_active=True,
        )

        self.uid = uuid.uuid4().hex[:6].upper()

        # Create active PO and GRN for testing
        self.po = PurchaseOrder.objects.create(
            client=self.client_obj,
            po_number=f"PO-TEST-GREY-{self.uid}",
            party=self.vendor,
            party_name=self.vendor.name,
            doc_date=date.today(),
            status="Approved",
            purchase_type="Ready Grey Fabric Purchase",
            subtotal=Decimal("36000.00"),
            total=Decimal("36000.00"),
        )
        self.po_line = PurchaseOrderLine.objects.create(
            client=self.client_obj,
            purchase_order=self.po,
            item=self.item,
            qty=Decimal("300.00"),
            rate=Decimal("120.00"),
            line_total=Decimal("36000.00"),
        )
        self.grn = GoodsReceipt.objects.create(
            client=self.client_obj,
            purchase_order=self.po,
            grn_number=f"GRN-TEST-{self.uid}",
            party=self.vendor,
            location=self.location,
            receipt_date=date.today(),
            qc_status="Pending",
        )
        self.grn_line = GoodsReceiptLine.objects.create(
            client=self.client_obj,
            goods_receipt=self.grn,
            item=self.item,
            ordered_qty=Decimal("300.00"),
            received_qty=Decimal("300.00"),
            fabric_width=Decimal("58.00"),
            unit_cost=Decimal("120.00"),
        )

    def test_01_unreceived_po_query(self):
        """Querying lot register for a PO with no receipts returns empty list without error."""
        po_empty = PurchaseOrder.objects.create(
            client=self.client_obj,
            po_number=f"PO-TEST-EMPTY-{self.uid}",
            party=self.vendor,
            party_name=self.vendor.name,
            doc_date=date.today(),
            status="Approved",
            subtotal=Decimal("10000.00"),
            total=Decimal("10000.00"),
        )
        res = self.api.get(f"/api/v1/inventory/lot-register/?po={po_empty.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data.get("records", [])), 0)

    def test_02_excess_quantity_rejected(self):
        """Total taka meters exceeding GRN received quantity is rejected with validation error."""
        excess_data = {
            "itemId": str(self.item.id),
            "supplierId": str(self.vendor.id),
            "grnId": str(self.grn.id),
            "grnNo": self.grn.grn_number,
            "lotNo": f"LOT-EXCESS-{self.uid}",
            "qcStatus": "Pending",
            "rate": 120,
            "takas": [
                {"takaRoll": "1", "qty": 200},
                {"takaRoll": "2", "qty": 150},  # 350m total vs 300m on GRN
            ],
        }
        res = self.api.post("/api/v1/inventory/lot-register/", excess_data, format="json")
        self.assertGreaterEqual(res.status_code, 400)

    def test_03_valid_batch_split_and_qc_hold(self):
        """Split a 300m GRN into 3 takas (100+100+100); verify placed in QC hold with traceability."""
        valid_data = {
            "itemId": str(self.item.id),
            "supplierId": str(self.vendor.id),
            "grnId": str(self.grn.id),
            "grnNo": self.grn.grn_number,
            "lotNo": f"LOT-SPLIT-{self.uid}",
            "qcStatus": "Pending",
            "rate": 120,
            "width": 58,
            "gsm": 140,
            "takas": [
                {"takaRoll": "1", "qty": 100, "rollWeight": 24, "width": 58, "remarks": "Roll 1"},
                {"takaRoll": "2", "qty": 100, "rollWeight": 25, "width": 58, "remarks": "Roll 2"},
                {"takaRoll": "3", "qty": 100, "rollWeight": 24.5, "width": 58, "remarks": "Roll 3"},
            ],
        }
        res = self.api.post("/api/v1/inventory/lot-register/", valid_data, format="json")
        self.assertIn(res.status_code, (200, 201))
        created_takas = res.data.get("takas", [])
        self.assertEqual(len(created_takas), 3)

        # Verify DB records
        db_takas = LotTaka.objects.filter(
            client_id=self.client_obj.id,
            lot_number=f"LOT-SPLIT-{self.uid}",
            deleted_at__isnull=True,
        )
        self.assertEqual(db_takas.count(), 3)
        for taka in db_takas:
            self.assertEqual(taka.qc_status, "Pending")
            self.assertEqual(taka.source_document_number, self.grn.grn_number)
            self.assertEqual(taka.received_quantity, Decimal("100.00"))

    def test_04_duplicate_roll_prevention(self):
        """Duplicate roll numbers in the same lot are rejected."""
        valid_data = {
            "itemId": str(self.item.id),
            "supplierId": str(self.vendor.id),
            "grnId": str(self.grn.id),
            "grnNo": self.grn.grn_number,
            "lotNo": f"LOT-DUP-{self.uid}",
            "takas": [{"takaRoll": "1", "qty": 100}],
        }
        res = self.api.post("/api/v1/inventory/lot-register/", valid_data, format="json")
        self.assertIn(res.status_code, (200, 201))

        # Attempt duplicate roll '1' in same lot
        dup_data = {
            "itemId": str(self.item.id),
            "supplierId": str(self.vendor.id),
            "grnId": str(self.grn.id),
            "grnNo": self.grn.grn_number,
            "lotNo": f"LOT-DUP-{self.uid}",
            "takas": [{"takaRoll": "1", "qty": 50}],
        }
        dup_res = self.api.post("/api/v1/inventory/lot-register/", dup_data, format="json")
        self.assertGreaterEqual(dup_res.status_code, 400)

    def test_05_prefilter_by_grn_and_po(self):
        """Filter lot takas by GRN number and PO id."""
        # First create lot
        lot_data = {
            "itemId": str(self.item.id),
            "supplierId": str(self.vendor.id),
            "grnId": str(self.grn.id),
            "grnNo": self.grn.grn_number,
            "lotNo": f"LOT-FILTER-{self.uid}",
            "takas": [
                {"takaRoll": "1", "qty": 150},
                {"takaRoll": "2", "qty": 150},
            ],
        }
        self.api.post("/api/v1/inventory/lot-register/", lot_data, format="json")

        # Query by GRN
        grn_res = self.api.get(f"/api/v1/inventory/lot-register/?grn={self.grn.grn_number}")
        self.assertEqual(grn_res.status_code, 200)
        self.assertEqual(len(grn_res.data.get("records", [])), 2)

        # Query by PO
        po_res = self.api.get(f"/api/v1/inventory/lot-register/?po={self.po.id}")
        self.assertEqual(po_res.status_code, 200)
        self.assertEqual(len(po_res.data.get("records", [])), 2)

    def test_06_standalone_register_summary_cards(self):
        """Standalone lot register returns records and summary KPIs."""
        res = self.api.get("/api/v1/inventory/lot-register/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("records", res.data)
        summary = res.data.get("summaryCards", {})
        self.assertIn("totalLots", summary)
        self.assertIn("totalQuantity", summary)
