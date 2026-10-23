"""Tests for RateMaster, PriceList, Sales/Purchase line rate defaulting,
margin control min selling rate, and purchase rate change approvals (PDF §01, §06, §11, §16)."""
import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import ApprovalRequest, ApprovalRule
from apps.masters.models import (
    Design,
    FabricQuality,
    Item,
    Location,
    Party,
    RateMaster,
    Shade,
)
from apps.purchase.models import PurchaseOrder
from apps.sales.models import Quotation, SalesOrder


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class RateMasterBaseTestCase(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("rm-tenant"), name="Rate Master Tenant")
        sync_permissions()
        seed_roles(self.tenant)

        self.today = timezone.localdate()
        self.customer = Party.objects.create(
            client=self.tenant, code=uid("CUST"), name="Anoop Textiles", type="Customer"
        )
        self.vendor = Party.objects.create(
            client=self.tenant, code=uid("VEND"), name="Surat Grey Mills", type="Vendor"
        )
        self.quality = FabricQuality.objects.create(
            client=self.tenant, code=uid("Q"), name="Cotton 60x60 Cambric"
        )
        self.design = Design.objects.create(
            client=self.tenant, code=uid("D"), name="Floral Print D-101"
        )
        self.shade = Shade.objects.create(
            client=self.tenant, code=uid("S"), name="Navy Blue S-05"
        )
        self.item = Item.objects.create(
            client=self.tenant,
            sku=uid("SKU"),
            name="Cotton 60x60 Cambric Navy",
            item_kind="Fabric",
            uom="Meter",
            cost_price=Decimal("80"),
            selling_price=Decimal("120"),
        )
        self.location = Location.objects.create(
            client=self.tenant, code=uid("WH"), name="Main Godown", type="Warehouse"
        )

        self.clerk = self.user_with(
            "view_masters", "create_purchase_order", "view_purchase",
            "view_sales", "create_quotation", "create_sales_order",
        )
        self.purchase_mgr = self.user_in("PURCHASE_MGR")
        self.sales_mgr = self.user_in("SALES_MGR")
        self.owner = self.user_in("OWNER")

    def user_with(self, *permission_ids):
        role = Role.objects.create(client=self.tenant, code=uid("R"), name=uid("Role"))
        for pid in permission_ids:
            RolePermission.objects.create(role=role, permission_id=pid)
        return User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant, role=role
        )

    def user_in(self, role_code):
        role = Role.objects.get(client=self.tenant, code=role_code)
        return User.objects.create_user(
            email=f"{uid(role_code.lower())}@example.com", password="pass", client=self.tenant, role=role
        )

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def decide(self, user, req_id, verb="approve"):
        return self.api(user).post(f"/api/v1/approvals/requests/{req_id}/{verb}/", {}, format="json")


class RateMasterCRUDTests(RateMasterBaseTestCase):
    def test_create_and_lookup_rate_master(self):
        client = self.api(self.owner)

        # 1. Create Sales RateMaster with min selling rate
        create_resp = client.post("/api/v1/masters/rate-masters/", {
            "rateType": "Sales",
            "partyId": str(self.customer.id),
            "fabricQualityId": str(self.quality.id),
            "designId": str(self.design.id),
            "shadeId": str(self.shade.id),
            "rate": "150.00",
            "minSellingRate": "135.00",
            "uom": "Meter",
            "validFrom": str(self.today),
            "notes": "Special party price contract",
        }, format="json")
        self.assertEqual(create_resp.status_code, 201, create_resp.content[:400])
        rm_id = create_resp.json()["id"]

        # 2. Retrieve
        get_resp = client.get(f"/api/v1/masters/rate-masters/{rm_id}/")
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(Decimal(str(get_resp.json()["rate"])), Decimal("150.00"))
        self.assertEqual(Decimal(str(get_resp.json()["minSellingRate"])), Decimal("135.00"))

        # 3. Lookup endpoint
        lookup_resp = client.get("/api/v1/masters/rate-masters/lookup/", {
            "rateType": "Sales",
            "partyId": str(self.customer.id),
            "fabricQualityId": str(self.quality.id),
            "designId": str(self.design.id),
            "shadeId": str(self.shade.id),
        })
        self.assertEqual(lookup_resp.status_code, 200)
        self.assertTrue(lookup_resp.json()["found"])
        self.assertEqual(Decimal(str(lookup_resp.json()["rate"])), Decimal("150.00"))
        self.assertEqual(Decimal(str(lookup_resp.json()["minSellingRate"])), Decimal("135.00"))

        # 4. Update
        patch_resp = client.patch(f"/api/v1/masters/rate-masters/{rm_id}/", {
            "rate": "155.00",
        }, format="json")
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(Decimal(str(patch_resp.json()["rate"])), Decimal("155.00"))

        # 5. Delete
        del_resp = client.delete(f"/api/v1/masters/rate-masters/{rm_id}/")
        self.assertEqual(del_resp.status_code, 204)
        self.assertFalse(RateMaster.objects.filter(id=rm_id, deleted_at__isnull=True).exists())


