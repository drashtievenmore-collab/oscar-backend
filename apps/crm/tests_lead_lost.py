"""Marking a lead lost preserves its record in the main lead collection."""

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.crm.models import Lead, Stage
from apps.crm.views import LeadViewSet


class LostLeadVisibilityTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Lost Lead Test")
        self.user = User.objects.create(
            client=self.tenant, name="Admin", email="lost-lead@test.invalid",
            status="Active", is_superuser=True,
        )
        self.stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        self.lead = Lead.objects.create(
            client=self.tenant, lead_number="L-001", name="Test Lead",
            stage=self.stage, owner=self.user,
        )

    def request(self, method, path, action, **kwargs):
        request = getattr(APIRequestFactory(), method)(path, {}, format="json")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)
        return LeadViewSet.as_view({method: action})(request, **kwargs)

    def mark_lost(self):
        return self.request(
            "post", f"/leads/{self.lead.id}/mark-lost/", "mark_lost", pk=self.lead.id,
        )

    def test_mark_lost_creates_stage_and_keeps_lead_in_main_list(self):
        response = self.mark_lost()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["lead"]["isLost"])
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.deleted_at)
        self.assertEqual(self.lead.stage.name, "Lost")
        self.assertEqual(self.lead.stage.sequence, 2)

        response = self.request("get", "/leads/", "list")
        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.data["results"] if str(row["id"]) == str(self.lead.id))
        self.assertTrue(row["isLost"])
        self.assertEqual(row["status"], "Lost")

        self.assertEqual(self.mark_lost().status_code, 200)
        self.assertEqual(Stage.objects.filter(client=self.tenant, name="Lost").count(), 1)

    def test_custom_lost_stage_is_exposed_for_filtering(self):
        custom_stage = Stage.objects.create(
            client=self.tenant, name="Not Interested", is_lost=True, sequence=2,
        )
        response = self.mark_lost()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["lead"]["isLost"])
        self.assertEqual(response.data["lead"]["status"], custom_stage.name)
        self.assertFalse(Stage.objects.filter(client=self.tenant, name="Lost").exists())

    def test_bulk_mark_lost_preserves_all_selected_leads(self):
        request = APIRequestFactory().post(
            "/leads/bulk-mark-lost/", {"ids": [str(self.lead.id)]}, format="json",
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)
        response = LeadViewSet.as_view({"post": "bulk_mark_lost"})(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["markedCount"], 1)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.deleted_at)
        self.assertTrue(self.lead.stage.is_lost)
