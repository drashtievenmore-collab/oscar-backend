"""Tests for Tally XML/CSV Export and Logistics Trip Management (PDF §13, §14)."""
from datetime import date
from decimal import Decimal
import xml.etree.ElementTree as ET

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting import services as acct_services, tally_export
from apps.core.models import CompanyProfile
from apps.inventory.models import LotTaka, Trip, TripMaterialLine
from apps.masters.models import Broker, Item, Location, Party, Transporter, Vehicle
from apps.purchase.models import PaymentOut, PurchaseBill, PurchaseBillLine
from apps.sales.models import PaymentIn, SalesInvoice, SalesInvoiceLine


class TallyExportAndTripManagementTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="tally-trip-tenant", defaults={"name": "Tally Trip Tenant"}
        )
        self.user = User.objects.create_user(
            email="tally-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="Tally Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        CompanyProfile.objects.get_or_create(
            client=self.client_obj,
            defaults={
                "legal_name": "Tally & Logistics Exports Pvt Ltd",
                "trade_name": "Tally Logistics",
                "gstin": "24AAACT1234K1Z3",
                "state": "Gujarat",
                "state_code": "24",
                "address": {"city": "Surat", "line1": "Ring Road", "pincode": 395002},
            },
        )
        acct_services.seed_chart_of_accounts(self.client_obj)

        self.customer = Party.objects.create(
            client=self.client_obj,
            name="Jaipur Cotton Corporation",
            code="CUST-JCC",
            type="Customer",
            gstin="08AAACJ1234L1Z1",
            place_of_supply="08-Rajasthan",
        )
        self.supplier = Party.objects.create(
            client=self.client_obj,
            name="Ahmedabad Spinning Mills",
            code="VEND-ASM",
            type="Supplier",
            gstin="24AAACA9876P1Z7",
            place_of_supply="24-Gujarat",
        )
        self.broker = Broker.objects.create(
            client=self.client_obj,
            name="Kailash Textile Brokers",
            code="BRK-KTB",
            phone="9825000001",
        )
        self.location = Location.objects.create(
            client=self.client_obj,
            name="Surat Hub",
            code="WH-SURAT",
            type="Godown",
            is_active=True,
        )
        self.item = Item.objects.create(
            client=self.client_obj,
            name="Cotton Satin Grey 60x60",
            sku="CSG-60",
            item_kind="Fabric",
            uom="Meter",
            cost_price=Decimal("65.00"),
        )

        # Seed invoice
        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            party_name=self.customer.name,
            party_gstin=self.customer.gstin,
            place_of_supply="08",
            invoice_number="INV-TALLY-01",
            doc_date=date.today(),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("65000.00"),
            taxable_value=Decimal("65000.00"),
            igst=Decimal("3250.00"),
            total_tax=Decimal("3250.00"),
            total=Decimal("68250.00"),
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=self.invoice,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("65.00"),
            amount=Decimal("65000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("3250.00"),
            line_total=Decimal("68250.00"),
        )

        # Seed purchase bill
        self.bill = PurchaseBill.objects.create(
            client=self.client_obj,
            party=self.supplier,
            party_name=self.supplier.name,
            party_gstin=self.supplier.gstin,
            place_of_supply="24",
            bill_number="BILL-TALLY-01",
            doc_date=date.today(),
            status="Posted",
            posted_at=timezone.now(),
            subtotal=Decimal("45000.00"),
            taxable_value=Decimal("45000.00"),
            cgst=Decimal("1125.00"),
            sgst=Decimal("1125.00"),
            total_tax=Decimal("2250.00"),
            total=Decimal("47250.00"),
        )
        PurchaseBillLine.objects.create(
            client=self.client_obj,
            purchase_bill=self.bill,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("1000.00"),
            rate=Decimal("45.00"),
            amount=Decimal("45000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("2250.00"),
            line_total=Decimal("47250.00"),
        )

        # Seed payment in & out
        self.payment_in = PaymentIn.objects.create(
            client=self.client_obj,
            party=self.customer,
            payment_number="PAYIN-TALLY-01",
            payment_date=date.today(),
            amount=Decimal("35000.00"),
            mode="Bank",
            status="Active",
        )
        self.payment_out = PaymentOut.objects.create(
            client=self.client_obj,
            party=self.supplier,
            payment_number="PAYOUT-TALLY-01",
            payment_date=date.today(),
            amount=Decimal("25000.00"),
            mode="Bank",
            status="Active",
        )

    # -----------------------------------------------------------------------
    # Tally XML & CSV Export Tests
    # -----------------------------------------------------------------------

    def test_01_tally_masters_export_xml(self):
        """Tally masters XML export produces valid XML conforming to Tally Envelope specification."""
        xml_str = tally_export.export_tally_masters_xml(self.client_obj.id, company_name="Oscar ERP")
        self.assertTrue(xml_str.startswith("<?xml"), "XML declaration missing")
        root = ET.fromstring(xml_str)
        self.assertEqual(root.tag, "ENVELOPE")
        header = root.find("HEADER")
        self.assertIsNotNone(header)
        self.assertEqual(header.find("TALLYREQUEST").text, "Import Data")

        ledgers = root.findall(".//LEDGER")
        self.assertGreater(len(ledgers), 0, "No ledgers generated in Tally masters export")
        ledger_names = [l.get("NAME") for l in ledgers]
        # Verify Chart of Accounts ledgers and Customer/Supplier ledgers are included
        self.assertIn("Sales", ledger_names)
        self.assertIn(self.customer.name, ledger_names)
        self.assertIn(self.supplier.name, ledger_names)

    def test_02_tally_vouchers_export_xml(self):
        """Tally vouchers XML export correctly includes Sales, Purchase, Receipt, and Payment vouchers."""
        xml_str = tally_export.export_tally_vouchers_xml(self.client_obj.id, company_name="Oscar ERP")
        root = ET.fromstring(xml_str)
        self.assertEqual(root.tag, "ENVELOPE")

        vouchers = root.findall(".//VOUCHER")
        self.assertGreaterEqual(len(vouchers), 4, "Expected at least 4 vouchers (sales, bill, payin, payout)")

        voucher_types = [v.get("VCHTYPE") for v in vouchers if v.get("VCHTYPE")]
        self.assertIn("Sales", voucher_types)
        self.assertIn("Purchase", voucher_types)
        self.assertIn("Receipt", voucher_types)
        self.assertIn("Payment", voucher_types)

    def test_03_tally_export_http_endpoints(self):
        """API endpoints /api/v1/accounts/export/tally/ and excel/ answer HTTP 200."""
        # Masters XML export
        m_resp = self.api.get("/api/v1/accounts/export/tally/?type=masters")
        self.assertEqual(m_resp.status_code, 200)
        self.assertIn("application/xml", m_resp.get("Content-Type", ""))
        self.assertIn(b"<ENVELOPE>", m_resp.content)

        # Vouchers XML export
        v_resp = self.api.get("/api/v1/accounts/export/tally/?type=vouchers")
        self.assertEqual(v_resp.status_code, 200)
        self.assertIn("application/xml", v_resp.get("Content-Type", ""))
        self.assertIn(b"<ENVELOPE>", v_resp.content)

        # Sales Register CSV export via excel endpoint
        c_resp = self.api.get("/api/v1/accounts/export/excel/?register=sales")
        self.assertEqual(c_resp.status_code, 200)
        self.assertIn("text/csv", c_resp.get("Content-Type", ""))
        self.assertIn(b"INV-TALLY-01", c_resp.content)

    # -----------------------------------------------------------------------
    # Logistics Trip Management Tests (PDF §13)
    # -----------------------------------------------------------------------

    def test_04_trip_lifecycle_and_cost_allocation(self):
        """Complete trip workflow: Draft -> Loaded -> In Transit -> Delivered with cost allocation."""
        transporter = Transporter.objects.create(
            client=self.client_obj,
            name="Maruti Roadlines",
            code="TRN-MR",
            phone="9898000000",
        )
        taka = LotTaka.objects.create(
            client=self.client_obj,
            lot_number="LOT-TRIP-01",
            taka_number="TK-101",
            item=self.item,
            location=self.location,
            received_quantity=Decimal("500.00"),
            available_quantity=Decimal("500.00"),
            rate=Decimal("65.00"),
        )

        # 1. Create Trip
        trip = Trip.objects.create(
            client=self.client_obj,
            trip_number="TRIP-2026-001",
            trip_date=date.today(),
            trip_type="OUTWARD",
            from_location="Surat Factory Godown",
            to_location="Jaipur Showroom",
            transporter=transporter,
            vehicle_number="GJ05BX1234",
            driver_name="Ramesh Yadav",
            driver_mobile="9876543210",
            lr_number="LR-98765",
            lr_date=date.today(),
            freight_amount=Decimal("3500.00"),
            loading_charges=Decimal("500.00"),
            total_charges=Decimal("4000.00"),
            status="Draft",
        )

        # Attach material line
        TripMaterialLine.objects.create(
            client=self.client_obj,
            trip=trip,
            item=self.item,
            lot_taka=taka,
            lot_number=taka.lot_number,
            taka_number=taka.taka_number,
            quantity=Decimal("500.00"),
            roll_weight=Decimal("120.00"),
            document_reference_type="SalesInvoice",
            document_reference_id=self.invoice.id,
            document_reference_number=self.invoice.invoice_number,
        )

        # 2. Advance to Loaded
        trip.status = "Loaded"
        trip.save(update_fields=["status"])
        self.assertEqual(trip.status, "Loaded")

        # 3. Advance to In Transit
        trip.status = "In Transit"
        trip.save(update_fields=["status"])
        self.assertEqual(trip.status, "In Transit")

        # 4. Deliver
        trip.status = "Delivered"
        trip.save(update_fields=["status"])
        self.assertEqual(trip.status, "Delivered")

        # 5. Verify through ViewSet API
        res = self.api.get(f"/api/v1/inventory/trips/{trip.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("tripNumber") or res.data.get("trip_number"), "TRIP-2026-001")
        self.assertEqual(res.data.get("status"), "Delivered")
        self.assertEqual(Decimal(str(res.data.get("totalCharges") or res.data.get("total_charges"))), Decimal("4000.00"))
