"""Unit tests for automatic Lead and Deal stage lifecycle driven by Quotation workflow:
1. New Lead created -> automatically enters Draft (linked Deal in Draft).
2. Quotation sent -> advances to Sent.
3. Quotation rejected -> moves to Open for negotiation/revision.
4. Quotation approved -> moves to Won and converts Lead.
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.crm.models import Deal, Lead, Stage
from apps.crm.views import LeadViewSet
from apps.sales.models import Quotation
from apps.sales.views import QuotationViewSet


class LeadDealLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Lifecycle Tenant")
        self.user = User.objects.create(
            client=self.tenant,
            name="Sales Rep",
            email="rep@test.invalid",
            status="Active",
            is_superuser=True,
        )
        self.stage = Stage.objects.create(client=self.tenant, name="New Lead", sequence=1)
        self.factory = APIRequestFactory()

    def test_new_lead_automatically_enters_draft_and_transitions_with_quotation(self):
        # 1. Create a new lead
        create_req = self.factory.post(
            "/leads/",
            {"name": "Apex Fabrics", "phone": "9876543210", "company": "Apex Industries"},
            format="json",
        )
        create_req.client_id = self.tenant.id
        force_authenticate(create_req, user=self.user)
        response = LeadViewSet.as_view({"post": "create"})(create_req)
        self.assertEqual(response.status_code, 201)
        lead_id = response.data["id"]

        lead = Lead.objects.get(id=lead_id)
        # Verify a Deal in "Draft" was automatically created and linked to this lead
        deal = Deal.objects.filter(lead=lead, client_id=self.tenant.id).first()
        self.assertIsNotNone(deal)
        self.assertEqual(deal.stage, "Draft")
        self.assertEqual(lead.converted_deal_id, deal.id)

        # 2. Create a Quotation linked to this lead
        quotation = Quotation.objects.create(
            client=self.tenant,
            quotation_number="QT-TEST-001",
            crm_lead=lead,
            crm_deal=deal,
            status="Draft",
            total=50000,
        )

        # 3. Send the quotation -> deal automatically enters "Sent"
        send_req = self.factory.post(
            f"/sales/quotations/{quotation.id}/send/",
            {"channel": "email", "recipients": ["client@apex.invalid"], "message": "Quotation details"},
            format="json",
        )
        send_req.client_id = self.tenant.id
        force_authenticate(send_req, user=self.user)
        send_resp = QuotationViewSet.as_view({"post": "send"})(send_req, pk=quotation.id)
        self.assertEqual(send_resp.status_code, 200)

        deal.refresh_from_db()
        self.assertEqual(deal.stage, "Sent")

        # 4. Reject the quotation -> deal automatically moves to "Open"
        reject_req = self.factory.post(
            f"/sales/quotations/{quotation.id}/reject/",
            {"reason": "Price negotiation requested"},
            format="json",
        )
        reject_req.client_id = self.tenant.id
        force_authenticate(reject_req, user=self.user)
        reject_resp = QuotationViewSet.as_view({"post": "reject"})(reject_req, pk=quotation.id)
        self.assertEqual(reject_resp.status_code, 200)

        deal.refresh_from_db()
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "Rejected")
        self.assertEqual(deal.stage, "Open")

        # 5. Approve the quotation -> deal moves to "Won"
        # Reset quotation status to draft or revised for approval
        quotation.status = "Draft"
        quotation.save(update_fields=["status"])

        approve_req = self.factory.post(
            f"/sales/quotations/{quotation.id}/approve/",
            {"reason": "Client confirmed updated order"},
            format="json",
        )
        approve_req.client_id = self.tenant.id
        force_authenticate(approve_req, user=self.user)
        approve_resp = QuotationViewSet.as_view({"post": "approve"})(approve_req, pk=quotation.id)
        self.assertEqual(approve_resp.status_code, 200)

        deal.refresh_from_db()
        quotation.refresh_from_db()
        self.assertEqual(deal.stage, "Won")
        self.assertEqual(quotation.status, "Accepted")
