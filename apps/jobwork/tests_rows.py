"""Job work child rows: header PATCH isolation, per-row CRUD, totals, permissions."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Permission, User, UserPermission
from apps.core.models import AuditLog

from .models import JobWorkOrder, JWOOutward

BASE = "/api/v1/jobwork"


def api_for(user):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
    return api


def make_limited_user(client, email, permission_ids):
    user = User.objects.create_user(email=email, password="pass", client=client)
    for pid in permission_ids:
        perm, _ = Permission.objects.get_or_create(
            id=pid, defaults={"module": "jobwork", "group": "Job Work", "label": pid}
        )
        UserPermission.objects.create(user=user, permission=perm, effect="grant")
    return user


class JobWorkRowsTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="jw-rows-tenant", defaults={"name": "JW Rows Tenant"}
        )
        self.admin = User.objects.create_user(
            email="jw-rows-admin@example.com", password="pass",
            client=self.client_obj, is_superuser=True,
        )
        self.api = api_for(self.admin)
        response = self.api.post(
            f"{BASE}/orders/",
            {
                "process": "Dyeing",
                "vendor": "Shree Dyers",
                "orderDate": "2026-04-10",
                "materials": [
                    {"fabricItem": "Grey Rayon", "fabricQuality": "60s", "qty": 1000, "rate": 10},
                ],
                "outwards": [{"date": "2026-04-11", "qty": 600}],
                "charges": [{"type": "Freight", "qty": 1, "rate": 500}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content[:1000])
        self.order = response.json()
        self.url = f"{BASE}/orders/{self.order['id']}"

    def _get(self):
        response = self.api.get(f"{self.url}/")
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    # -- header PATCH --------------------------------------------------------
    def test_header_patch_does_not_touch_children(self):
        before = self._get()
        response = self.api.patch(
            f"{self.url}/", {"remarks": "urgent", "process": "Printing"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        after = response.json()
        self.assertEqual(after["remarks"], "urgent")
        for kind in ("materials", "outwards", "charges"):
            self.assertEqual(after[kind], before[kind], kind)

    def test_header_patch_with_nested_rows_is_rejected(self):
        response = self.api.patch(
            f"{self.url}/", {"remarks": "x", "outwards": []}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:500])
        self.assertEqual(len(self._get()["outwards"]), 1)

    def test_header_totals_are_read_only(self):
        response = self.api.patch(
            f"{self.url}/", {"plannedQty": 1, "totalAmount": 1}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(float(response.json()["plannedQty"]), 1000)
        self.assertEqual(float(response.json()["totalAmount"]), 10000)

    # -- per-row CRUD ----------------------------------------------------------
    def test_outward_row_create_update_delete(self):
        response = self.api.post(
            f"{self.url}/outwards/",
            {"date": "2026-04-12", "qty": 300, "no": "CLIENT-INVENTED", "transporter": "Tempo"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        row = response.json()
        self.assertTrue(row["no"].startswith("JWOUT-"))
        self.assertNotEqual(row["no"], "CLIENT-INVENTED")

        response = self.api.get(f"{self.url}/outwards/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 2)

        response = self.api.patch(
            f"{self.url}/outwards/{row['id']}/", {"qty": 350, "lrNo": "LR-9"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(float(response.json()["qty"]), 350)
        self.assertEqual(response.json()["lrNo"], "LR-9")
        self.assertEqual(response.json()["no"], row["no"])

        response = self.api.delete(f"{self.url}/outwards/{row['id']}/")
        self.assertEqual(response.status_code, 204, response.content[:500])
        self.assertEqual(len(self._get()["outwards"]), 1)
        # Soft delete: row still exists, marked deleted.
        self.assertTrue(
            JWOOutward.objects.filter(pk=row["id"], deleted_at__isnull=False).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(entity_id=self.order["id"], action="outwards_delete").exists()
        )

    def test_inward_cannot_exceed_outstanding(self):
        response = self.api.post(
            f"{self.url}/inwards/", {"date": "2026-04-20", "qty": 700}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:500])
        response = self.api.post(
            f"{self.url}/inwards/",
            {"date": "2026-04-20", "qty": 500, "rejected": 20},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        inward = response.json()
        self.assertTrue(inward["no"].startswith("JWIN-"))
        self.assertEqual(float(inward["accepted"]), 480)
        # Second receipt may only take the remaining 100.
        response = self.api.post(
            f"{self.url}/inwards/", {"date": "2026-04-21", "qty": 150}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:500])
        # Editing the receipt itself is checked against the outstanding qty excluding itself.
        response = self.api.patch(
            f"{self.url}/inwards/{inward['id']}/", {"qty": 600, "accepted": 580}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        # Outward cannot drop below what has been received.
        outward_id = self.order["outwards"][0]["id"]
        response = self.api.patch(
            f"{self.url}/outwards/{outward_id}/", {"qty": 100}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content[:500])
        response = self.api.delete(f"{self.url}/outwards/{outward_id}/")
        self.assertEqual(response.status_code, 409, response.content[:500])

    def test_charge_row_update_and_delete(self):
        charge = self.order["charges"][0]
        self.assertEqual(float(charge["amount"]), 500)
        response = self.api.patch(
            f"{self.url}/charges/{charge['id']}/", {"qty": 2}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(float(response.json()["amount"]), 1000)
        response = self.api.delete(f"{self.url}/charges/{charge['id']}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self._get()["charges"], [])

    def test_unknown_row_is_404(self):
        response = self.api.patch(
            f"{self.url}/outwards/00000000-0000-0000-0000-000000000000/",
            {"qty": 1}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    # -- totals ----------------------------------------------------------------
    def test_material_changes_recompute_header_totals(self):
        response = self.api.post(
            f"{self.url}/materials/",
            {"fabricItem": "Grey Cotton", "qty": 500, "rate": 12},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        mat = response.json()
        self.assertEqual(float(mat["amount"]), 6000)
        order = self._get()
        self.assertEqual(float(order["plannedQty"]), 1500)
        self.assertEqual(float(order["totalAmount"]), 16000)

        response = self.api.patch(f"{self.url}/materials/{mat['id']}/", {"qty": 250}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:500])
        order = self._get()
        self.assertEqual(float(order["plannedQty"]), 1250)
        self.assertEqual(float(order["totalAmount"]), 13000)

        response = self.api.delete(f"{self.url}/materials/{mat['id']}/")
        self.assertEqual(response.status_code, 204)
        order = self._get()
        self.assertEqual(float(order["plannedQty"]), 1000)
        self.assertEqual(float(order["totalAmount"]), 10000)

    # -- closed orders -----------------------------------------------------------
    def test_closed_order_rows_are_frozen(self):
        response = self.api.post(f"{self.url}/close/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response.json()["status"], "Closed")
        outward_id = self.order["outwards"][0]["id"]
        self.assertEqual(self.api.delete(f"{self.url}/outwards/{outward_id}/").status_code, 409)
        self.assertEqual(
            self.api.post(f"{self.url}/outwards/", {"date": "2026-05-01", "qty": 1}, format="json").status_code,
            409,
        )
        self.assertEqual(len(self._get()["outwards"]), 1)

    # -- plan charges --------------------------------------------------------------
    def test_plan_charge_rows(self):
        response = self.api.post(
            f"{BASE}/process-plans/",
            {
                "date": "2026-04-01", "process": "Dyeing", "vendor": "Shree Dyers",
                "charges": [{"type": "Dyeing", "qty": 100, "rate": 5}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        plan = response.json()
        purl = f"{BASE}/process-plans/{plan['id']}"
        # Header PATCH keeps charges and refuses nested rows.
        response = self.api.patch(f"{purl}/", {"remarks": "ok"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["charges"]), 1)
        self.assertEqual(
            self.api.patch(f"{purl}/", {"charges": []}, format="json").status_code, 400
        )
        response = self.api.post(f"{purl}/charges/", {"type": "Freight", "qty": 1, "rate": 300}, format="json")
        self.assertEqual(response.status_code, 201, response.content[:500])
        cid = response.json()["id"]
        response = self.api.patch(f"{purl}/charges/{cid}/", {"rate": 400}, format="json")
        self.assertEqual(float(response.json()["amount"]), 400)
        self.assertEqual(self.api.delete(f"{purl}/charges/{cid}/").status_code, 204)
        self.assertEqual(len(self.api.get(f"{purl}/").json()["charges"]), 1)


class JobWorkPermissionTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="jw-perm-tenant", defaults={"name": "JW Perm Tenant"}
        )
        self.order = JobWorkOrder.objects.create(
            client=self.client_obj, jwo_no="JWO-PERM-1", process="Dyeing",
            vendor_name="V", order_date="2026-04-10", status="In-Process",
        )
        self.viewer = make_limited_user(self.client_obj, "jw-viewer@example.com", ["view_jobwork"])
        self.manager = make_limited_user(
            self.client_obj, "jw-manager@example.com", ["view_jobwork", "manage_jobwork"]
        )
        self.approver = make_limited_user(
            self.client_obj, "jw-approver@example.com",
            ["view_jobwork", "manage_jobwork", "approve_jobwork"],
        )
        self.url = f"{BASE}/orders/{self.order.id}"

    def test_viewer_can_read_but_not_write(self):
        api = api_for(self.viewer)
        self.assertEqual(api.get(f"{BASE}/orders/").status_code, 200)
        self.assertEqual(api.get(f"{self.url}/outwards/").status_code, 200)
        response = api.post(f"{self.url}/outwards/", {"date": "2026-04-11", "qty": 1}, format="json")
        self.assertEqual(response.status_code, 403, response.content[:500])
        self.assertEqual(response.json()["code"], "manage_jobwork")
        self.assertEqual(api.patch(f"{self.url}/", {"remarks": "x"}, format="json").status_code, 403)
        self.assertEqual(
            api.post(f"{BASE}/orders/", {"process": "X", "orderDate": "2026-04-10"}, format="json").status_code,
            403,
        )

    def test_manager_writes_but_cannot_approve(self):
        api = api_for(self.manager)
        response = api.post(f"{self.url}/outwards/", {"date": "2026-04-11", "qty": 5}, format="json")
        self.assertEqual(response.status_code, 201, response.content[:500])
        response = api.post(f"{self.url}/close/", {}, format="json")
        self.assertEqual(response.status_code, 403, response.content[:500])
        self.assertEqual(response.json()["code"], "approve_jobwork")
        # Status moves into Completed / Closed through PATCH are approval points too.
        response = api.patch(f"{self.url}/", {"status": "Completed"}, format="json")
        self.assertEqual(response.status_code, 403, response.content[:500])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "In-Process")

    def test_approver_can_close_and_reopen(self):
        api = api_for(self.approver)
        self.assertEqual(api.post(f"{self.url}/close/", {}, format="json").status_code, 200)
        response = api.patch(f"{self.url}/", {"status": "In-Process"}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertTrue(
            AuditLog.objects.filter(entity_id=self.order.id, action="closed").exists()
        )

    def test_plan_approval_requires_approve_jobwork(self):
        plan_resp = api_for(self.manager).post(
            f"{BASE}/process-plans/",
            {"date": "2026-04-01", "process": "Dyeing", "status": "Draft"},
            format="json",
        )
        self.assertEqual(plan_resp.status_code, 201, plan_resp.content[:500])
        pid = plan_resp.json()["id"]
        response = api_for(self.manager).post(f"{BASE}/process-plans/{pid}/approve/", {}, format="json")
        self.assertEqual(response.status_code, 403)
        response = api_for(self.approver).post(f"{BASE}/process-plans/{pid}/approve/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response.json()["status"], "Active")
        self.assertTrue(response.json()["approver"])
        response = api_for(self.manager).post(f"{BASE}/process-plans/{pid}/complete/", {}, format="json")
        self.assertEqual(response.status_code, 403)
        response = api_for(self.approver).post(f"{BASE}/process-plans/{pid}/complete/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Completed")
