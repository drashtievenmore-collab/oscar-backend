"""Tests for MIS Reporting and Business Position Dashboard (PDF §12, §14)."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting import services as acct_services
from apps.core.models import CompanyProfile
from apps.masters.models import Item, Location, Party
from apps.purchase.models import PaymentOut, PurchaseBill, PurchaseBillLine
from apps.reports import dashboard
from apps.reports.views import REPORT_CATALOGUE
from apps.sales.models import PaymentIn, SalesInvoice, SalesInvoiceLine


class MISReportsAndDashboardTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="report-test-tenant", defaults={"name": "Report Test Tenant"}
        )
        self.user = User.objects.create_user(
            email="report-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="Report Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        CompanyProfile.objects.get_or_create(
            client=self.client_obj,
            defaults={
                "legal_name": "Oscar MIS Mills Ltd",
                "trade_name": "Oscar MIS",
                "gstin": "24AAACM1234D1Z4",
                "state": "Gujarat",
                "state_code": "24",
                "address": {"city": "Surat", "line1": "Ring Road", "pincode": 395002},
            },
        )
        acct_services.seed_chart_of_accounts(self.client_obj)

        self.party = Party.objects.create(
            client=self.client_obj,
            name="Surat Silk Emporium",
            code="CUST-SSE",
            type="Customer",
            gstin="24AAACS7777H1Z5",
            place_of_supply="24-Gujarat",
        )

        self.supplier = Party.objects.create(
            client=self.client_obj,
            name="Vapi Grey Textiles",
            code="VEND-VGT",
            type="Supplier",
            gstin="24AAACV4444J1Z8",
            place_of_supply="24-Gujarat",
        )

        self.location = Location.objects.create(
            client=self.client_obj,
            name="Main Warehouse",
            code="WH-MAIN",
            type="Godown",
            is_active=True,
        )

        self.item = Item.objects.create(
            client=self.client_obj,
            name="Viscose Rayon Printed",
            sku="VRP-100",
            item_kind="Fabric",
            uom="Meter",
            cost_price=Decimal("75.00"),
        )

        # Seed sample sales invoice
        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.party,
            party_name=self.party.name,
            party_gstin=self.party.gstin,
            place_of_supply="24",
            invoice_number="INV-RPT-001",
            doc_date=date.today(),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("75000.00"),
            taxable_value=Decimal("75000.00"),
            cgst=Decimal("1875.00"),
            sgst=Decimal("1875.00"),
            total_tax=Decimal("3750.00"),
            total=Decimal("78750.00"),
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=self.invoice,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("75.00"),
            amount=Decimal("75000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("3750.00"),
            line_total=Decimal("78750.00"),
        )

        # Seed sample payment in
        self.payment_in = PaymentIn.objects.create(
            client=self.client_obj,
            party=self.party,
            payment_number="PAY-RPT-001",
            payment_date=date.today(),
            amount=Decimal("40000.00"),
            mode="Bank",
            status="Active",
        )

        # Seed sample purchase bill
        self.bill = PurchaseBill.objects.create(
            client=self.client_obj,
            party=self.supplier,
            party_name=self.supplier.name,
            party_gstin=self.supplier.gstin,
            place_of_supply="24",
            bill_number="BILL-RPT-001",
            doc_date=date.today(),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("50000.00"),
            taxable_value=Decimal("50000.00"),
            cgst=Decimal("1250.00"),
            sgst=Decimal("1250.00"),
            total_tax=Decimal("2500.00"),
            total=Decimal("52500.00"),
        )
        PurchaseBillLine.objects.create(
            client=self.client_obj,
            purchase_bill=self.bill,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("50.00"),
            amount=Decimal("50000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("2500.00"),
            line_total=Decimal("52500.00"),
        )

    def test_01_all_mis_report_catalogue_keys_return_200(self):
        """Every report key registered in REPORT_CATALOGUE answers HTTP 200."""
        today = date.today().isoformat()
        first_of_month = date.today().replace(day=1).isoformat()

        for key, name, module, params in REPORT_CATALOGUE:
            query = {}
            if "date_from" in params:
                query["date_from"] = first_of_month
            if "date_to" in params:
                query["date_to"] = today
            if "asOf" in params:
                query["asOf"] = today
            if "month" in params:
                query["month"] = date.today().strftime("%Y-%m")

            url = f"/api/v1/reports/{key}/"
            res = self.api.get(url, query)
            self.assertEqual(
                res.status_code,
                200,
                f"Report '{key}' ({name}) failed with HTTP {res.status_code}: {res.data}",
            )
            # Response must be structured dict or list
            self.assertTrue(
                isinstance(res.data, (dict, list)),
                f"Report '{key}' returned unexpected payload type {type(res.data)}",
            )

    def test_02_daily_business_position_numbers_match_documents(self):
        """Daily business position sales, collections, and purchases match underlying documents."""
        pos = dashboard.business_dashboard(self.client_obj.id, day=date.today())
        self.assertIn("kpis", pos)
        kpis = pos["kpis"]

        # Sales today: matches the posted invoice (₹78,750.00)
        self.assertEqual(kpis["sales"]["current"], Decimal("78750.00"))

        # Collection today: matches payment in (₹40,000.00)
        self.assertEqual(kpis["collection"]["current"], Decimal("40000.00"))

        # Purchase today: matches purchase bill (₹52,500.00)
        self.assertEqual(kpis["purchase"]["current"], Decimal("52500.00"))

    def test_03_main_dashboard_api_endpoint(self):
        """Dashboard overview endpoint /api/v1/dashboard/ returns 200 and KPI structure."""
        res = self.api.get("/api/v1/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("sales", res.data)
        self.assertIn("purchase", res.data)
        self.assertIn("cash", res.data)
        self.assertEqual(Decimal(str(res.data["sales"]["monthToDate"])), Decimal("78750.00"))
        self.assertEqual(Decimal(str(res.data["purchase"]["monthToDate"])), Decimal("52500.00"))
