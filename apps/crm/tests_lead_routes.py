"""Ensure the lead actions resolve at the URLs used by the frontend."""

from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse


@override_settings(ROOT_URLCONF="apps.crm.urls")
class LeadActionRouteTests(SimpleTestCase):
    def test_mark_lost_route_accepts_post(self):
        lead_id = "00000000-0000-0000-0000-000000000001"
        path = f"/leads/{lead_id}/mark-lost/"

        self.assertEqual(reverse("crm-leads-mark-lost", args=[lead_id]), path)
        match = resolve(path)
        self.assertEqual(match.func.actions["post"], "mark_lost")
        self.assertEqual(match.kwargs["pk"], lead_id)

    def test_bulk_mark_lost_route_accepts_post(self):
        path = "/leads/bulk-mark-lost/"

        self.assertEqual(reverse("crm-leads-bulk-mark-lost"), path)
        self.assertEqual(resolve(path).func.actions["post"], "bulk_mark_lost")
