"""Tests for Jobwork ITC-04 export endpoint."""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.jobwork.views import ITC04ExportView, ITC04ReportView


class JobworkITC04ExportTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Jobwork ITC04 Tenant")
        self.user = User.objects.create(
            client=self.tenant,
            name="Jobwork Accountant",
            email="jw@itc04.test",
            status="Active",
            is_staff=True,
            is_superuser=True,
        )
        self.factory = APIRequestFactory()

    def test_url_reversals(self):
        self.assertEqual(reverse("jobwork-itc04-report"), "/api/v1/jobwork/itc-04/")
        self.assertEqual(reverse("jobwork-itc04-export"), "/api/v1/jobwork/itc-04/export/")

    def test_itc04_export_csv(self):
        request = self.factory.get("/api/v1/jobwork/itc-04/export/?table=both&quarter=Q1&financialYear=2026-27")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = ITC04ExportView.as_view()
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("csv", response.get("Content-Type", "").lower())
