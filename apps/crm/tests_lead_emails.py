"""Unit tests for Lead Email sending functionality."""

from unittest.mock import patch
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.crm.models import Lead, LeadEmail, Stage
from apps.crm.views import LeadViewSet


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class LeadEmailSendingTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Email Test Tenant")
        self.user = User.objects.create(
            client=self.tenant,
            name="Sales Rep",
            email="sales@test.invalid",
            status="Active",
            is_superuser=True,
        )
        self.stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        self.lead = Lead.objects.create(
            client=self.tenant,
            lead_number="L-EMAIL-001",
            name="Acme Corp",
            email="contact@acme.invalid",
            stage=self.stage,
            owner=self.user,
        )
        mail.outbox = []

    def request(self, method, path, action, data=None, **kwargs):
        factory = APIRequestFactory()
        req_func = getattr(factory, method)
        request = req_func(path, data or {}, format="json")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)
        return LeadViewSet.as_view({method: action})(request, **kwargs)

    def test_send_lead_email_success(self):
        payload = {
            "toAddresses": ["client@domain.com"],
            "subject": "Follow-up on Proposal",
            "body": "Hello, please find the proposal details attached.",
        }
        response = self.request("post", f"/leads/{self.lead.id}/emails/", "emails", data=payload, pk=self.lead.id)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.subject, "Follow-up on Proposal")
        self.assertIn("client@domain.com", sent_email.to)
        self.assertIn("Hello, please find the proposal details attached.", sent_email.body)

        # Check DB record
        record = LeadEmail.objects.filter(lead=self.lead).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "Sent")
        self.assertIsNotNone(record.sent_at)
        self.assertIsNotNone(record.provider_message_id)
        self.assertFalse(record.error_message)
        self.assertEqual(record.to_addresses, ["client@domain.com"])

        # Check timeline endpoint includes sent email
        timeline_response = self.request("get", f"/leads/{self.lead.id}/timeline/", "timeline", pk=self.lead.id)
        self.assertEqual(timeline_response.status_code, 200)
        items = timeline_response.data.get("data") if isinstance(timeline_response.data, dict) else timeline_response.data
        email_events = [event for event in items if event.get("type") == "email"]
        self.assertTrue(len(email_events) >= 1)
        self.assertEqual(email_events[0]["title"], "Follow-up on Proposal")
        self.assertEqual(email_events[0]["status"], "Sent")

    def test_send_lead_email_validation_failures(self):
        # Empty recipient
        res1 = self.request(
            "post",
            f"/leads/{self.lead.id}/emails/",
            "emails",
            data={"toAddresses": [], "subject": "Test", "body": "Body"},
            pk=self.lead.id,
        )
        self.assertEqual(res1.status_code, 400)

        # Invalid recipient format
        res2 = self.request(
            "post",
            f"/leads/{self.lead.id}/emails/",
            "emails",
            data={"toAddresses": ["not-an-email"], "subject": "Test", "body": "Body"},
            pk=self.lead.id,
        )
        self.assertEqual(res2.status_code, 400)

        # Empty subject
        res3 = self.request(
            "post",
            f"/leads/{self.lead.id}/emails/",
            "emails",
            data={"toAddresses": ["test@example.com"], "subject": "", "body": "Body"},
            pk=self.lead.id,
        )
        self.assertEqual(res3.status_code, 400)

        # Empty body
        res4 = self.request(
            "post",
            f"/leads/{self.lead.id}/emails/",
            "emails",
            data={"toAddresses": ["test@example.com"], "subject": "Test", "body": "  "},
            pk=self.lead.id,
        )
        self.assertEqual(res4.status_code, 400)

        self.assertEqual(len(mail.outbox), 0)

    def test_send_lead_email_provider_failure_handling(self):
        # Mock EmailMultiAlternatives.send to simulate SMTP failure
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=Exception("Connection refused by SMTP")):
            payload = {
                "toAddresses": ["recipient@example.com"],
                "subject": "Failing Email",
                "body": "Should fail",
            }
            response = self.request(
                "post",
                f"/leads/{self.lead.id}/emails/",
                "emails",
                data=payload,
                pk=self.lead.id,
            )

            # Endpoint must return error and not mark as 201 Created
            self.assertIn(response.status_code, [400, 422])
            self.assertEqual(len(mail.outbox), 0)

            # Record must exist in DB marked as Failed with error details
            record = LeadEmail.objects.filter(lead=self.lead, subject="Failing Email").first()
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "Failed")
            self.assertIsNone(record.sent_at)
            self.assertIn("Connection refused by SMTP", record.error_message)