class SalesDocumentRateMasterTests(RateMasterBaseTestCase):
    def setUp(self):
        super().setUp()
        # Seed standard sales rate
        self.sales_rm = RateMaster.objects.create(
            client=self.tenant,
            rate_type="Sales",
            party=self.customer,
            item=self.item,
            rate=Decimal("150.00"),
            min_selling_rate=Decimal("130.00"),
            uom="Meter",
            valid_from=self.today,
        )

    def test_sales_order_line_defaults_from_rate_master(self):
        client = self.api(self.clerk)
        resp = client.post("/api/v1/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(self.today),
            "lineItems": [
                {
                    "itemId": str(self.item.id),
                    "qty": 50,
                    # rate is 0 / omitted: should pick up 150.00 from RateMaster
                    "rate": 0,
                    "discount": 0,
                    "tax": 5,
                }
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        order = SalesOrder.objects.get(id=resp.json()["id"])
        line = order.line_items.first()
        self.assertEqual(line.rate, Decimal("150.00"))
        self.assertEqual(order.subtotal, Decimal("7500.00"))  # 50 * 150

    def test_quotation_line_defaults_from_rate_master(self):
        client = self.api(self.clerk)
        resp = client.post("/api/v1/sales/quotations/", {
            "partyId": str(self.customer.id),
            "date": str(self.today),
            "lineItems": [
                {
                    "itemId": str(self.item.id),
                    "qty": 20,
                    "rate": 0,
                    "discount": 0,
                    "tax": 5,
                }
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        quote = Quotation.objects.get(id=resp.json()["id"])
        line = quote.line_items.first()
        self.assertEqual(line.rate, Decimal("150.00"))

    def test_min_selling_rate_feeds_margin_control_and_needs_owner_approval(self):
        # Min selling rate is 130.00. Salesperson sells at 110.00 (< 130.00).
        client = self.api(self.clerk)
        resp = client.post("/api/v1/sales/orders/", {
            "partyId": str(self.customer.id),
            "date": str(self.today),
            "lineItems": [
                {
                    "itemId": str(self.item.id),
                    "qty": 100,
                    "rate": 110,  # Below min selling rate 130.00
                    "discount": 0,
                    "tax": 5,
                }
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        order = SalesOrder.objects.get(id=resp.json()["id"])
        # Should be gated by margin control
        self.assertEqual(order.stage, "Pending Approval")
        line = order.line_items.first()
        self.assertTrue(line.below_margin or line.below_cost)

        # Check pending approval request exists addressing OWNER
        req = ApprovalRequest.objects.get(
            client=self.tenant,
            document_type="below_margin_sale",
            document_id=str(order.id),
            status="Pending",
        )
        self.assertEqual(req.new_values.get("approverRoleCode"), "OWNER")

        # Owner approves
        decide_resp = self.decide(self.owner, req.id, "approve")
        self.assertEqual(decide_resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.stage, "Confirmed")


class PurchaseOrderRateToleranceTests(RateMasterBaseTestCase):
    def setUp(self):
        super().setUp()
        # Seed standard purchase rate
        self.purchase_rm = RateMaster.objects.create(
            client=self.tenant,
            rate_type="Purchase",
            party=self.vendor,
            item=self.item,
            rate=Decimal("60.00"),
            uom="Meter",
            valid_from=self.today,
        )

    def test_po_line_defaults_from_purchase_rate_master(self):
        client = self.api(self.clerk)
        resp = client.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.vendor.id),
            "date": str(self.today),
            "lineItems": [
                {
                    "itemId": str(self.item.id),
                    "qty": 1000,
                    "rate": 0,  # Should pick up 60.00
                    "tax": 5,
                }
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        po = PurchaseOrder.objects.get(id=resp.json()["id"])
        line = po.line_items.first()
        self.assertEqual(line.rate, Decimal("60.00"))
        # Within standard rate: no approval needed
        self.assertEqual(po.status, "Draft")
        self.assertFalse(ApprovalRequest.objects.filter(document_id=str(po.id), status="Pending").exists())

    def test_po_rate_increase_above_tolerance_triggers_approval_and_holds_po(self):
        # Create tolerance rule: 5% tolerance, approver: PURCHASE_MGR
        ApprovalRule.objects.create(
            client=self.tenant,
            document_type="purchase_order_rate",
            name="PO Rate Variance > 5%",
            condition_type="variance_percent",
            threshold_value=Decimal("5.00"),
            approver_role_code="PURCHASE_MGR",
            is_active=True,
        )

        client = self.api(self.clerk)
        # Standard rate is 60.00. 5% tolerance is 63.00. Clerk enters 70.00 (+16.67% > 5%)
        resp = client.post("/api/v1/purchase/orders/", {
            "vendorId": str(self.vendor.id),
            "date": str(self.today),
            "lineItems": [
                {
                    "itemId": str(self.item.id),
                    "qty": 1000,
                    "rate": 70,  # Exceeds 60 by 16.67%
                    "tax": 5,
                }
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:600])
        po = PurchaseOrder.objects.get(id=resp.json()["id"])
        self.assertEqual(po.status, "Pending Approval")

        # Approval request was created
        req = ApprovalRequest.objects.get(
            client=self.tenant,
            document_type="purchase_order_rate",
            document_id=str(po.id),
            status="Pending",
        )
        self.assertEqual(req.new_values.get("approverRoleCode"), "PURCHASE_MGR")
        self.assertIn("exceeds standard purchase rate", req.reason)

        # Direct approve on PO is blocked because of pending approval request
        blocked_resp = self.api(self.purchase_mgr).post(f"/api/v1/purchase/orders/{po.id}/approve/", {}, format="json")
        self.assertIn(blocked_resp.status_code, (400, 409))

        # Purchase Manager approves via approval inbox
        decide_resp = self.decide(self.purchase_mgr, req.id, "approve")
        self.assertEqual(decide_resp.status_code, 200)

        po.refresh_from_db()
        self.assertEqual(po.status, "Approved")
