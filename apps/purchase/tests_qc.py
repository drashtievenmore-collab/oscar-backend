"""Tests for Grey Fabric Quality Control (QC) within Purchase Order workflow.

Run with:
    python manage.py test apps.purchase.tests_qc --keepdb
"""
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.hrms.models import Employee
from apps.inventory.models import QCInspection
from apps.masters.models import FabricQuality, Item, Location, Party
from apps.pms.models import ProductionInstruction, DailyProductionEntry
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine


class PurchaseOrderQCTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="QC Test Client", slug="qc-test")
        self.user = User.objects.create_user(
            email="qcadmin@example.com",
            password="secretpassword",
            client=self.tenant,
            is_superuser=True,
            name="Quality Inspector",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.quality = FabricQuality.objects.create(client=self.tenant, name="Cotton 40x40", code="CT40")
        self.fabric = Item.objects.create(
            client=self.tenant,
            sku="GREY-CT40",
            name="Grey Cotton 40x40",
            uom="Mtr",
            item_kind="Standalone",
            fabric_quality=self.quality,
            cost_price=Decimal("45.00"),
        )
        self.vendor = Party.objects.create(
            client=self.tenant,
            name="Vendor A Weaving Mills",
            code="V-VENDORA",
            type="Vendor",
        )
        self.godown = Location.objects.create(
            client=self.tenant,
            name="Central Mill Godown",
            code="WH-CMG",
            type="Warehouse",
        )

        # Create PO
        self.po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number="PO-2026-0099",
            doc_date=date.today(),
            party=self.vendor,
            location=self.godown,
            status="In Production",
            purchase_type="Vendor A Production",
            subtotal=Decimal("225000.00"),
            taxable_value=Decimal("225000.00"),
            total=Decimal("225000.00"),
        )
        self.poi = PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=self.po,
            line_no=1,
            item=self.fabric,
            qty=Decimal("5000.00"),
            rate=Decimal("45.00"),
            amount=Decimal("225000.00"),
        )

        # Create Employee for instruction
        self.employee = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-001",
            name="QC Operator",
            joining_date=date(2026, 1, 5),
            status="Active",
        )

        # Create linked Production Instruction
        self.pi = ProductionInstruction.objects.create(
            client=self.tenant,
            instruction_number="PI-2026-0042",
            pi_date=date.today(),
            order_reference=self.po.po_number,
            agency_name=self.vendor.name,
            fabric=self.fabric.name,
            order_meter=Decimal("5000.00"),
            employee=self.employee,
            status="In Progress",
        )

        # Record daily production entries totaling 3000 Mtr produced
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=self.pi,
            entry_date=date.today() - timedelta(days=2),
            meters=Decimal("1800.00"),
            entered_by_employee=self.employee,
            entered_by_user=self.user,
        )
        DailyProductionEntry.objects.create(
            client=self.tenant,
            instruction=self.pi,
            entry_date=date.today() - timedelta(days=1),
            meters=Decimal("1200.00"),
            entered_by_employee=self.employee,
            entered_by_user=self.user,
        )

    def test_qc_endpoint_get_linked_details_and_summary(self):
        """GET /api/purchase-orders/<id>/qc-inspection/ returns accurate read-only details and metrics."""
        url = f"/api/v1/purchase/orders/{self.po.id}/qc-inspection/"
        res = self.api.get(url)
        self.assertEqual(res.status_code, 200)

        data = res.json()
        linked = data.get("linkedDetails", {})
        summary = data.get("summary", {})

        self.assertEqual(linked.get("poNumber"), "PO-2026-0099")
        self.assertEqual(linked.get("instructionNumber"), "PI-2026-0042")
        self.assertEqual(linked.get("vendorName"), "Vendor A Weaving Mills")
        self.assertEqual(Decimal(str(linked.get("poQuantityMtr"))), Decimal("5000.00"))
        self.assertEqual(Decimal(str(linked.get("totalProducedMtr"))), Decimal("3000.00"))
        self.assertEqual(Decimal(str(linked.get("pendingForQcMtr"))), Decimal("3000.00"))

        self.assertEqual(Decimal(str(summary.get("totalProducedMtr"))), Decimal("3000.00"))
        self.assertEqual(Decimal(str(summary.get("totalInspectedMtr"))), Decimal("0.00"))
        self.assertEqual(Decimal(str(summary.get("pendingForInspectionMtr"))), Decimal("3000.00"))
        self.assertEqual(Decimal(str(summary.get("completionPercentage"))), Decimal("0.00"))

        # Verify active inspectors list
        inspectors = data.get("inspectors", [])
        self.assertTrue(any(i["id"] == str(self.employee.id) for i in inspectors))

    def test_qc_submission_validations(self):
        """Validates future date, quantity reconciliation, and eligible over-inspection blocking."""
        url = f"/api/v1/purchase/orders/{self.po.id}/qc-inspection/"

        # 1. Future date rejected
        future_payload = {
            "inspection_date": str(date.today() + timedelta(days=2)),
            "inspected_quantity": "500",
            "accepted_quantity": "500",
            "inspector": str(self.employee.id),
            "status": "Submitted",
        }
        res_future = self.api.post(url, future_payload, format="json")
        self.assertEqual(res_future.status_code, 400)
        self.assertIn("Future inspection dates are not permitted", res_future.json().get("message", ""))

        # 2. Reconciliation failure (Accepted 400 + Rejected 50 != Inspected 500)
        mismatch_payload = {
            "inspection_date": str(date.today()),
            "inspected_quantity": "500",
            "accepted_quantity": "400",
            "rejected_quantity": "50",
            "reprocess_quantity": "0",
            "hold_quantity": "0",
            "inspector": str(self.employee.id),
            "status": "Submitted",
        }
        res_mismatch = self.api.post(url, mismatch_payload, format="json")
        self.assertEqual(res_mismatch.status_code, 400)
        self.assertIn("Quantity reconciliation mismatch", res_mismatch.json().get("message", ""))

        # 3. Over-inspection rejected (Produced till date is 3000, attempting 3500)
        over_payload = {
            "inspection_date": str(date.today()),
            "inspected_quantity": "3500",
            "accepted_quantity": "3500",
            "inspector": str(self.employee.id),
            "status": "Submitted",
        }
        res_over = self.api.post(url, over_payload, format="json")
        self.assertEqual(res_over.status_code, 400)
        self.assertIn("exceeds pending produced quantity eligible for QC", res_over.json().get("message", ""))

    def test_qc_successful_submission_and_summary_update(self):
        """Successfully submits a QC entry and verifies summary recalculation and previous entries."""
        url = f"/api/v1/purchase/orders/{self.po.id}/qc-inspection/"

        # Inspect 1500 Mtr with breakdown: 1400 Accepted, 50 Rejected, 30 Reprocess, 20 Hold
        valid_payload = {
            "inspection_date": str(date.today()),
            "inspected_quantity": "1500.00",
            "accepted_quantity": "1400.00",
            "rejected_quantity": "50.00",
            "reprocess_quantity": "30.00",
            "hold_quantity": "20.00",
            "inspector": str(self.employee.id),
            "quality_grade": "A Grade",
            "gsm": "140",
            "width": "58",
            "defects_found": "Minor weaving slubs in initial 20m",
            "remarks": "Overall good grey fabric quality",
            "status": "Submitted",
        }
        res = self.api.post(url, valid_payload, format="json")
        self.assertEqual(res.status_code, 201)

        data = res.json()
        summary = data.get("summary", {})
        self.assertEqual(Decimal(str(summary.get("totalProducedMtr"))), Decimal("3000.00"))
        self.assertEqual(Decimal(str(summary.get("totalInspectedMtr"))), Decimal("1500.00"))
        self.assertEqual(Decimal(str(summary.get("totalAcceptedMtr"))), Decimal("1400.00"))
        self.assertEqual(Decimal(str(summary.get("totalRejectedMtr"))), Decimal("50.00"))
        self.assertEqual(Decimal(str(summary.get("totalReprocessMtr"))), Decimal("30.00"))
        self.assertEqual(Decimal(str(summary.get("totalHoldMtr"))), Decimal("20.00"))
        self.assertEqual(Decimal(str(summary.get("pendingForInspectionMtr"))), Decimal("1500.00"))
        self.assertEqual(Decimal(str(summary.get("completionPercentage"))), Decimal("50.00"))

        # Verify previous entries list
        entries = data.get("entries", [])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(Decimal(str(entry.get("inspectedQuantity"))), Decimal("1500.00"))
        self.assertEqual(entry.get("qualityGrade"), "A Grade")
        self.assertEqual(entry.get("inspectorName"), "QC Operator")
        self.assertTrue(entry.get("inspectionNumber", "").startswith("QC-"))

    def test_qc_draft_and_deletion(self):
        """Allows saving a draft and deleting it safely without affecting final inspection balances."""
        url = f"/api/v1/purchase/orders/{self.po.id}/qc-inspection/"

        draft_payload = {
            "inspection_date": str(date.today()),
            "inspected_quantity": "800.00",
            "accepted_quantity": "800.00",
            "inspector": str(self.employee.id),
            "status": "Draft",
        }
        res_draft = self.api.post(url, draft_payload, format="json")
        self.assertEqual(res_draft.status_code, 201)

        draft_id = res_draft.json().get("entry", {}).get("id")
        self.assertIsNotNone(draft_id)

        # Delete the draft entry
        res_del = self.api.delete(f"{url}?entry_id={draft_id}")
        self.assertEqual(res_del.status_code, 200)

        # Check that it was deleted
        self.assertFalse(QCInspection.objects.filter(id=draft_id, deleted_at__isnull=True).exists())
