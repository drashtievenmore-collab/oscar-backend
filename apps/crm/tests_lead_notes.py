"""Tests for Lead note PATCH and DELETE operations."""

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.crm.models import Lead, LeadNote, Stage
from apps.crm.views import LeadViewSet


class LeadNotePatchTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Note Tenant")
        self.stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        self.author = User.objects.create(
            client=self.tenant,
            name="Note Author",
            email="author@note.test",
            status="Active",
            is_staff=False,
            is_superuser=False,
        )
        self.other_user = User.objects.create(
            client=self.tenant,
            name="Other Sales Rep",
            email="other@note.test",
            status="Active",
            is_staff=False,
            is_superuser=False,
        )
        self.admin_user = User.objects.create(
            client=self.tenant,
            name="Admin User",
            email="admin@note.test",
            status="Active",
            is_staff=True,
            is_superuser=True,
        )
        self.lead = Lead.objects.create(
            client=self.tenant,
            stage=self.stage,
            name="Lead for Notes",
            phone="9988776655",
            company="Test Mills",
        )
        self.note = LeadNote.objects.create(
            client=self.tenant,
            lead=self.lead,
            author=self.author,
            body="Original note text",
        )
        self.factory = APIRequestFactory()

    def test_author_can_patch_own_note(self):
        request = self.factory.patch(
            f"/leads/{self.lead.id}/notes/{self.note.id}/",
            {"body": "Updated by author"},
            format="json",
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.author)

        view = LeadViewSet.as_view({"patch": "note_detail"})
        response = view(request, pk=str(self.lead.id), row_id=str(self.note.id))

        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "Updated by author")

    def test_admin_can_patch_any_note(self):
        request = self.factory.patch(
            f"/leads/{self.lead.id}/notes/{self.note.id}/",
            {"body": "Edited by admin"},
            format="json",
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.admin_user)

        view = LeadViewSet.as_view({"patch": "note_detail"})
        response = view(request, pk=str(self.lead.id), row_id=str(self.note.id))

        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "Edited by admin")

    def test_non_author_non_admin_cannot_patch_note(self):
        request = self.factory.patch(
            f"/leads/{self.lead.id}/notes/{self.note.id}/",
            {"body": "Unauthorized edit"},
            format="json",
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.other_user)

        view = LeadViewSet.as_view({"patch": "note_detail"})
        response = view(request, pk=str(self.lead.id), row_id=str(self.note.id))

        self.assertEqual(response.status_code, 403)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "Original note text")

    def test_patch_non_existent_note_raises_404(self):
        request = self.factory.patch(
            f"/leads/{self.lead.id}/notes/00000000-0000-0000-0000-000000000099/",
            {"body": "Does not exist"},
            format="json",
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.author)

        view = LeadViewSet.as_view({"patch": "note_detail"})
        response = view(request, pk=str(self.lead.id), row_id="00000000-0000-0000-0000-000000000099")

        self.assertEqual(response.status_code, 404)

    def test_delete_note_still_works(self):
        request = self.factory.delete(
            f"/leads/{self.lead.id}/notes/{self.note.id}/"
        )
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.admin_user)

        view = LeadViewSet.as_view({"delete": "note_detail"})
        response = view(request, pk=str(self.lead.id), row_id=str(self.note.id))

        self.assertIn(response.status_code, [200, 204])
        self.note.refresh_from_db()
        self.assertIsNotNone(self.note.deleted_at)
