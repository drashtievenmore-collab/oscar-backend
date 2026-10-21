import uuid
from decimal import Decimal
from datetime import date
from django.test import TestCase
from rest_framework.test import APIClient
from apps.accounts.models import Client, User
from apps.accounts.authentication import build_tokens
from apps.masters.models import Item, ItemCategory, Location, Party, Unit
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine, GoodsReceipt
from apps.inventory.models import LotTaka, StockBalance, StockMovement
from apps.inventory import qc as qc_service

class StrictQuantityAndDuplicateValidationTests(TestCase):
    def setUp(self):
        self.tenant, _ = Client.objects.get_or_create(
            slug=f"val-tenant-{uuid.uuid4().hex[:6]}",
            defaults={"name": "Validation Tenant"},
        )
        self.user = User.objects.create_user(
            email=f"val-{uuid.uuid4().hex[:6]}@example.com",
            password="pass",
            client=self.tenant,
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.uom = Unit.objects.create(client=self.tenant, code="Mtr", label="Mtr", is_base_unit=True)
        self.cat = ItemCategory.objects.create(client=self.tenant, name="Grey Fabric")
        self.item = Item.objects.create(
            client=self.tenant,
            sku=f"GREY-COT-{uuid.uuid4().hex[:4]}",
            name="Grey Cotton Sheeting",
            category=self.cat,
            tracking_type="TAKA",
            tax_pct=Decimal("5.00"),
            uom="Mtr",
            cost_price=Decimal("80.00"),
        )
        self.godown = Location.objects.create(
            client=self.tenant,
            code="WH-MAIN",
            name="Main Warehouse Godown",
            type="Godown",
            is_active=True,
        )
        self.qc_loc = Location.objects.create(
            client=self.tenant,
            code="SYS-QC-HOLD",
            name="QC Hold Location",
            type="Godown",
            is_active=True,
        )
        self.reject_loc = Location.objects.create(
            client=self.tenant,
            code="SYS-REJECT",
            name="Rejected Stock",
            type="Godown",
            is_active=True,
        )
        self.supplier = Party.objects.create(
            client=self.tenant,
            name="Premier Weaving Mills",
            type="Vendor",
            credit_limit=Decimal("500000.00"),
        )

    def _create_approved_po(self, qty=1000):
        po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number=f"PO-{uuid.uuid4().hex[:6]}",
            party=self.supplier,
            doc_date=date(2026, 10, 20),
            purchase_type="Grey Stock Purchase",
            status="Approved",
            created_by=self.user,
        )
        po_line = PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=po,
            item=self.item,
            qty=Decimal(str(qty)),
            rate=Decimal("80.00"),
            tax_pct=Decimal("5.00"),
            uom="Mtr",
            uom_factor=Decimal("1.00"),
        )
        return po, po_line

    def test_over_allocation_prevented_on_grn(self):
        po, po_line = self._create_approved_po(qty=500)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-OVAL-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 300,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201, grn_res.content)
        grn_id = grn_res.json()["id"]

        over_res = self.api.post(
            "/api/v1/inventory/lot-register/",
            {
                "grnId": grn_id,
                "itemId": str(self.item.id),
                "lotNo": "LOT-OVAL-1",
                "takas": [
                    {"takaRoll": "1", "qty": 180},
                    {"takaRoll": "2", "qty": 150},
                ],
            },
            format="json",
        )
        self.assertEqual(over_res.status_code, 400)
        self.assertIn("cannot exceed", str(over_res.content))

    def test_duplicate_taka_ids_prevented(self):
        po, po_line = self._create_approved_po(qty=500)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-DUP-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 200,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201)
        grn_id = grn_res.json()["id"]

        dup_res = self.api.post(
            "/api/v1/inventory/lot-register/",
            {
                "grnId": grn_id,
                "itemId": str(self.item.id),
                "lotNo": "LOT-DUP-1",
                "takas": [
                    {"takaRoll": "1", "qty": 100},
                    {"takaRoll": "1", "qty": 100},
                ],
            },
            format="json",
        )
        self.assertEqual(dup_res.status_code, 400)
        self.assertIn("Duplicate", str(dup_res.content))

    def test_mismatched_grn_totals_validation(self):
        po, po_line = self._create_approved_po(qty=400)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-MIS-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 400,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201)
        grn_id = grn_res.json()["id"]

        mismatch_res = self.api.post(
            "/api/v1/inventory/qc/complete/",
            {
                "receiptId": grn_id,
                "goodQty": 350,
                "rejectQty": 0,
            },
            format="json",
        )
        self.assertEqual(mismatch_res.status_code, 400)
        self.assertIn("Expected a total of 400", str(mismatch_res.content))

    def test_partial_qc_rejection_reconciles_and_posts_accepted_stock_only(self):
        po, po_line = self._create_approved_po(qty=500)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-PART-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 270,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201)
        grn_id = grn_res.json()["id"]

        lot_res = self.api.post(
            "/api/v1/inventory/lot-register/",
            {
                "grnId": grn_id,
                "itemId": str(self.item.id),
                "lotNo": "LOT-PART-1",
                "takas": [
                    {"takaRoll": "1", "qty": 90},
                    {"takaRoll": "2", "qty": 90},
                    {"takaRoll": "3", "qty": 90},
                ],
            },
            format="json",
        )
        self.assertIn(lot_res.status_code, (200, 201), lot_res.content)

        takas = list(LotTaka.objects.filter(source_document_id=grn_id, deleted_at__isnull=True).order_by("taka_number"))
        self.assertEqual(len(takas), 3)

        qc_res = self.api.post(
            "/api/v1/inventory/qc/complete/",
            {
                "receiptId": grn_id,
                "takas": [
                    {"id": str(takas[0].id), "disposition": "Passed"},
                    {"id": str(takas[1].id), "disposition": "Passed"},
                    {"id": str(takas[2].id), "disposition": "Rejected"},
                ],
            },
            format="json",
        )
        self.assertEqual(qc_res.status_code, 200, qc_res.content)
        data = qc_res.json()

        self.assertEqual(data["status"], "Partially Approved")
        self.assertEqual(data["goodQty"], 180.0)
        self.assertEqual(data["rejectQty"], 90.0)
        self.assertEqual(data["postedStock"], 180.0)

        takas[0].refresh_from_db()
        takas[1].refresh_from_db()
        takas[2].refresh_from_db()

        self.assertEqual(takas[0].qc_status, "Passed")
        self.assertEqual(takas[0].status, "Active")
        self.assertEqual(str(takas[0].location_id), str(self.godown.id))
        self.assertEqual(takas[0].available_quantity, Decimal("90.0000"))

        self.assertEqual(takas[1].qc_status, "Passed")
        self.assertEqual(takas[1].status, "Active")
        self.assertEqual(str(takas[1].location_id), str(self.godown.id))
        self.assertEqual(takas[1].available_quantity, Decimal("90.0000"))

        self.assertEqual(takas[2].qc_status, "Rejected")
        self.assertEqual(takas[2].status, "Reject")
        self.assertEqual(str(takas[2].location_id), str(self.reject_loc.id))
        self.assertEqual(takas[2].available_quantity, Decimal("90.0000"))

        godown_balance = StockBalance.objects.filter(
            client=self.tenant, item=self.item, location=self.godown
        ).first()
        self.assertIsNotNone(godown_balance)
        self.assertEqual(godown_balance.on_hand, Decimal("180.0000"))

    def test_duplicate_qc_posting_prevented(self):
        po, po_line = self._create_approved_po(qty=300)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-DUPQC-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 180,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201)
        grn_id = grn_res.json()["id"]

        first_qc = self.api.post(
            "/api/v1/inventory/qc/complete/",
            {
                "receiptId": grn_id,
                "goodQty": 180,
                "rejectQty": 0,
            },
            format="json",
        )
        self.assertEqual(first_qc.status_code, 200)

        second_qc = self.api.post(
            "/api/v1/inventory/qc/complete/",
            {
                "receiptId": grn_id,
                "goodQty": 180,
                "rejectQty": 0,
            },
            format="json",
        )
        self.assertEqual(second_qc.status_code, 409)
        self.assertEqual(second_qc.json().get("code"), "QC_ALREADY_DONE")

    def test_successful_valid_transaction(self):
        po, po_line = self._create_approved_po(qty=360)
        grn_res = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-SUCC-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 360,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn_res.status_code, 201)
        grn_id = grn_res.json()["id"]

        lot_res = self.api.post(
            "/api/v1/inventory/lot-register/",
            {
                "grnId": grn_id,
                "itemId": str(self.item.id),
                "lotNo": "LOT-SUCC-1",
                "takas": [
                    {"takaRoll": "1", "qty": 90},
                    {"takaRoll": "2", "qty": 90},
                    {"takaRoll": "3", "qty": 90},
                    {"takaRoll": "4", "qty": 90},
                ],
            },
            format="json",
        )
        self.assertIn(lot_res.status_code, (200, 201), lot_res.content)

        qc_res = self.api.post(
            "/api/v1/inventory/qc/complete/",
            {
                "receiptId": grn_id,
                "goodQty": 360,
                "rejectQty": 0,
            },
            format="json",
        )
        self.assertEqual(qc_res.status_code, 200)
        self.assertEqual(qc_res.json()["status"], "Approved")

        receipt = GoodsReceipt.objects.get(pk=grn_id)
        self.assertEqual(receipt.qc_status, "Approved")

        available_takas = LotTaka.objects.filter(source_document_id=grn_id, status="Active", qc_status="Passed")
        self.assertEqual(available_takas.count(), 4)

    def test_over_receipt_against_po_prevented(self):
        po, po_line = self._create_approved_po(qty=500)
        grn1 = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-POREC-1-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 350,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn1.status_code, 201)

        grn2 = self.api.post(
            "/api/v1/purchase/receipts/",
            {
                "vendorId": str(self.supplier.id),
                "purchaseOrderId": str(po.id),
                "locationId": str(self.godown.id),
                "receiptDate": "2026-10-20",
                "supplierChallanNo": f"CH-POREC-2-{uuid.uuid4().hex[:4]}",
                "lines": [
                    {
                        "itemId": str(self.item.id),
                        "poLineId": str(po_line.id),
                        "receivedQty": 200,
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(grn2.status_code, 400)
        self.assertIn("exceeds remaining unreceived PO quantity", str(grn2.content))
