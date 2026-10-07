"""Job Work register tests — derived registers plus nested reprocess lines."""
from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User

from .models import JobWorkOrder, JWOInward, JWOMaterial, JWOOutward


def make_user(client):
    user = User.objects.filter(email="pending-register@example.com").first()
    if not user:
        user = User.objects.create_user(
            email="pending-register@example.com",
            password="pass",
            client=client,
        )
    return user


class PendingRegisterTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="pending-test-tenant", defaults={"name": "Pending Test Tenant"}
        )
        self.user = make_user(self.client_obj)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
        )
        self.api = api

    def _make_jwo(self, jwo_no, expected_completion, outwards, inwards):
        order = JobWorkOrder.objects.create(
            client=self.client_obj,
            jwo_no=jwo_no,
            process="Dyeing",
            vendor_name="Vendor B",
            order_date=date(2026, 1, 5),
            planned_qty=24640,
            expected_completion=expected_completion,
            status="In-Process",
        )
        JWOMaterial.objects.create(
            client=self.client_obj,
            job_work_order=order,
            fabric_item="Cotton Fabric",
            fabric_quality="GSM 120",
            shade="Navy Blue",
            qty=24640,
        )
        for i, qty in enumerate(outwards):
            JWOOutward.objects.create(
                client=self.client_obj,
                job_work_order=order,
                no=f"OUT-{jwo_no}-{i}",
                date=date(2026, 2, 1),
                qty=qty,
            )
        for i, qty in enumerate(inwards):
            JWOInward.objects.create(
                client=self.client_obj,
                job_work_order=order,
                no=f"IN-{jwo_no}-{i}",
                date=date(2026, 3, 1),
                qty=qty,
                accepted=qty,
            )
        return order

    def _register(self, **params):
        response = self.api.get("/api/v1/jobwork/orders/pending-register/", params)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def test_pending_qty_is_outward_minus_inward(self):
        self._make_jwo(
            "JWO-PEND-001",
            date.today() + timedelta(days=30),
            outwards=[15000, 9640],
            inwards=[12000],
        )
        body = self._register()
        row = next(r for r in body["results"] if r["jwoNo"] == "JWO-PEND-001")
        self.assertEqual(row["totalOutward"], 24640)
        self.assertEqual(row["totalInward"], 12000)
        self.assertEqual(row["pendingQty"], 12640)
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["fabricItem"], "Cotton Fabric")
        self.assertEqual(row["vendor"], "Vendor B")

    def test_inward_updates_pending_automatically(self):
        order = self._make_jwo(
            "JWO-PEND-002",
            date.today() + timedelta(days=30),
            outwards=[24640],
            inwards=[12000],
        )
        self.assertEqual(
            next(
                r for r in self._register()["results"]
                if r["jwoNo"] == "JWO-PEND-002"
            )["pendingQty"],
            12640,
        )
        JWOInward.objects.create(
            client=self.client_obj,
            job_work_order=order,
            no="IN-EXTRA",
            date=date(2026, 4, 1),
            qty=5000,
            accepted=5000,
        )
        self.assertEqual(
            next(
                r for r in self._register()["results"]
                if r["jwoNo"] == "JWO-PEND-002"
            )["pendingQty"],
            7640,
        )

    def test_completed_when_fully_received(self):
        self._make_jwo(
            "JWO-PEND-003",
            date.today() + timedelta(days=30),
            outwards=[15000],
            inwards=[15000],
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-PEND-003"
        )
        self.assertEqual(row["pendingQty"], 0)
        self.assertEqual(row["status"], "Completed")
        self.assertEqual(row["daysPending"], 0)

    def test_overdue_when_expected_date_passed(self):
        self._make_jwo(
            "JWO-PEND-004",
            date.today() - timedelta(days=1),
            outwards=[18000],
            inwards=[5000],
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-PEND-004"
        )
        self.assertEqual(row["pendingQty"], 13000)
        self.assertEqual(row["status"], "Overdue")

    def test_jwo_without_outward_is_excluded(self):
        self._make_jwo(
            "JWO-PEND-005",
            date.today() + timedelta(days=30),
            outwards=[],
            inwards=[],
        )
        body = self._register()
        self.assertFalse([r for r in body["results"] if r["jwoNo"] == "JWO-PEND-005"])

    def test_status_filter(self):
        body = self._register(status="Completed")
        self.assertTrue(all(r["status"] == "Completed" for r in body["results"]))


class VendorReconciliationTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="pending-test-tenant", defaults={"name": "Pending Test Tenant"}
        )
        self.user = make_user(self.client_obj)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
        )
        self.api = api

    def _make_jwo(self, jwo_no, expected_completion, outwards, inwards):
        order = JobWorkOrder.objects.create(
            client=self.client_obj,
            jwo_no=jwo_no,
            process="Dyeing",
            vendor_name="Vendor B",
            order_date=date(2026, 1, 5),
            planned_qty=24640,
            expected_completion=expected_completion,
            status="In-Process",
        )
        JWOMaterial.objects.create(
            client=self.client_obj,
            job_work_order=order,
            fabric_item="Cotton Fabric",
            fabric_quality="GSM 120",
            shade="Navy Blue",
            qty=24640,
        )
        for i, qty in enumerate(outwards):
            JWOOutward.objects.create(
                client=self.client_obj,
                job_work_order=order,
                no=f"OUT-{jwo_no}-{i}",
                date=date(2026, 2, 1),
                qty=qty,
            )
        for i, (qty, accepted, rejected) in enumerate(inwards):
            JWOInward.objects.create(
                client=self.client_obj,
                job_work_order=order,
                no=f"IN-{jwo_no}-{i}",
                date=date(2026, 3, 1),
                qty=qty,
                accepted=accepted,
                rejected=rejected,
            )
        return order

    def _register(self, **params):
        response = self.api.get(
            "/api/v1/jobwork/orders/vendor-reconciliation/", params
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def test_quantities_derive_from_source_transactions(self):
        self._make_jwo(
            "JWO-RECON-001",
            date.today() + timedelta(days=30),
            outwards=[15000, 9640],
            inwards=[(12000, 11800, 200)],
        )
        body = self._register()
        row = next(r for r in body["results"] if r["jwoNo"] == "JWO-RECON-001")
        self.assertEqual(row["totalOutward"], 24640)
        self.assertEqual(row["totalInward"], 12000)
        self.assertEqual(row["acceptedQty"], 11800)
        self.assertEqual(row["rejectedQty"], 200)
        self.assertEqual(row["pendingQty"], 12640)
        self.assertEqual(row["difference"], 12640)
        self.assertEqual(row["status"], "Pending")

    def test_inward_updates_reconciliation_automatically(self):
        order = self._make_jwo(
            "JWO-RECON-002",
            date.today() + timedelta(days=30),
            outwards=[24640],
            inwards=[(12000, 11800, 200)],
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-RECON-002"
        )
        self.assertEqual(row["pendingQty"], 12640)
        JWOInward.objects.create(
            client=self.client_obj,
            job_work_order=order,
            no="IN-EXTRA",
            date=date(2026, 4, 1),
            qty=5000,
            accepted=4950,
            rejected=50,
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-RECON-002"
        )
        self.assertEqual(row["totalInward"], 17000)
        self.assertEqual(row["acceptedQty"], 16750)
        self.assertEqual(row["rejectedQty"], 250)
        self.assertEqual(row["pendingQty"], 7640)

    def test_reconciled_when_fully_received(self):
        self._make_jwo(
            "JWO-RECON-003",
            date.today() + timedelta(days=30),
            outwards=[15000],
            inwards=[(15000, 14800, 200)],
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-RECON-003"
        )
        self.assertEqual(row["pendingQty"], 0)
        self.assertEqual(row["status"], "Reconciled")

    def test_mismatch_when_expected_date_passed(self):
        self._make_jwo(
            "JWO-RECON-004",
            date.today() - timedelta(days=1),
            outwards=[18000],
            inwards=[(16500, 16000, 500)],
        )
        row = next(
            r for r in self._register()["results"] if r["jwoNo"] == "JWO-RECON-004"
        )
        self.assertEqual(row["pendingQty"], 1500)
        self.assertEqual(row["status"], "Mismatch")

    def test_jwo_without_outward_is_excluded(self):
        self._make_jwo(
            "JWO-RECON-005",
            date.today() + timedelta(days=30),
            outwards=[],
            inwards=[],
        )
        body = self._register()
        self.assertFalse([r for r in body["results"] if r["jwoNo"] == "JWO-RECON-005"])


class ReprocessTests(TestCase):
    """Reprocess lines nest under the JWO like outwards/inwards."""

    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="reprocess-test-tenant", defaults={"name": "Reprocess Test Tenant"}
        )
        self.user = make_user(self.client_obj)
        api = APIClient()
        api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
        )
        self.api = api

    def _create_jwo(self, reprocesses=None):
        payload = {
            "process": "Dyeing",
            "vendor": "Vendor B",
            "orderDate": "2026-01-05",
            "plannedQty": 12000,
            "expectedCompletion": "2026-12-31",
            "materials": [
                {
                    "fabricItem": "Cotton Fabric",
                    "fabricQuality": "GSM 120",
                    "shade": "Navy Blue",
                    "qty": 12000,
                    "rate": 8,
                    "amount": 96000,
                }
            ],
            "outwards": [{"no": "OUT-1", "date": "2026-02-01", "qty": 12000}],
            "inwards": [
                {
                    "no": "IN-2026-015",
                    "date": "2026-03-01",
                    "qty": 12000,
                    "accepted": 11800,
                    "rejected": 200,
                }
            ],
            "reprocesses": reprocesses or [],
        }
        response = self.api.post("/api/v1/jobwork/orders/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content[:1000])
        return response.json()

    def test_nested_create_round_trip(self):
        body = self._create_jwo([
            {
                "no": "RP-2026-001",
                "inwardNo": "IN-2026-015",
                "date": "2026-10-15",
                "qty": 2000,
                "reason": "Shade mismatch",
                "expectedReturn": "2026-10-25",
                "status": "Sent to Vendor",
                "remarks": "Resend for redye",
            }
        ])
        self.assertEqual(len(body["reprocesses"]), 1)
        line = body["reprocesses"][0]
        self.assertEqual(line["no"], "RP-2026-001")
        self.assertEqual(line["inwardNo"], "IN-2026-015")
        self.assertEqual(float(line["qty"]), 2000)
        self.assertEqual(line["reason"], "Shade mismatch")
        self.assertEqual(line["expectedReturn"], "2026-10-25")
        self.assertEqual(line["status"], "Sent to Vendor")

    def test_patch_add_and_edit_reprocess(self):
        body = self._create_jwo([
            {
                "no": "RP-2026-001",
                "inwardNo": "IN-2026-015",
                "date": "2026-10-15",
                "qty": 2000,
                "reason": "Shade mismatch",
                "status": "Pending",
            }
        ])
        kept = body["reprocesses"][0]
        response = self.api.patch(
            f"/api/v1/jobwork/orders/{body['id']}/",
            {
                "reprocesses": [
                    {**kept, "qty": 1500, "status": "Sent to Vendor"},
                    {
                        "no": "RP-2026-002",
                        "inwardNo": "IN-2026-015",
                        "date": "2026-10-18",
                        "qty": 500,
                        "reason": "Print defect",
                        "status": "Pending",
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content[:1000])
        lines = {line["no"]: line for line in response.json()["reprocesses"]}
        self.assertEqual(set(lines), {"RP-2026-001", "RP-2026-002"})
        self.assertEqual(float(lines["RP-2026-001"]["qty"]), 1500)
        self.assertEqual(lines["RP-2026-001"]["status"], "Sent to Vendor")

    def test_patch_without_reprocesses_keeps_them(self):
        body = self._create_jwo([
            {
                "no": "RP-2026-001",
                "inwardNo": "IN-2026-015",
                "date": "2026-10-15",
                "qty": 2000,
                "reason": "Shade mismatch",
                "status": "Pending",
            }
        ])
        response = self.api.patch(
            f"/api/v1/jobwork/orders/{body['id']}/",
            {"remarks": "just a note"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertEqual(len(response.json()["reprocesses"]), 1)
