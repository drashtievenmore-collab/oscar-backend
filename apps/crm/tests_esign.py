"""DocuSign send-for-signature / signature-status (apps/crm/esign.py).

DocuSign itself is mocked: these tests cover the endpoint contract, not the
network calls.
"""
from datetime import date
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.core.files import delete_bytes
from apps.masters.models import Party

from .models import Contract
from .views import ContractViewSet

DOCUSIGN = dict(
    DOCUSIGN_INTEGRATION_KEY="key",
    DOCUSIGN_USER_ID="user",
    DOCUSIGN_ACCOUNT_ID="account",
    DOCUSIGN_PRIVATE_KEY_PATH="docusign.key",
)


class ContractESignTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Co")
        self.admin = User.objects.create(
            client=self.client_obj, name="Admin", email="admin@t.co", status="Active",
            is_superuser=True,
        )
        self.party = Party.objects.create(
            client=self.client_obj, code="CUST-1", type="Customer", name="Proj Co",
        )
        self.contract = Contract.objects.create(
            client=self.client_obj, contract_number="CON-T-1", title="Supply — Proj Co",
            party=self.party, start_date=date(2026, 10, 1), end_date=date(2027, 10, 1),
            status="Active", body="Payment Terms: 30 days",
        )
        self.factory = APIRequestFactory()

    def call(self, method, action, data=None):
        url = f"/api/v1/crm/contracts/{self.contract.id}/{action}/"
        request = getattr(self.factory, method)(url, data or {}, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.admin)
        name = "send_for_signature" if action == "send-for-signature" else "signature_status"
        return ContractViewSet.as_view({method: name})(request, pk=self.contract.id)

    def test_requires_signer_email(self):
        response = self.call("post", "send-for-signature")
        self.assertEqual(response.status_code, 400)
        self.assertIn("signerEmail", response.data["field_errors"])

    def test_unconfigured_server_says_so(self):
        response = self.call("post", "send-for-signature", {"signerEmail": "a@b.co"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "ESIGN_NOT_CONFIGURED")

    @override_settings(**DOCUSIGN)
    def test_send_then_complete_stores_signed_pdf(self):
        with mock.patch(
            "apps.crm.esign.create_envelope",
            return_value={"envelopeId": "env-1", "status": "sent"},
        ) as create:
            response = self.call(
                "post", "send-for-signature", {"signerEmail": "a@b.co", "signerName": "Asha"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["esignStatus"], "sent")
        self.assertEqual(create.call_args.kwargs["signer_name"], "Asha")
        self.assertTrue(create.call_args.kwargs["pdf_bytes"].startswith(b"%PDF"))

        again = self.call("post", "send-for-signature", {"signerEmail": "a@b.co"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "ESIGN_ALREADY_SENT")

        with mock.patch(
            "apps.crm.esign.envelope_status", return_value={"status": "completed"}
        ), mock.patch("apps.crm.esign.download_signed", return_value=b"%PDF-signed"):
            response = self.call("get", "signature-status")
        self.assertEqual(response.data["esignStatus"], "completed")
        self.assertTrue(response.data["signedFileUrl"])

        self.contract.refresh_from_db()
        self.assertIsNotNone(self.contract.signed_at)
        self.assertEqual(self.contract.signed_file.file_size, len(b"%PDF-signed"))
        delete_bytes(self.contract.signed_file.storage_key)
