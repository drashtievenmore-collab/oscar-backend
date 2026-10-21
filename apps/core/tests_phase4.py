"""Phase 4 Control, Alerts, MIS, E-Invoicing, and ITC-04 tests."""
from decimal import Decimal
from datetime import date, timedelta
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User, Role
from apps.core.models import ApprovalRule, ApprovalRequest, AlertSetting, AlertIncident
from apps.core.approval_services import create_approval_request, approve_request, reject_request
from apps.masters.models import Party, FabricQuality, Shade, Item, ItemCategory, Location
from apps.sales.models import SalesInvoice, SalesInvoiceLine, DeliveryChallan
from apps.jobwork.models import JobWorkOrder, JWOOutward, JWOInward
from apps.jobwork.itc04_reports import get_itc04_report_data, generate_itc04_csv


class Phase4ControlAndMISReportingTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="phase4-tenant", defaults={"name": "Phase 4 Tenant"}
        )
        self.user = User.objects.create_user(
            email="phase4-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="Phase4 Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.party = Party.objects.create(
            client=self.client_obj,
            name="Radhe Krishna Textiles",
            code="CUST-RKT",
            type="Customer",
            gstin="24AAACR1234A1Z5",
            place_of_supply="24-Gujarat",
            credit_limit=Decimal("50000.00"),
            credit_days=30,
        )
        self.vendor = Party.objects.create(
            client=self.client_obj,
            name="Surat Dyeing Mills",
            code="VEND-SDM",
            type="Processor",
            gstin="24AAACS5678B1Z6",
            place_of_supply="24-Gujarat",
        )
        self.location = Location.objects.create(
            client=self.client_obj,
            code="WH-PHASE4",
            name="Phase 4 Warehouse",
            type="Godown",
            is_active=True,
        )

    def test_approval_engine_flow(self):
        """Verify ApprovalRule evaluation, pending creation, approve and reject."""
        rule = ApprovalRule.objects.create(
            client=self.client_obj,
            document_type="discount_limit",
            name="Discount above 5%",
            condition_type="threshold_percent",
            threshold_value=Decimal("5.0"),
            approver_role_code="sales_manager",
            is_active=True,
        )

        # 1. Trigger approval request
        req = create_approval_request(
            client_id=self.client_obj.id,
            document_type="discount_limit",
            document_id="SO-1001",
            document_number="SO-1001",
            variance_pct=Decimal("8.5"),
            requested_by=self.user,
            reason="Bulk deal special discount requested.",
        )
        self.assertIsNotNone(req)
        self.assertEqual(req.status, "Pending")
        self.assertEqual(req.rule, rule)

        # 2. Query My Approvals Inbox via API
        resp = self.api.get("/api/v1/approvals/requests/")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.data.get("results", [])), 1)

        # 3. Approve via service
        approved_req = approve_request(req, approver=self.user, comments="Discount approved for volume.")
        self.assertEqual(approved_req.status, "Approved")
        self.assertEqual(approved_req.decided_by, self.user)

        # 4. Another request rejected
        req2 = create_approval_request(
            client_id=self.client_obj.id,
            document_type="discount_limit",
            document_id="SO-1002",
            document_number="SO-1002",
            variance_pct=Decimal("12.0"),
            requested_by=self.user,
            reason="Unapproved excessive discount.",
        )
        rejected_req = reject_request(req2, approver=self.user, comments="Too high, reject.")
        self.assertEqual(rejected_req.status, "Rejected")

    def test_alert_runner_and_incidents(self):
        """Verify alert runner command checks thresholds and creates AlertIncident records."""
        # Create an alert setting
        setting = AlertSetting.objects.create(
            client=self.client_obj,
            alert_key="job_delayed",
            name="Delayed JWO Alert",
            threshold_days=5,
            is_enabled=True,
        )

        # Create delayed JWO
        past_date = date.today() - timedelta(days=15)
        jwo = JobWorkOrder.objects.create(
            client=self.client_obj,
            jwo_no="JWO-ALERT-01",
            vendor=self.vendor,
            vendor_name=self.vendor.name,
            process="Dyeing",
            order_date=past_date,
            expected_completion=past_date + timedelta(days=2),
            status="In-Process",
            planned_qty=Decimal("500.00"),
        )

        # Run alerts management command for this tenant
        call_command("run_alerts", tenant=self.client_obj.slug)

        # Verify incident generated
        incident = AlertIncident.objects.filter(
            client_id=self.client_obj.id
        ).first()
        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, "Active")

        # Test badge API
        badge_resp = self.api.get("/api/v1/alerts/incidents/badge/")
        self.assertEqual(badge_resp.status_code, 200)
        self.assertGreaterEqual(badge_resp.data.get("activeCount", 0), 1)

    def test_einvoice_and_eway_bill_generation(self):
        """Verify IRN & E-Way Bill generation on sales invoice and delivery challan."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.party,
            party_name=self.party.name,
            party_gstin=self.party.gstin,
            place_of_supply=self.party.place_of_supply,
            doc_date=date.today(),
            invoice_number="INV-2026-001",
            status="Posted",
            location=self.location,
            subtotal=Decimal("125000.00"),
            total_tax=Decimal("6250.00"),
            total=Decimal("131250.00"),
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            line_no=1,
            item_name="Cotton Satin Printed",
            sku="CSP-001",
            hsn_code="5208",
            qty=Decimal("1000.00"),
            rate=Decimal("125.00"),
            amount=Decimal("125000.00"),
            tax_pct=Decimal("5.00"),
        )

        # 1. Generate IRN
        resp = self.api.post(f"/api/v1/sales/invoices/{invoice.id}/generate-irn/")
        self.assertEqual(resp.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.einvoice_status, "Generated")
        self.assertTrue(len(invoice.irn) == 64)
        self.assertIsNotNone(invoice.signed_qrcode)

        # 2. Generate E-Way bill on invoice
        ewb_resp = self.api.post(
            f"/api/v1/sales/invoices/{invoice.id}/generate-eway-bill/",
            {"vehicleNo": "GJ05AB1234", "distanceKm": 150},
        )
        self.assertEqual(ewb_resp.status_code, 200)
        invoice.refresh_from_db()
        self.assertTrue(invoice.eway_bill_number.startswith("24"))
        self.assertEqual(invoice.vehicle_number, "GJ05AB1234")

        # 3. Delivery Challan E-Way Bill
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-2026-001",
            party=self.party,
            party_name=self.party.name,
            doc_date=date.today(),
            status="Dispatched",
            location=self.location,
        )
        dc_resp = self.api.post(
            f"/api/v1/sales/delivery-challans/{challan.id}/generate-eway-bill/",
            {"vehicleNo": "GJ05CD5678", "distanceKm": 250},
        )
        self.assertEqual(dc_resp.status_code, 200)
        challan.refresh_from_db()
        self.assertTrue(challan.eway_bill_number.startswith("24"))
        self.assertEqual(challan.vehicle_number, "GJ05CD5678")

    def test_itc04_quarterly_reporting_and_csv(self):
        """Verify Table 4 and Table 5A generation and CSV export for GST ITC-04."""
        jwo = JobWorkOrder.objects.create(
            client=self.client_obj,
            jwo_no="JWO-ITC04-01",
            vendor=self.vendor,
            vendor_name=self.vendor.name,
            process="Rotary Printing",
            order_date=date.today(),
            planned_qty=Decimal("2000.00"),
            rate=Decimal("15.00"),
            status="In-Process",
        )
        # Outward challan
        outward = JWOOutward.objects.create(
            client=self.client_obj,
            job_work_order=jwo,
            challan_no="CH-OUT-001",
            date=date.today(),
            qty=Decimal("2000.00"),
        )
        # Inward receipt
        inward = JWOInward.objects.create(
            client=self.client_obj,
            job_work_order=jwo,
            challan_no="CH-IN-001",
            date=date.today(),
            qty=Decimal("1950.00"),
            accepted=Decimal("1950.00"),
            rejected=Decimal("0.00"),
            status="Received",
        )

        # 1. API GET ITC-04
        resp = self.api.get("/api/v1/jobwork/itc-04/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("table4", resp.data)
        self.assertIn("table5a", resp.data)
        self.assertEqual(len(resp.data["table4"]), 1)
        self.assertEqual(len(resp.data["table5a"]), 1)
        self.assertEqual(resp.data["table4"][0]["challanNumber"], "CH-OUT-001")
        self.assertEqual(resp.data["table5a"][0]["inwardChallanNumber"], "CH-IN-001")

        # 2. CSV Export endpoint
        csv_resp = self.api.get("/api/v1/jobwork/itc-04/export/")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertEqual(csv_resp["Content-Type"], "text/csv")
        self.assertIn("FORM GST ITC-04", csv_resp.content.decode("utf-8"))
        self.assertIn("CH-OUT-001", csv_resp.content.decode("utf-8"))
        self.assertIn("CH-IN-001", csv_resp.content.decode("utf-8"))

    def test_mis_reports_endpoints(self):
        """Verify MIS reporting endpoints return valid backend calculations."""
        # 1. Annual Commercial Leakage
        resp1 = self.api.get("/api/v1/reports/annual-commercial-leakage/")
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("grossSales", resp1.data)
        self.assertIn("items", resp1.data)

        # 2. Party Profitability
        resp2 = self.api.get("/api/v1/reports/party-profitability/")
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("results", resp2.data)

        # 3. Design Colour Analysis
        resp3 = self.api.get("/api/v1/reports/design-colour-analysis/")
        self.assertEqual(resp3.status_code, 200)
        self.assertIn("results", resp3.data)

        # 4. Stock Ageing MIS
        resp4 = self.api.get("/api/v1/reports/stock-ageing-mis/")
        self.assertEqual(resp4.status_code, 200)
        self.assertIn("buckets", resp4.data)
        self.assertIn("summary", resp4.data)
