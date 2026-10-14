from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.customer_clients import CustomerClientViewSet
from apps.accounts.models import Client, User
from apps.crm.models import CrmProject, Deal
from apps.masters.models import Party
from apps.masters.views import PartyViewSet


class CustomerClientTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Test Company", slug="customer-client-tests")
        self.other_tenant = Client.objects.create(name="Other Company", slug="other-client-tests")
        self.user = User.objects.create(
            client=self.tenant, name="Administrator", email="admin@example.com", status="Active",
        )
        self.user.permission_ids = ["menu_admin", "manage_company_profile"]
        self.factory = APIRequestFactory()
        self.customer = self.party("Customer", "CUST-001", "Customer One")
        self.dual = self.party("Both", "CUST-002", "Dual Partner")
        self.vendor = self.party("Vendor", "VEND-001", "Vendor One")
        self.transporter = self.party("Transporter", "TRNS-001", "Transporter One")
        self.foreign = self.party("Customer", "CUST-001", "Foreign Customer", self.other_tenant)

    def party(self, party_type, code, name, tenant=None):
        return Party.objects.create(
            client=tenant or self.tenant, type=party_type, code=code, name=name,
            email="customer@example.com", phone="+91 9876543210",
            billing_address={"city": "Surat", "state": "Gujarat"},
        )

    def request(self, method="get", action="list", data=None, pk=None, viewset=CustomerClientViewSet):
        request = getattr(self.factory, method)("/api/v1/admin/customer-clients/", data=data or {}, format="json")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)
        return viewset.as_view({method: action})(request, **({"pk": pk} if pk else {}))

    def test_route_uses_customer_viewset(self):
        self.assertEqual(reverse("admin-customer-clients-list"), "/api/v1/admin/customer-clients/")

    def test_lists_existing_customers_and_dual_partners(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual({row["id"] for row in response.data["results"]}, {str(self.customer.id), str(self.dual.id)})
        self.assertEqual(response.data["aggregates"]["total"], 2)
        row = response.data["results"][0]
        self.assertEqual(row["phone"], self.customer.phone)
        self.assertEqual(row["billingAddress"]["city"], "Surat")

    def test_includes_new_party_records_without_duplicate_clients(self):
        response = self.request("post", "create", {"name": "New Customer"}, viewset=PartyViewSet)
        self.assertEqual(response.status_code, 201)
        rows = self.request().data["results"]
        self.assertIn(response.data["id"], {row["id"] for row in rows})
        self.assertEqual(Client.objects.count(), 2)

    def test_paginates_all_customers(self):
        for index in range(26):
            self.party("Customer", f"CUST-EXTRA-{index}", f"Extra {index:02}")
        first = self.request(data={"page_size": 25})
        second = self.request(data={"page_size": 25, "page": 2})
        self.assertEqual(first.data["count"], 28)
        self.assertEqual(len(first.data["results"]), 25)
        self.assertEqual(len(second.data["results"]), 3)
        self.assertIsNotNone(first.data["next"])

    def test_excludes_deleted_customers(self):
        self.customer.soft_delete(self.user)
        self.assertEqual(self.request().data["count"], 1)

    def test_cannot_read_or_modify_other_tenants_or_vendors(self):
        for party in (self.foreign, self.vendor, self.transporter):
            self.assertEqual(self.request(action="retrieve", pk=party.id).status_code, 404)
            self.assertEqual(self.request("patch", "partial_update", {"name": "Changed"}, party.id).status_code, 404)

    def test_edit_updates_same_customer_and_preserves_dual_role(self):
        response = self.request("patch", "partial_update", {"name": "Updated Partner", "phone": ""}, self.dual.id)
        self.assertEqual(response.status_code, 200)
        self.dual.refresh_from_db()
        self.assertEqual(self.dual.name, "Updated Partner")
        self.assertEqual(self.dual.phone, "")
        self.assertEqual(self.dual.type, "Both")
        self.assertEqual(self.dual.billing_address["state"], "Gujarat")

    def test_creates_customer_with_server_code(self):
        response = self.request("post", "create", {"name": "Client Created", "email": "", "phone": ""})
        self.assertEqual(response.status_code, 201)
        party = Party.objects.get(pk=response.data["id"])
        self.assertEqual(party.type, "Customer")
        self.assertEqual(party.client_id, self.tenant.id)
        self.assertTrue(party.code.startswith("CUST-"))

    def test_rejects_vendor_conversion(self):
        response = self.request("patch", "partial_update", {"type": "Vendor"}, self.customer.id)
        self.assertEqual(response.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.type, "Customer")

    def test_counts_only_current_tenant_live_relationships(self):
        Deal.objects.create(client=self.tenant, party=self.customer, title="Deal One", deal_number="DEAL-1")
        removed = Deal.objects.create(client=self.tenant, party=self.customer, title="Removed Deal", deal_number="DEAL-2")
        removed.soft_delete(self.user)
        Deal.objects.create(client=self.other_tenant, party=self.customer, title="Foreign Deal", deal_number="DEAL-3")
        CrmProject.objects.create(client=self.tenant, party=self.customer, name="Project One")
        self.dual.status = "Inactive"
        self.dual.save()
        response = self.request()
        row = next(row for row in response.data["results"] if row["id"] == str(self.customer.id))
        self.assertEqual(row["deals"], 1)
        self.assertEqual(row["projects"], 1)
        self.assertEqual(response.data["aggregates"], {"total": 2, "active": 1, "withDeals": 1, "withProjects": 1})

    def test_requires_administration_permissions(self):
        self.user.permission_ids = []
        self.assertEqual(self.request().status_code, 403)
        self.user.permission_ids = ["menu_admin"]
        self.assertEqual(self.request("patch", "partial_update", {"name": "Denied"}, self.customer.id).status_code, 403)

    def test_delete_archives_shared_customer(self):
        response = self.request("delete", "destroy", pk=self.customer.id)
        self.assertEqual(response.status_code, 204)
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.deleted_at)
        self.assertEqual(self.request().data["count"], 1)
