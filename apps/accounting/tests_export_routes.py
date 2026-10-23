"""Tests for Accounting Tally and Excel export endpoints."""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.accounting.views import TallyExportView, ExcelExportView


class AccountingExportRouteTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Accounting Export Tenant")
        self.user = User.objects.create(
            client=self.tenant,
            name="Accountant",
            email="acct@export.test",
            status="Active",
            is_staff=True,
            is_superuser=True,
        )
        self.factory = APIRequestFactory()

    def test_url_reversals(self):
        self.assertEqual(reverse("accounts-tally-export"), "/api/v1/accounts/export/tally/")
        self.assertEqual(reverse("accounts-excel-export"), "/api/v1/accounts/export/excel/")

    def test_tally_export_vouchers(self):
        request = self.factory.get("/api/v1/accounts/export/tally/?type=vouchers")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = TallyExportView.as_view()
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("xml", response.get("Content-Type", "").lower())

    def test_excel_export_sales(self):
        request = self.factory.get("/api/v1/accounts/export/excel/?register=sales")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = ExcelExportView.as_view()
        response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("csv", response.get("Content-Type", "").lower())
