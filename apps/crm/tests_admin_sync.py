"""Administration is the main table: parties and employees must reach it.

Two syncs are asserted here, both of which the Administration screens depend
on:

  - a converted lead and a hand-entered customer are the same row in
    ``masters.Party``, so ``/parties/customers/`` -- what Administration ->
    Clients reads -- must return both, and must say which is which.
  - an HRMS employee owns a linked ``accounts.User``, and editing the employee
    must carry the identity fields back onto that login.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.accounts.serializers import UserSerializer
from apps.crm.models import Lead, Stage
from apps.crm.services import convert_lead_to_customer
from apps.hrms.models import Department, Designation, Employee
from apps.masters.views import PartyViewSet
from apps.hrms.views import EmployeeViewSet
from apps.accounts.views import UserViewSet
from apps.masters.models import Party


class AdminSyncTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Test Co")
        self.admin = User.objects.create(
            client=self.client_obj, name="Admin", email="admin@t.co", status="Active",
            is_superuser=True,
        )
        self.stage_new = Stage.objects.create(
            client=self.client_obj, name="New Lead", sequence=1, is_active=True
        )
        self.factory = APIRequestFactory()

    def call(self, view, method, url, data=None, action=None, **kwargs):
        """A request the tenant permission layer accepts.

        ``force_authenticate`` skips the authentication class, and that class
        is what stamps ``request.client_id`` -- so a test that does not set it
        is rejected as "not attached to a tenant", not as unauthorised.
        """
        request = getattr(self.factory, method)(url, data or {}, format="json")
        request.client_id = self.client_obj.id
        force_authenticate(request, user=self.admin)
        actions = {
            "get": {"get": action or "list"},
            "post": {"post": "create"},
            "patch": {"patch": "partial_update"},
        }[method]
        return view.as_view(actions)(request, **kwargs)

    # -- Customers -> Administration -> Clients ---------------------------
    def test_customers_endpoint_returns_manual_and_converted_parties(self):
        manual = Party.objects.create(
            client=self.client_obj, code="CUST-1", type="Customer",
            name="Hand Entered Ltd", phone="9000000001", email="manual@t.co",
            created_by=self.admin,
        )
        lead = Lead.objects.create(
            client=self.client_obj, lead_number="L-001", name="Ravi",
            company="Converted Textiles", phone="9000000002", email="ravi@t.co",
            stage=self.stage_new, owner=self.admin, created_by=self.admin,
        )
        convert_lead_to_customer(lead, user=self.admin)
        # The service re-reads and saves its own instance of the lead.
        lead.refresh_from_db()
        Party.objects.create(
            client=self.client_obj, code="VEND-1", type="Vendor",
            name="A Supplier", created_by=self.admin,
        )

        response = self.call(
            PartyViewSet, "get", "/parties/customers/", action="customers"
        )
        self.assertEqual(response.status_code, 200)
        rows = response.data["results"]
        by_name = {row["name"]: row for row in rows}

        # A hand-entered customer and the one lead conversion created are both
        # here -- Administration shows one list, not two.
        self.assertEqual(
            sorted(by_name), ["Converted Textiles", "Hand Entered Ltd"]
        )
        self.assertEqual(by_name["Hand Entered Ltd"]["id"], str(manual.id))
        self.assertEqual(by_name["Converted Textiles"]["id"], str(lead.party_id))

        # Which side of the CRM each account came from.
        self.assertEqual(by_name["Converted Textiles"]["leadCount"], 1)
        self.assertEqual(by_name["Hand Entered Ltd"]["leadCount"], 0)

    def test_lead_count_is_zero_everywhere_else(self):
        """The annotation is opt-in: a plain party read must not break."""
        Party.objects.create(
            client=self.client_obj, code="CUST-2", type="Customer",
            name="Plain Ltd", created_by=self.admin,
        )
        response = self.call(PartyViewSet, "get", "/parties/")
        row = next(r for r in response.data["results"] if r["name"] == "Plain Ltd")
        self.assertEqual(row["leadCount"], 0)

    # -- Employees -> Administration -> Users -----------------------------
    def test_employee_create_and_update_sync_the_user_row(self):
        department = Department.objects.create(
            client=self.client_obj, name="Sales", created_by=self.admin
        )
        designation = Designation.objects.create(
            client=self.client_obj, name="Executive", created_by=self.admin
        )

        # Created over the API, the way the HRMS screen does it, so the
        # viewset hook that owns the sync is the one under test.
        response = self.call(
            EmployeeViewSet, "post", "/hrms/employees/",
            {
                "name": "Asha Rao",
                "email": "asha@t.co",
                "phone": "9111111111",
                "departmentId": str(department.id),
                "designationId": str(designation.id),
                "joiningDate": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 201, response.data)
        employee = Employee.objects.get(email="asha@t.co")

        # The create path must have produced the login on its own.
        user = User.objects.get(email="asha@t.co")
        self.assertEqual(user.employee_id, employee.id)
        self.assertEqual(user.department, "Sales")
        self.assertEqual(user.joined_date, employee.joining_date)

        row = UserSerializer(user).data
        self.assertTrue(row["isFromHrms"])
        self.assertEqual(row["employeeId"], employee.employee_code)
        self.assertEqual(row["designation"], "Executive")
        self.assertEqual(row["employeeStatus"], "Active")

        # The admin list the screen reads has to carry the same thing.
        listed = self.call(UserViewSet, "get", "/admin/users/").data["results"]
        asha = next(u for u in listed if u["email"] == "asha@t.co")
        self.assertTrue(asha["isFromHrms"])
        self.assertEqual(asha["designation"], "Executive")
        self.assertEqual(asha["employeeStatus"], "Active")

        # An HRMS edit has to travel back onto the login row, otherwise the
        # admin screen keeps showing the name HRMS no longer holds.
        response = self.call(
            EmployeeViewSet, "patch", f"/hrms/employees/{employee.id}/",
            {"name": "Asha R. Rao", "phone": "9222222222", "designationId": None},
            pk=str(employee.id),
        )
        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        self.assertEqual(user.name, "Asha R. Rao")
        self.assertEqual(user.phone, "9222222222")
        self.assertIsNone(UserSerializer(user).data["designation"])

        # Re-syncing is idempotent: one employee, one login.
        self.assertEqual(
            User.objects.filter(client=self.client_obj, employee=employee).count(), 1
        )

    def test_direct_account_is_not_marked_as_hrms(self):
        user = User.objects.create(
            client=self.client_obj, name="Direct", email="direct@t.co", status="Active"
        )
        row = UserSerializer(user).data
        self.assertFalse(row["isFromHrms"])
        self.assertIsNone(row["employeeId"])
        self.assertIsNone(row["designation"])
        self.assertIsNone(row["employeeStatus"])