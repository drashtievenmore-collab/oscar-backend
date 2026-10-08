"""Lead -> Estimate -> Quotation -> Approval -> Customer auto-conversion."""
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, User
from apps.crm.models import Lead, Stage
from apps.crm.services import convert_lead_to_customer
from apps.masters.models import Party
from apps.sales.models import Estimate, Quotation


class LeadConversionFlowTest(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Co")
        self.user = User.objects.create(
            client=self.client_obj, name="Sales Owner", email="owner@t.co", status="Active"
        )
        self.stage_new = Stage.objects.create(
            client=self.client_obj, name="New Lead", sequence=1, is_active=True
        )
        self.stage_won = Stage.objects.create(
            client=self.client_obj, name="Won", sequence=7,
            is_won=True, is_active=True,
        )
        self.lead = Lead.objects.create(
            client=self.client_obj,
            lead_number="L-001",
            name="Ravi Contact",
            company="Ravi Textiles",
            phone="9999999999",
            email="ravi@t.co",
            city="Surat",
            state="Gujarat",
            country="India",
            stage=self.stage_new,
            source=None,
            owner=self.user,
            created_by=self.user,
        )
        self.party = Party.objects.create(
            client=self.client_obj, code="CUST-1", type="Customer",
            name="Ravi Textiles", phone="9999999999", email="ravi@t.co",
            created_by=self.user,
        )
        self.estimate = Estimate.objects.create(
            client=self.client_obj, party=self.party, doc_date=timezone.localdate(),
            status="Draft", crm_lead=self.lead, created_by=self.user,
        )
        self.quotation = Quotation.objects.create(
            client=self.client_obj, party=self.party, doc_date=timezone.localdate(),
            status="Sent", estimate=self.estimate, crm_lead=self.lead,
            subject="Fabric quote", created_by=self.user,
        )

    def test_convert_service_links_and_marks_converted(self):
        result = convert_lead_to_customer(
            self.lead, user=self.user,
            source="Quotation Approval", reference="QT-1",
        )
        self.assertFalse(result["created"])  # matched existing party
        lead = result["lead"]
        self.assertEqual(lead.party_id, self.party.id)
        self.assertIsNotNone(lead.converted_at)
        self.assertEqual(lead.conversion_source, "Quotation Approval")
        self.assertEqual(lead.conversion_reference, "QT-1")
        self.assertEqual(lead.stage_id, self.stage_won.id)
        self.assertEqual(Party.objects.filter(
            client_id=self.client_obj.id, name__iexact="Ravi Textiles",
            deleted_at__isnull=True).count(), 1)

    def test_convert_service_creates_party_preserving_lead(self):
        self.party.soft_delete(self.user)
        result = convert_lead_to_customer(
            self.lead, user=self.user,
            source="Quotation Approval", reference="QT-9",
        )
        self.assertTrue(result["created"])
        party = result["party"]
        self.assertEqual(party.name, "Ravi Textiles")
        self.assertEqual(party.phone, "9999999999")
        self.assertEqual(party.email, "ravi@t.co")
        self.assertEqual(party.place_of_supply, "Gujarat")
        self.assertTrue(party.contacts.filter(name="Ravi Contact").exists())

    def test_reconvert_is_idempotent_no_duplicate(self):
        first = convert_lead_to_customer(self.lead, user=self.user, reference="QT-1")
        count = Party.objects.filter(
            client_id=self.client_obj.id, deleted_at__isnull=True).count()
        second = convert_lead_to_customer(self.lead, user=self.user, reference="QT-1")
        self.assertEqual(second["party"].id, first["party"].id)
        self.assertFalse(second["created"])
        self.assertEqual(Party.objects.filter(
            client_id=self.client_obj.id, deleted_at__isnull=True).count(), count)


class QuotationApproveEndpointTest(TestCase):
    """POST /sales/quotations/{id}/approve/ -> Accepted + auto-conversion."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Co")
        self.admin = User.objects.create(
            client=self.client_obj, name="Admin", email="admin@t.co",
            status="Active", is_superuser=True,
        )
        self.stage_new = Stage.objects.create(
            client=self.client_obj, name="New Lead", sequence=1, is_active=True
        )
        Stage.objects.create(
            client=self.client_obj, name="Won", sequence=7,
            is_won=True, is_active=True,
        )
        self.lead = Lead.objects.create(
            client=self.client_obj, lead_number="L-010", name="Mira Contact",
            company="Mira Fabrics", phone="8888888888", email="mira@t.co",
            city="Surat", state="Gujarat", country="India",
            stage=self.stage_new, owner=self.admin, created_by=self.admin,
        )
        self.party = Party.objects.create(
            client=self.client_obj, code="CUST-10", type="Customer",
            name="Temp Party", created_by=self.admin,
        )
        self.quotation = Quotation.objects.create(
            client=self.client_obj, party=self.party, doc_date=timezone.localdate(),
            status="Sent", crm_lead=self.lead, subject="Fabric quote",
            created_by=self.admin,
        )

    def _approve(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.sales.views import QuotationViewSet

        factory = APIRequestFactory()
        request = factory.post(
            f"/sales/quotations/{self.quotation.id}/approve/", {}, format="json"
        )
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.admin)
        view = QuotationViewSet.as_view({"post": "approve"})
        return view(request, pk=str(self.quotation.id))

    def test_approve_converts_lead_and_keeps_quotation_linked(self):
        from apps.sales.models import SalesOrder

        parties_before = Party.objects.filter(
            client_id=self.client_obj.id, deleted_at__isnull=True).count()
        response = self._approve()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["message"],
            "Quotation approved. Lead has been converted to Customer successfully.",
        )

        self.quotation.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(self.quotation.status, "Accepted")
        # Quotation still linked to the lead and to a customer.
        self.assertEqual(self.quotation.crm_lead_id, self.lead.id)
        self.assertIsNotNone(self.quotation.party_id)
        # Lead converted: party link + stamps + Won stage (out of pipeline).
        self.assertEqual(self.lead.party_id, self.quotation.party_id)
        self.assertIsNotNone(self.lead.converted_at)
        self.assertEqual(self.lead.conversion_reference, self.quotation.quotation_number)
        self.assertTrue(self.lead.stage.is_won)
        # Customer record carries the lead data.
        customer = Party.objects.get(pk=self.lead.party_id)
        self.assertEqual(customer.phone, "8888888888")
        self.assertEqual(customer.email, "mira@t.co")
        # No Sales Order is created by approval.
        self.assertEqual(SalesOrder.objects.count(), 0)

        # Re-approving links the same customer: no duplicate, still Accepted.
        response2 = self._approve()
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(
            response2.data["customer"]["id"], response.data["customer"]["id"])
        self.assertEqual(Party.objects.filter(
            client_id=self.client_obj.id, deleted_at__isnull=True).count(),
            parties_before + 1)  # exactly the one created by approval
