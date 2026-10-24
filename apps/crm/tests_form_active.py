"""The live lead form is one server-side switch shared by every user."""

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.crm.models import Form
from apps.crm.views import FormViewSet


class ActiveLeadFormTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Active Form Test", slug="active-form-test")
        self.user = User.objects.create(
            client=self.tenant, name="Admin", email="active-form@test.invalid",
            status="Active", is_superuser=True,
        )
        self.first = Form.objects.create(client=self.tenant, name="Lead create form", kind="lead")
        self.second = Form.objects.create(client=self.tenant, name="hello", kind="lead")

    def call(self, method, path, action, data=None):
        factory = APIRequestFactory()
        request = factory.get(path, data or {}) if method == "get" else factory.post(path, data or {}, format="json")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)
        return FormViewSet.as_view({method: action})(request)

    def active_id(self):
        return self.call("get", "/forms/active/", "active", {"kind": "lead"}).data["formId"]

    def set_active(self, form_id):
        return self.call("post", "/forms/set-active/", "set_active", {"formId": form_id, "kind": "lead"})

    def test_switching_one_form_on_switches_the_other_off(self):
        self.assertIsNone(self.active_id())
        self.assertEqual(self.set_active(str(self.first.id)).status_code, 200)
        self.assertEqual(self.active_id(), str(self.first.id))

        self.set_active(str(self.second.id))
        self.assertEqual(self.active_id(), str(self.second.id))
        self.first.refresh_from_db()
        self.assertFalse(self.first.is_active)

    def test_all_off_leaves_no_active_form(self):
        self.set_active(str(self.first.id))
        response = self.set_active(None)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["formId"])
        self.assertIsNone(self.active_id())
        self.assertFalse(Form.objects.filter(client=self.tenant, is_active=True).exists())

    def test_unknown_form_is_rejected(self):
        self.assertEqual(self.set_active("not-a-uuid").status_code, 400)
        other = Client.objects.create(name="Other Tenant", slug="other-tenant-form-test")
        foreign = Form.objects.create(client=other, name="Theirs", kind="lead")
        self.assertEqual(self.set_active(str(foreign.id)).status_code, 400)
