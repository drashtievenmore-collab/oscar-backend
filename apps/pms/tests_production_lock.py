"""Daily production entries are frozen once the instruction is verified/closed.

Run from ``oscar-backend``::

    python manage.py test apps.pms.tests_production_lock
"""
from datetime import date
from decimal import Decimal

from rest_framework.test import APIClient

from apps.core.exceptions import ValidationFailed
from apps.pms import production_services as services
from apps.pms.models import DailyProductionEntry

from .tests_production import ProductionTestBase


class DailyEntryLockTests(ProductionTestBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        from apps.accounts.authentication import build_tokens

        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.employee = self.make_employee()
        self.instruction = self.make_instruction(employee=self.employee)
        self.entry = services.record_daily_entry(
            client=self.client_obj,
            instruction=self.instruction,
            entry_date=date(2026, 10, 1),
            meters=Decimal("40"),
            entered_by_employee=self.employee,
            entered_by_user=self.user,
        )

    def _verify(self):
        services.verify_instruction(self.instruction, user=self.user, reason="checked")
        self.instruction.refresh_from_db()

    def test_create_rejected_after_verify(self):
        self._verify()
        with self.assertRaises(ValidationFailed):
            services.record_daily_entry(
                client=self.client_obj,
                instruction=self.instruction,
                entry_date=date(2026, 10, 2),
                meters=Decimal("10"),
                entered_by_employee=self.employee,
            )

    def test_update_allowed_while_open(self):
        res = self.api.patch(
            f"/api/v1/production/daily-entries/{self.entry.id}/", {"meters": "45"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.meters, Decimal("45"))

    def test_update_rejected_after_verify(self):
        self._verify()
        res = self.api.patch(
            f"/api/v1/production/daily-entries/{self.entry.id}/", {"meters": "45"}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.meters, Decimal("40"))

    def test_delete_rejected_after_verify(self):
        self._verify()
        res = self.api.delete(f"/api/v1/production/daily-entries/{self.entry.id}/")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertTrue(
            DailyProductionEntry.objects.filter(pk=self.entry.pk, deleted_at__isnull=True).exists()
        )

    def test_draft_instruction_rejects_entries(self):
        draft = self.make_instruction(employee=self.employee, approve=False)
        with self.assertRaises(ValidationFailed):
            services.record_daily_entry(
                client=self.client_obj,
                instruction=draft,
                entry_date=date(2026, 10, 2),
                meters=Decimal("10"),
                entered_by_employee=self.employee,
            )

    def test_status_not_writable_via_patch(self):
        res = self.api.patch(
            f"/api/v1/production/instructions/{self.instruction.id}/", {"status": "Closed"}, format="json"
        )
        self.instruction.refresh_from_db()
        self.assertNotEqual(self.instruction.status, "Closed", res.content)

    def test_verify_without_matching_item_creates_no_lot(self):
        self._verify()
        self.assertEqual(self.instruction.status, "PO Pending")
        self.assertIsNone(self.instruction.grey_lot_id)

    def test_vpi_api_removed(self):
        res = self.api.get("/api/v1/jobwork/process-instructions/")
        self.assertEqual(res.status_code, 404)


class FinalPoTests(DailyEntryLockTests):
    def _make_vendor_and_item(self):
        from apps.masters.models import Item, Party

        Party.objects.create(
            client=self.client_obj, code="VEND-T1", type="Vendor",
            name=self.instruction.agency_name,
        )
        Item.objects.create(
            client=self.client_obj, sku="GREY-60", name="Grey 60s", uom="Meter",
            tax_pct=Decimal("5"),
        )
        self.instruction.fabric = "GREY-60"
        self.instruction.agreed_rate = Decimal("12.50")
        self.instruction.save(update_fields=["fabric", "agreed_rate"])

    def test_generate_po_creates_pending_approval_po(self):
        self._make_vendor_and_item()
        self._verify()
        instruction = services.generate_final_job_work_po(self.instruction, user=self.user)
        self.assertEqual(instruction.status, "PO Created")
        po = instruction.final_po
        self.assertEqual(po.status, "Pending Approval")
        line = po.line_items.get()
        self.assertEqual(line.qty, Decimal("40"))
        self.assertEqual(line.rate, Decimal("12.50"))
        self.assertEqual(po.subtotal, Decimal("500.00"))

    def test_generate_po_without_vendor_party_fails(self):
        self._verify()
        with self.assertRaises(ValidationFailed):
            services.generate_final_job_work_po(self.instruction, user=self.user)
