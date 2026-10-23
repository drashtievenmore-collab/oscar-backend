"""Tests for GST E-Invoicing & E-Way Bill Integration Engine (PDF §20)."""
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.models import AuditLog, CompanyProfile
from apps.masters.models import Location, Party
from apps.sales.models import DeliveryChallan, SalesInvoice, SalesInvoiceLine
from apps.sales import einvoice_service


class EInvoiceIntegrationTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="einvoice-tenant", defaults={"name": "EInvoice Tenant"}
        )
        self.user = User.objects.create_user(
            email="einvoice-admin@example.com",
            password="pass",
            client=self.client_obj,
            is_superuser=True,
            name="EInvoice Admin",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        self.company_profile = CompanyProfile.objects.create(
            client=self.client_obj,
            legal_name="Apex Texfab Pvt Ltd",
            trade_name="Apex Texfab",
            gstin="24AABCA1234F1Z9",
            state="Gujarat",
            state_code="24",
            address={"city": "Surat", "line1": "Ring Road", "pincode": 395002},
        )

        self.party = Party.objects.create(
            client=self.client_obj,
            name="Kiran Silk Mills",
            code="CUST-KSM",
            type="Customer",
            gstin="24AABCK5678D1Z2",
            place_of_supply="24-Gujarat",
        )

        self.location = Location.objects.create(
            client=self.client_obj,
            code="WH-SURAT",
            name="Surat Warehouse",
            type="Godown",
            is_active=True,
        )

        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.party,
            party_name=self.party.name,
            party_gstin=self.party.gstin,
            place_of_supply="24",
            doc_date=date.today(),
            invoice_number="INV-2026-901",
            status="Posted",
            location=self.location,
            subtotal=Decimal("50000.00"),
            taxable_value=Decimal("50000.00"),
            total_tax=Decimal("2500.00"),
            total=Decimal("52500.00"),
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=self.invoice,
            line_no=1,
            item_name="Modal Satin Dyed",
            sku="MSD-01",
            hsn_code="5407",
            qty=Decimal("500.00"),
            rate=Decimal("100.00"),
            amount=Decimal("50000.00"),
            tax_pct=Decimal("5.00"),
            tax_amount=Decimal("2500.00"),
            line_total=Decimal("52500.00"),
        )

    @override_settings(EINVOICE_ENABLED=True, EINVOICE_PROVIDER="sandbox-sim")
    def test_missing_company_gstin_raises_validation_error(self):
        """Supplier GSTIN must be read from CompanyProfile; missing GSTIN must raise clear error."""
        self.company_profile.gstin = ""
        self.company_profile.save()

        with self.assertRaises(ValidationError) as ctx:
            einvoice_service.generate_irn(self.invoice, user=self.user)
        self.assertIn("companyGstin", ctx.exception.message_dict)

    @override_settings(EINVOICE_ENABLED=True, EINVOICE_PROVIDER="sandbox-sim")
    def test_invalid_company_gstin_raises_validation_error(self):
        """Invalid length supplier GSTIN must raise clear ValidationError."""
        self.company_profile.gstin = "INVALID_GSTIN"
        self.company_profile.save()

        with self.assertRaises(ValidationError) as ctx:
            einvoice_service.generate_irn(self.invoice, user=self.user)
        self.assertIn("companyGstin", ctx.exception.message_dict)

    @override_settings(EINVOICE_ENABLED=False)
    def test_einvoice_disabled_blocks_generation(self):
        """When EINVOICE_ENABLED is False, generate_irn and generate_eway_bill must be blocked."""
        with self.assertRaises(ValidationError) as ctx:
            einvoice_service.generate_irn(self.invoice, user=self.user)
        self.assertIn("einvoice", ctx.exception.message_dict)

        with self.assertRaises(ValidationError) as ctx:
            einvoice_service.generate_eway_bill(self.invoice, user=self.user)
        self.assertIn("ewayBill", ctx.exception.message_dict)

    @override_settings(EINVOICE_ENABLED=True, EINVOICE_PROVIDER="sandbox-sim")
    def test_sandbox_sim_generates_irn_and_audit_log(self):
        """Sandbox simulator must generate valid IRN and record full request/response in AuditLog."""
        res = einvoice_service.generate_irn(self.invoice, user=self.user)
        self.invoice.refresh_from_db()

        self.assertEqual(self.invoice.einvoice_status, "Generated")
        self.assertEqual(len(self.invoice.irn), 64)
        self.assertIsNotNone(self.invoice.ack_no)
        self.assertIsNotNone(self.invoice.signed_qrcode)

        # Verify AuditLog entry has full request and response
        audit = AuditLog.objects.filter(
            client=self.client_obj,
            action="einvoice.generate_irn",
            entity_id=self.invoice.id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertIsNotNone(audit.before)
        self.assertEqual(audit.before.get("Version"), "1.03")
        self.assertEqual(audit.before["SellerDtls"]["Gstin"], self.company_profile.gstin)
        self.assertEqual(audit.after["irn"], self.invoice.irn)
        self.assertEqual(audit.after["status"], "Generated")

    @override_settings(EINVOICE_ENABLED=True, EINVOICE_PROVIDER="sandbox-sim")
    def test_generate_and_cancel_irn(self):
        """IRN cancellation within statutory window records audit."""
        einvoice_service.generate_irn(self.invoice, user=self.user)
        self.invoice.refresh_from_db()

        cancel_res = einvoice_service.cancel_irn(
            self.invoice, reason="1", remarks="Order cancelled by buyer", user=self.user
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.einvoice_status, "Cancelled")

        cancel_audit = AuditLog.objects.filter(
            client=self.client_obj,
            action="einvoice.cancel_irn",
            entity_id=self.invoice.id,
        ).first()
        self.assertIsNotNone(cancel_audit)
        self.assertEqual(cancel_audit.before["cancelReason"], "1")
        self.assertEqual(cancel_audit.after["status"], "Cancelled")

    @override_settings(EINVOICE_ENABLED=True, EINVOICE_PROVIDER="sandbox-sim")
    def test_generate_eway_bill_on_challan_and_audit(self):
        """E-Way bill generation on delivery challan records 12-digit number and audit payload."""
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-2026-901",
            party=self.party,
            party_name=self.party.name,
            party_gstin=self.party.gstin,
            doc_date=date.today(),
            status="Dispatched",
            location=self.location,
        )
        res = einvoice_service.generate_eway_bill(
            challan,
            transporter_id="TRANS-99",
            vehicle_no="GJ05XY9999",
            approx_distance_km=250,
            user=self.user,
        )
        challan.refresh_from_db()
        self.assertEqual(len(challan.eway_bill_number), 12)
        self.assertEqual(challan.vehicle_number, "GJ05XY9999")
        self.assertEqual(challan.transporter, "TRANS-99")

        audit = AuditLog.objects.filter(
            client=self.client_obj,
            action="einvoice.generate_eway_bill",
            entity_id=challan.id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.before["vehicleNo"], "GJ05XY9999")
        self.assertEqual(audit.after["ewayBillNumber"], challan.eway_bill_number)

    @override_settings(
        EINVOICE_ENABLED=True,
        EINVOICE_PROVIDER="gsp-nic",
        EINVOICE_CLIENT_ID="",
        EINVOICE_CLIENT_SECRET="",
    )
    def test_gsp_nic_provider_missing_credentials_raises_validation_error(self):
        """GspNicProvider must require CA credentials before live calls."""
        with self.assertRaises(ValidationError) as ctx:
            einvoice_service.generate_irn(self.invoice, user=self.user)
        self.assertIn("gspCredentials", ctx.exception.message_dict)

    @override_settings(
        EINVOICE_ENABLED=True,
        EINVOICE_PROVIDER="gsp-nic",
        EINVOICE_CLIENT_ID="dummy-client-id",
        EINVOICE_CLIENT_SECRET="dummy-secret",
        EINVOICE_USERNAME="dummy-user",
        EINVOICE_PASSWORD="dummy-password",
    )
    def test_gsp_nic_provider_structure_with_credentials(self):
        """When credentials are provided, GspNicProvider processes request and logs audit."""
        res = einvoice_service.generate_irn(self.invoice, user=self.user)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.einvoice_status, "Generated")
        self.assertEqual(res["provider"], "gsp-nic")
