"""Discount authority and margin approvals on sales documents (PDF §12, §16).

Default policy: 1% auto-allowed, salesperson up to 2%, manager up to 5%,
owner above that or on any line below cost / minimum margin.
"""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import ApprovalRequest
from apps.inventory.models import LotTaka
from apps.masters.models import Item, Location, Party
from apps.sales.models import Quotation, SalesOrder


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class DiscountApprovalTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("disc"), name="Discount Tenant")
        sync_permissions()
        seed_roles(self.tenant)
        self.salesperson = self.user("SALES_EXEC")
        self.other_salesperson = self.user("SALES_EXEC")
        self.manager = self.user("SALES_MGR")
        self.owner = self.user("OWNER")

        self.customer = Party.objects.create(
            client=self.tenant, code=uid("C"), name="Radhe Textiles", type="Customer"
        )
        self.fabric = Item.objects.create(
            client=self.tenant, sku=uid("FAB"), name="Cotton 60x60", item_kind="Service", uom="Mtr"
        )

    def user(self, role_code):
        role = Role.objects.get(client=self.tenant, code=role_code)
        return User.objects.create_user(
            email=f"{uid(role_code.lower())}@example.com", password="pass",
            client=self.tenant, role=role, name=role_code.title(),
        )

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def lines(self, discount, rate=100):
        return [{"itemId": str(self.fabric.id), "qty": 10, "rate": rate, "tax": 0, "discount": discount}]

    def create_order(self, user, discount, rate=100):
        resp = self.api(user).post("/api/v1/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": self.lines(discount, rate),
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        return SalesOrder.objects.get(pk=resp.json()["id"])

    def pending_request(self, document):
        return ApprovalRequest.objects.get(
            client_id=self.tenant.id, document_id=str(document.id), status="Pending"
        )

    def decide(self, user, req, verb="approve"):
        return self.api(user).post(f"/api/v1/approvals/requests/{req.id}/{verb}/", {}, format="json")

    # -- sales orders -------------------------------------------------------------
    def test_discount_within_salesperson_limit_confirms(self):
        order = self.create_order(self.salesperson, discount=1.5)
        self.assertEqual(order.stage, "Confirmed")
        self.assertFalse(ApprovalRequest.objects.filter(document_id=str(order.id)).exists())

    def test_acceptance_salesperson_5pct_needs_manager_then_confirms(self):
        order = self.create_order(self.salesperson, discount=5)
        self.assertEqual(order.stage, "Pending Approval")
        req = self.pending_request(order)
        self.assertEqual(req.document_type, "discount_limit")
        self.assertEqual(req.new_values["approverRoleCode"], "SALES_MGR")
        self.assertEqual(req.rule.approver_role_code, "SALES_MGR")

        # Nothing can be dispatched while it waits.
        blocked = self.api(self.salesperson).post(
            f"/api/v1/sales/orders/{order.id}/convert-to-challan/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 409, blocked.content[:300])

        # Confirming it again by PATCH does not get round the gate.
        resp = self.api(self.salesperson).patch(
            f"/api/v1/sales/orders/{order.id}/", {"stage": "Confirmed"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.stage, "Pending Approval")

        # A fellow salesperson is not an approver.
        self.assertEqual(self.decide(self.other_salesperson, req).status_code, 403)

        resp = self.decide(self.manager, req)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.stage, "Confirmed")

    def test_manager_can_give_up_to_manager_limit_without_asking(self):
        order = self.create_order(self.manager, discount=4)
        self.assertEqual(order.stage, "Confirmed")

    def test_large_discount_needs_owner_not_manager(self):
        order = self.create_order(self.salesperson, discount=8)
        req = self.pending_request(order)
        self.assertEqual(req.new_values["approverRoleCode"], "OWNER")
        self.assertEqual(self.decide(self.manager, req).status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.stage, "Pending Approval")

        self.assertEqual(self.decide(self.owner, req).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.stage, "Confirmed")

    def test_acceptance_below_cost_line_needs_owner(self):
        location = Location.objects.create(
            client=self.tenant, code=uid("WH"), name="Godown", type="Warehouse"
        )
        LotTaka.objects.create(
            client=self.tenant, taka_id=uid("TAK"), lot_number=uid("LOT"), taka_number="1",
            item=self.fabric, location=location, received_quantity=Decimal("100"),
            available_quantity=Decimal("100"), status="Available", uom="Mtr", rate=Decimal("120"),
        )
        order = self.create_order(self.manager, discount=0, rate=100)
        self.assertEqual(order.stage, "Pending Approval")
        req = self.pending_request(order)
        self.assertEqual(req.document_type, "below_margin_sale")
        self.assertTrue(req.new_values["belowCost"])
        self.assertEqual(req.new_values["approverRoleCode"], "OWNER")
        self.assertEqual(self.decide(self.owner, req).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.stage, "Confirmed")

    def test_rejection_sends_order_back_to_draft(self):
        order = self.create_order(self.salesperson, discount=4)
        resp = self.decide(self.manager, self.pending_request(order), verb="reject")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.stage, "Draft")

    def test_requester_cannot_approve_own_request(self):
        order = self.create_order(self.manager, discount=8)  # owner level
        req = self.pending_request(order)
        self.assertEqual(self.decide(self.manager, req).status_code, 403)

    def test_discount_check_lists_lines(self):
        order = self.create_order(self.salesperson, discount=4)
        resp = self.api(self.salesperson).get(f"/api/v1/sales/orders/{order.id}/discount-check/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["requiredLevel"], "manager")
        self.assertEqual(body["lines"][0]["approverRoleCode"], "SALES_MGR")

    # -- quotations ---------------------------------------------------------------
    def test_quotation_send_is_held_until_approved(self):
        resp = self.api(self.salesperson).post("/api/v1/sales/quotations/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": self.lines(4),
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        quotation = Quotation.objects.get(pk=resp.json()["id"])

        sent = self.api(self.salesperson).post(
            f"/api/v1/sales/quotations/{quotation.id}/send/", {"channel": "whatsapp"}, format="json"
        )
        self.assertEqual(sent.status_code, 409, sent.content[:400])
        self.assertEqual(sent.json()["code"], "APPROVAL_REQUIRED")
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "Pending Approval")

        self.assertEqual(self.decide(self.manager, self.pending_request(quotation)).status_code, 200)
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "Draft")

        sent = self.api(self.salesperson).post(
            f"/api/v1/sales/quotations/{quotation.id}/send/", {"channel": "whatsapp"}, format="json"
        )
        self.assertEqual(sent.status_code, 200, sent.content[:400])

    def test_request_approval_at_save_keeps_the_salespersons_reason(self):
        resp = self.api(self.salesperson).post("/api/v1/sales/quotations/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": self.lines(4),
        }, format="json")
        quotation_id = resp.json()["id"]
        resp = self.api(self.salesperson).post(
            f"/api/v1/sales/quotations/{quotation_id}/request-approval/",
            {"reason": "Bulk order, long-term customer"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertTrue(resp.json()["needsApproval"])
        req = ApprovalRequest.objects.get(document_id=quotation_id, status="Pending")
        self.assertIn("Bulk order, long-term customer", req.reason)

        # Sending later re-runs the gate and keeps the reason.
        self.api(self.salesperson).post(
            f"/api/v1/sales/quotations/{quotation_id}/send/", {"channel": "whatsapp"}, format="json"
        )
        req.refresh_from_db()
        self.assertIn("Bulk order, long-term customer", req.reason)

        within = self.api(self.salesperson).post("/api/v1/sales/quotations/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": self.lines(1),
        }, format="json").json()["id"]
        resp = self.api(self.salesperson).post(
            f"/api/v1/sales/quotations/{within}/request-approval/", {}, format="json"
        )
        self.assertEqual(resp.json(), {"needsApproval": False})

    # -- invoices -----------------------------------------------------------------
    def test_direct_invoice_finalize_is_held(self):
        role = Role.objects.create(client=self.tenant, code=uid("BILL"), name="Biller")
        for permission_id in ("view_sales", "create_invoice", "finalize_invoice"):
            RolePermission.objects.create(role=role, permission_id=permission_id)
        biller = User.objects.create_user(
            email=f"{uid('bill')}@example.com", password="pass", client=self.tenant, role=role
        )
        created = self.api(biller).post("/api/v1/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "lineItems": self.lines(4),
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]
        resp = self.api(biller).post(f"/api/v1/sales/invoices/{invoice_id}/finalize/", {}, format="json")
        self.assertEqual(resp.status_code, 409, resp.content[:400])
        self.assertEqual(resp.json()["payload"]["approverRoleCode"], "SALES_MGR")

    # -- hand-made requests -----------------------------------------------------------
    def test_hand_made_request_cannot_name_its_approver(self):
        order = self.create_order(self.salesperson, discount=0)
        resp = self.api(self.salesperson).post("/api/v1/approvals/requests/", {
            "document_type": "discount_limit",
            "document_id": str(order.id),
            "document_number": order.order_number,
            "reason": "please",
            "new_values": {"approverRoleCodes": ["SALES_EXEC"], "system": True, "maxDiscountPct": "100"},
        }, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content[:400])
        req = ApprovalRequest.objects.get(pk=resp.json()["id"])
        self.assertNotIn("approverRoleCodes", req.new_values)
        self.assertNotIn("system", req.new_values)


class DiscountPolicyEndpointTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("policy"), name="Policy Tenant")
        sync_permissions()
        seed_roles(self.tenant)

    def api_as(self, role_code):
        role = Role.objects.get(client=self.tenant, code=role_code)
        user = User.objects.create_user(
            email=f"{uid('p')}@example.com", password="pass", client=self.tenant, role=role
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def test_get_shows_defaults_and_my_limit(self):
        resp = self.api_as("SALES_EXEC").get("/api/v1/approvals/discount-policy/")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["salespersonMaxPct"], "2.00")
        self.assertEqual(body["managerMaxPct"], "5.00")
        self.assertEqual(body["myLevel"], "salesperson")
        self.assertEqual(body["myMaxDiscountPct"], "2.00")
        self.assertEqual(body["max_discount_pct"], "2.00")

    def test_only_rule_admins_may_change_it(self):
        resp = self.api_as("SALES_EXEC").patch(
            "/api/v1/approvals/discount-policy/", {"salespersonMaxPct": 10}, format="json"
        )
        self.assertEqual(resp.status_code, 403)

        owner = self.api_as("OWNER")
        resp = owner.patch("/api/v1/approvals/discount-policy/", {
            "salespersonMaxPct": 3, "managerMaxPct": 8, "minMarginPct": 7,
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["salespersonMaxPct"], "3.00")

        # The older single-threshold shape still works.
        resp = owner.patch("/api/v1/approvals/discount-policy/", {
            "max_discount_pct": 4, "approver_role_code": "sales_manager",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["salespersonMaxPct"], "4.00")
        self.assertEqual(resp.json()["managerRoleCode"], "SALES_MGR")

    def test_limits_must_rise(self):
        resp = self.api_as("OWNER").patch(
            "/api/v1/approvals/discount-policy/", {"salespersonMaxPct": 9, "managerMaxPct": 5}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content[:300])
