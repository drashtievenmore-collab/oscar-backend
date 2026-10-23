"""One test per alert check in apps/core/alert_services.py (PDF §19).

Each test builds the smallest data that should raise the alert, runs that
single check, and asserts an AlertIncident exists. A few tests also cover the
cases that used to misfire (partial inward, verified instruction, thresholds).
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core import alert_services as alerts
from apps.core.models import AlertIncident, AlertSetting
from apps.inventory.models import LotTaka, QCInspection, QCInspectionLine, Sample, StockBalance
from apps.jobwork.models import JobWorkOrder, JWOInward, JWOOutward
from apps.masters.models import Agency, Item, Location, Party
from apps.pms.models import ProductionInstruction
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine, VendorBill, VendorBillLine
from apps.sales.models import SalesInvoice, SalesOrder, SalesOrderLine


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class AlertChecksTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("alerts"), name="Alerts Tenant")
        self.user = User.objects.create_user(
            email=f"{uid('admin')}@example.com",
            password="pass",
            client=self.tenant,
            is_superuser=True,
            name="Alerts Admin",
        )
        alerts.ensure_alert_settings(self.tenant)
        self.today = timezone.localdate()

        self.customer = Party.objects.create(
            client=self.tenant, name="Radhe Textiles", code=uid("CUST"), type="Customer"
        )
        self.processor = Party.objects.create(
            client=self.tenant, name="Surat Dyeing Mills", code=uid("PROC"), type="Processor"
        )
        self.vendor = Party.objects.create(
            client=self.tenant, name="Grey Supplier", code=uid("VEND"), type="Vendor"
        )
        self.location = Location.objects.create(
            client=self.tenant, code=uid("WH"), name="Main Godown", type="Godown", is_active=True
        )
        self.item = Item.objects.create(
            client=self.tenant, sku=uid("FAB"), name="Cotton 60x60", item_kind="Fabric", uom="Mtr"
        )

    # -- helpers ----------------------------------------------------------
    def run_check(self, check):
        return check(self.tenant.id, self.today)

    def incidents(self, key):
        return AlertIncident.objects.filter(client_id=self.tenant.id, alert_key=key)

    def make_instruction(self, **kwargs):
        from apps.hrms.models import Employee

        if not hasattr(self, "employee"):
            self.employee = Employee.objects.create(
                client=self.tenant,
                employee_code=uid("EMP"),
                name="Ramesh Kumar",
                email=f"{uid('emp')}@example.com",
                joining_date="2026-01-01",
                employment_type="Regular",
                status="Active",
            )
        defaults = dict(
            employee=self.employee,
            client=self.tenant,
            instruction_number=uid("PROD"),
            agency_name="Vendor A Agency",
            agency=Agency.objects.create(client=self.tenant, name="Vendor A Agency", code=uid("AG")),
            order_reference=uid("PO"),
            order_meter=Decimal("25000"),
            status="Running",
        )
        defaults.update(kwargs)
        return ProductionInstruction.objects.create(**defaults)

    def make_jwo(self, **kwargs):
        defaults = dict(
            client=self.tenant,
            jwo_no=uid("JWO"),
            vendor=self.processor,
            vendor_name=self.processor.name,
            process="Dyeing",
            order_date=self.today,
            planned_qty=Decimal("100"),
            expected_loss_pct=Decimal("3"),
            status="Sent",
        )
        defaults.update(kwargs)
        return JobWorkOrder.objects.create(**defaults)

    def send(self, jwo, qty):
        return JWOOutward.objects.create(
            client=self.tenant, job_work_order=jwo, no=uid("OUT"), date=self.today, qty=Decimal(qty)
        )

    def receive(self, jwo, qty):
        return JWOInward.objects.create(
            client=self.tenant, job_work_order=jwo, no=uid("IN"), date=self.today, qty=Decimal(qty)
        )

    # -- 1 ----------------------------------------------------------------
    def test_production_update_missing(self):
        pi = self.make_instruction(status="Running")
        self.run_check(alerts.check_production_update_missing)
        self.assertTrue(self.incidents("production_update_missing").filter(entity_id=str(pi.id)).exists())

    # -- 2 ----------------------------------------------------------------
    def test_job_delayed_raises_for_running_instruction_and_jwo(self):
        pi = self.make_instruction(expected_completion_date=self.today - timedelta(days=3))
        jwo = self.make_jwo(expected_completion=self.today - timedelta(days=10), status="In-Process")
        self.run_check(alerts.check_job_delayed)
        self.assertTrue(self.incidents("job_delayed").filter(entity_id=str(pi.id)).exists())
        incident = self.incidents("job_delayed").get(entity_id=str(jwo.id))
        self.assertEqual(incident.severity, "Critical")

    def test_job_delayed_ignores_finished_instructions(self):
        for status in ("Verified", "PO Created", "Closed", "Completed"):
            self.make_instruction(status=status, expected_completion_date=self.today - timedelta(days=5))
        self.run_check(alerts.check_job_delayed)
        self.assertFalse(self.incidents("job_delayed").exists())

    # -- 3 ----------------------------------------------------------------
    def test_agency_pending_aging_uses_threshold_setting(self):
        jwo = self.make_jwo(order_date=self.today - timedelta(days=8), status="Sent")
        self.run_check(alerts.check_agency_pending_aging)
        self.assertFalse(self.incidents("agency_pending_aging").exists())  # default is 15 days

        AlertSetting.objects.filter(client=self.tenant, alert_key="agency_pending_aging").update(threshold_days=7)
        self.run_check(alerts.check_agency_pending_aging)
        self.assertTrue(self.incidents("agency_pending_aging").filter(entity_id=str(jwo.id)).exists())

    # -- 4 ----------------------------------------------------------------
    def test_loss_tolerance_ignores_partial_inward(self):
        # PDF §07 example: 100 sent, 50 back -> 50 still pending, not a loss.
        jwo = self.make_jwo(status="Partially Received")
        self.send(jwo, "100")
        self.receive(jwo, "50")
        self.run_check(alerts.check_loss_tolerance_exceeded)
        self.assertFalse(self.incidents("loss_tolerance_exceeded").exists())

    def test_loss_tolerance_raises_when_fully_returned(self):
        jwo = self.make_jwo(status="Received")
        self.send(jwo, "100")
        self.receive(jwo, "50")
        self.receive(jwo, "40")  # 10% loss against a 3% tolerance
        self.run_check(alerts.check_loss_tolerance_exceeded)
        incident = self.incidents("loss_tolerance_exceeded").get(entity_id=str(jwo.id))
        self.assertEqual(incident.metadata["lossPct"], "10.00")

    def test_loss_within_tolerance_is_quiet(self):
        jwo = self.make_jwo(status="Received")
        self.send(jwo, "100")
        self.receive(jwo, "98")
        self.run_check(alerts.check_loss_tolerance_exceeded)
        self.assertFalse(self.incidents("loss_tolerance_exceeded").exists())

    # -- 5 ----------------------------------------------------------------
    def test_qc_rejection_high_from_lines(self):
        qc = QCInspection.objects.create(
            client=self.tenant, inspection_number=uid("QC"), inspection_date=self.today
        )
        for n, disposition in enumerate(["Pass", "Pass", "Pass", "Reject", "Reprocess"], start=1):
            QCInspectionLine.objects.create(
                client=self.tenant, inspection=qc, taka_number=str(n), disposition=disposition
            )
        self.run_check(alerts.check_qc_rejection_high)
        self.assertTrue(self.incidents("qc_rejection_high").filter(entity_id=str(qc.id)).exists())

    def test_qc_rejection_high_from_quantities(self):
        qc = QCInspection.objects.create(
            client=self.tenant,
            inspection_number=uid("QC"),
            inspection_date=self.today,
            total_inspected_qty=Decimal("1000"),
            rejected_qty=Decimal("80"),
        )
        self.run_check(alerts.check_qc_rejection_high)
        self.assertTrue(self.incidents("qc_rejection_high").filter(entity_id=str(qc.id)).exists())

    def add_entry(self, pi, meters, reject, rework, on=None):
        from apps.pms.models import DailyProductionEntry

        return DailyProductionEntry.objects.create(
            client=self.tenant, instruction=pi, entry_date=on or self.today,
            meters=Decimal(meters), reject_meter=Decimal(reject), reprocess_meter=Decimal(rework),
            entered_by_employee=self.employee, status="Submitted",
        )

    def test_agency_reject_and_reprocess_above_threshold(self):
        # The screenshot case: 500 produced, 45 rejected, 78 reprocess = 24.6% > 5%.
        pi = self.make_instruction(status="Partially Completed")
        self.add_entry(pi, "500", "45", "78")
        self.run_check(alerts.check_qc_rejection_high)
        incident = self.incidents("qc_rejection_high").get(entity_id=str(pi.id))
        self.assertIn("24.60%", incident.title)

    def test_agency_dashboard_splits_quality(self):
        from apps.pms.production_reports import agency_production_view

        pi = self.make_instruction(status="Partially Completed")
        self.add_entry(pi, "500", "45", "78")
        row = next(r for r in agency_production_view(self.tenant.id, day=self.today) if r["agency"] == pi.agency_name)
        self.assertEqual(row["acceptedMeters"], 377.0)
        self.assertEqual(row["rejectedMeters"], 45.0)
        self.assertEqual(row["reprocessMeters"], 78.0)
        self.assertEqual(row["rejectPct"], 9.0)
        self.assertEqual(row["reprocessPct"], 15.6)

    # -- 6 ----------------------------------------------------------------
    def test_negative_stock(self):
        balance = StockBalance.objects.create(
            client=self.tenant, item=self.item, location=self.location, on_hand=Decimal("-5")
        )
        self.run_check(alerts.check_negative_stock)
        incident = self.incidents("negative_stock").get(entity_id=str(balance.id))
        self.assertIn("Mtr", incident.message)

    # -- 7 ----------------------------------------------------------------
    def test_credit_overdue_invoice(self):
        invoice = SalesInvoice.objects.create(
            client=self.tenant,
            invoice_number=uid("INV"),
            party=self.customer,
            doc_date=self.today - timedelta(days=60),
            due_date=self.today - timedelta(days=40),
            status="Unpaid",
            subtotal=Decimal("25000"),
            total=Decimal("25000"),
        )
        self.run_check(alerts.check_credit_limit_exceeded)
        incident = self.incidents("credit_limit_exceeded").get(entity_id=str(invoice.id))
        self.assertEqual(incident.severity, "Critical")

    def test_credit_limit_exposure(self):
        self.customer.credit_limit = Decimal("50000")
        self.customer.balance = Decimal("45000")
        self.customer.save()
        SalesOrder.objects.create(
            client=self.tenant,
            order_number=uid("SO"),
            party=self.customer,
            stage="Confirmed",
            doc_date=self.today,
            subtotal=Decimal("10000"),
            total=Decimal("10000"),
        )
        self.run_check(alerts.check_credit_limit_exceeded)
        incident = self.incidents("credit_limit_exceeded").get(entity_id=str(self.customer.id))
        self.assertEqual(incident.metadata["exposure"], "55000.00")

    # -- 8 ----------------------------------------------------------------
    def test_below_cost_sale(self):
        order = SalesOrder.objects.create(
            client=self.tenant, order_number=uid("SO"), party=self.customer, stage="Confirmed", doc_date=self.today
        )
        SalesOrderLine.objects.create(
            client=self.tenant,
            sales_order=order,
            item=self.item,
            line_no=1,
            qty=Decimal("100"),
            rate=Decimal("50"),
            landed_cost=Decimal("64.74"),
            margin_pct=Decimal("-29.48"),
            below_cost=True,
        )
        self.run_check(alerts.check_below_cost_sale)
        incident = self.incidents("below_cost_sale").get(entity_id=str(order.id))
        self.assertEqual(incident.severity, "Critical")
        self.assertIn("₹50", incident.message)

    # -- 9 ----------------------------------------------------------------
    def test_large_discount_uses_threshold_setting(self):
        invoice = SalesInvoice.objects.create(
            client=self.tenant,
            invoice_number=uid("INV"),
            party=self.customer,
            doc_date=self.today,
            due_date=self.today + timedelta(days=30),
            status="Unpaid",
            subtotal=Decimal("100000"),
            total_discount=Decimal("4000"),  # 4%: above the default 3% setting
            total=Decimal("96000"),
        )
        self.run_check(alerts.check_large_discount)
        self.assertTrue(self.incidents("large_discount").filter(entity_id=str(invoice.id)).exists())

    # -- 10 ---------------------------------------------------------------
    def test_sample_overdue(self):
        sample = Sample.objects.create(
            client=self.tenant,
            sample_number=uid("SMP"),
            sample_date=self.today - timedelta(days=20),
            party=self.customer,
            quantity=Decimal("2"),
            is_returnable=True,
            due_date=self.today - timedelta(days=5),
            status="Issued",
        )
        self.run_check(alerts.check_sample_overdue)
        self.assertTrue(self.incidents("sample_overdue").filter(entity_id=str(sample.id)).exists())
        sample.refresh_from_db()
        self.assertEqual(sample.status, "Overdue")

    # -- 11 ---------------------------------------------------------------
    def test_vendor_bill_mismatch(self):
        po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number=uid("PO"),
            party=self.vendor,
            doc_date=self.today,
            status="Approved",
            created_by=self.user,
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=po,
            line_no=1,
            item=self.item,
            qty=Decimal("1000"),
            rate=Decimal("45"),
            tax_pct=Decimal("5"),
            uom="Mtr",
        )
        bill = VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number=uid("VB"),
            bill_date=self.today,
            party=self.vendor,
            party_name=self.vendor.name,
            purchase_order=po,
            gst_pct=Decimal("5"),
            subtotal=Decimal("50000"),
            gst_amount=Decimal("2500"),
            total=Decimal("52500"),
            status="Pending",
        )
        VendorBillLine.objects.create(
            client=self.tenant,
            vendor_bill=bill,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000"),
            rate=Decimal("50"),  # PO rate is 45
            amount=Decimal("50000"),
            line_total=Decimal("52500"),
        )
        self.run_check(alerts.check_vendor_bill_mismatch)
        incident = self.incidents("vendor_bill_mismatch").get(entity_id=str(bill.id))
        self.assertEqual(incident.entity_number, bill.vendor_bill_number)
        self.assertEqual(incident.severity, "Critical")  # ₹5,000 diff > ₹1,000 setting

    # -- 12 ---------------------------------------------------------------
    def test_slow_moving_stock(self):
        taka = LotTaka.objects.create(
            client=self.tenant,
            taka_id=uid("TAK"),
            lot_number=uid("LOT"),
            taka_number="1",
            item=self.item,
            location=self.location,
            received_date=self.today - timedelta(days=120),
            received_quantity=Decimal("100"),
            available_quantity=Decimal("100"),
            status="Finished",
            uom="Mtr",
        )
        self.run_check(alerts.check_slow_moving_stock)
        self.assertTrue(self.incidents("slow_moving_stock").filter(entity_id=str(taka.id)).exists())

    # -- 13 ---------------------------------------------------------------
    def test_combined_grey_stock_low(self):
        from apps.inventory import services as stock

        stock.set_combined_grey_stock_setting(
            client_id=self.tenant.id, threshold=Decimal("10000"), is_enabled=True, user=self.user
        )
        self.run_check(alerts.check_combined_grey_stock_low)
        self.assertTrue(self.incidents("combined_grey_stock_low").exists())

    # -- runner -----------------------------------------------------------
    def test_runner_logs_and_continues_when_a_check_fails(self):
        def broken(client_id, as_of):
            raise RuntimeError("boom")

        self.make_instruction(status="Running")
        original = alerts.ALERT_CHECKS
        alerts.ALERT_CHECKS = [("broken", broken)] + list(original)
        try:
            errors = []
            with self.assertLogs("apps.core.alert_services", level="ERROR"):
                alerts.run_all_alert_checks(self.tenant.id, as_of=self.today, errors=errors)
        finally:
            alerts.ALERT_CHECKS = original
        self.assertEqual(errors, [{"check": "broken", "error": "RuntimeError: boom"}])
        self.assertTrue(self.incidents("production_update_missing").exists())

    def test_disabled_setting_suppresses_incident(self):
        AlertSetting.objects.filter(client=self.tenant, alert_key="production_update_missing").update(is_enabled=False)
        self.make_instruction(status="Running")
        self.run_check(alerts.check_production_update_missing)
        self.assertFalse(self.incidents("production_update_missing").exists())


class AlertRunEndpointTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("alerts-api"), name="Alerts API Tenant")
        self.admin = User.objects.create_user(
            email=f"{uid('admin')}@example.com", password="pass", client=self.tenant, is_superuser=True
        )
        self.plain = User.objects.create_user(
            email=f"{uid('user')}@example.com", password="pass", client=self.tenant
        )

    def api_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def test_admin_can_run_alerts(self):
        resp = self.api_for(self.admin).post("/api/v1/alerts/incidents/run/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["errors"], [])
        self.assertIn("incidentCount", resp.data)

    def test_user_without_manage_alerts_is_refused(self):
        resp = self.api_for(self.plain).post("/api/v1/alerts/incidents/run/", {}, format="json")
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_bad_as_of_is_rejected(self):
        resp = self.api_for(self.admin).post(
            "/api/v1/alerts/incidents/run/", {"asOf": "22-10-2026"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
