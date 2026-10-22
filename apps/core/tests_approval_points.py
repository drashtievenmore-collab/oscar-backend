"""The PDF §16 approval points routed through the generic approval engine.

Stock adjustment above the limit, brokerage rate change, incentive manual
adjustment, debit/credit notes and vendor bill mismatch. Each test raises
the request as one user and decides it as another, through the inbox API.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import ApprovalRequest
from apps.inventory.models import StockMovement
from apps.masters.models import Broker, Item, Location, Party
from apps.sales.models import BrokerCommission, SalesInvoice


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class ApprovalPointBase(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("ap"), name="Approval Points Tenant")
        sync_permissions()
        seed_roles(self.tenant)
        ledger.seed_chart_of_accounts(self.tenant)
        self.today = timezone.localdate()
        self.customer = Party.objects.create(client=self.tenant, code=uid("C"), name="Radhe Textiles", type="Customer")
        self.vendor = Party.objects.create(client=self.tenant, code=uid("V"), name="Grey Supplier", type="Vendor")
        self.location = Location.objects.create(client=self.tenant, code=uid("WH"), name="Godown", type="Warehouse")
        self.item = Item.objects.create(
            client=self.tenant, sku=uid("FAB"), name="Cotton", item_kind="Standalone", uom="Mtr",
            cost_price=Decimal("50"),
        )

    def user_with(self, *permission_ids):
        role = Role.objects.create(client=self.tenant, code=uid("R"), name=uid("Role"))
        for permission_id in permission_ids:
            RolePermission.objects.create(role=role, permission_id=permission_id)
        return User.objects.create_user(email=f"{uid('u')}@example.com", password="pass", client=self.tenant, role=role)

    def user_in(self, role_code):
        return User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant,
            role=Role.objects.get(client=self.tenant, code=role_code),
        )

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def decide(self, user, req_id, verb="approve"):
        return self.api(user).post(f"/api/v1/approvals/requests/{req_id}/{verb}/", {}, format="json")


@override_settings(STOCK_ADJUSTMENT_APPROVAL_LIMIT="1000")
class StockAdjustmentApprovalTests(ApprovalPointBase):
    def adjust(self, user, qty):
        return self.api(user).post("/api/v1/inventory/adjustments/", {
            "itemId": str(self.item.id), "locationId": str(self.location.id),
            "quantity": qty, "reason": "Physical count",
        }, format="json")

    def test_small_adjustment_posts_at_once(self):
        resp = self.adjust(self.user_with("adjust_stock"), 10)  # 10 x 50 = 500
        self.assertEqual(resp.status_code, 201, resp.content[:400])

    def test_large_adjustment_waits_then_posts_on_approval(self):
        clerk = self.user_with("adjust_stock")
        resp = self.adjust(clerk, 100)  # 100 x 50 = 5,000 > 1,000
        self.assertEqual(resp.status_code, 202, resp.content[:400])
        self.assertTrue(resp.json()["pendingApproval"])
        self.assertFalse(StockMovement.objects.filter(client_id=self.tenant.id, type="ADJUSTMENT").exists())

        req_id = resp.json()["approvalRequestId"]
        self.assertEqual(self.decide(self.user_in("SALES_MGR"), req_id).status_code, 403)
        resp = self.decide(self.user_in("INV_MGR"), req_id)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        movement = StockMovement.objects.get(client_id=self.tenant.id, type="ADJUSTMENT")
        self.assertEqual(movement.quantity, Decimal("100"))

    def test_rejected_adjustment_never_posts(self):
        resp = self.adjust(self.user_with("adjust_stock"), 100)
        self.assertEqual(self.decide(self.user_in("INV_MGR"), resp.json()["approvalRequestId"], "reject").status_code, 200)
        self.assertFalse(StockMovement.objects.filter(client_id=self.tenant.id, type="ADJUSTMENT").exists())


class BrokerageRateChangeTests(ApprovalPointBase):
    def setUp(self):
        super().setUp()
        self.broker = Broker.objects.create(
            client=self.tenant, code=uid("BRK"), name="Mehta Brokers", commission_type="percent",
            default_value=Decimal("2"),
        )
        self.invoice = SalesInvoice.objects.create(
            client=self.tenant, invoice_number=uid("INV"), party=self.customer, doc_date=self.today,
            due_date=self.today + timedelta(days=30), status="Unpaid",
            subtotal=Decimal("10000"), taxable_value=Decimal("10000"), total=Decimal("10000"),
        )
        self.commission = BrokerCommission.objects.create(
            client=self.tenant, commission_number="BRK-COMM-1", broker=self.broker, invoice=self.invoice,
            party=self.customer, invoice_date=self.today, invoice_amount=Decimal("10000"),
            commission_type="percent", rate=Decimal("2"), commission_amount=Decimal("200"), status="Accrued",
        )
        self.clerk = self.user_with("view_sales", "manage_brokers", "view_masters")

    def test_view_only_user_cannot_edit_commission(self):
        resp = self.api(self.user_with("view_sales")).patch(
            f"/api/v1/sales/broker-commissions/{self.commission.id}/", {"rate": 5}, format="json"
        )
        self.assertEqual(resp.status_code, 403)

    def test_status_and_amount_are_not_patchable(self):
        resp = self.api(self.clerk).patch(
            f"/api/v1/sales/broker-commissions/{self.commission.id}/", {"status": "Paid"}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content[:300])

    def test_commission_rate_change_applies_after_approval(self):
        resp = self.api(self.clerk).patch(
            f"/api/v1/sales/broker-commissions/{self.commission.id}/", {"rate": 3}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        req_id = resp.json()["pendingApproval"]["approvalRequestId"]
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.rate, Decimal("2"))

        self.assertEqual(self.decide(self.user_in("SALES_MGR"), req_id).status_code, 200)
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.rate, Decimal("3"))
        self.assertEqual(self.commission.commission_amount, Decimal("300"))
        self.assertEqual(self.commission.commission_number, "BRK-COMM-1")

    def test_broker_default_rate_change_waits_other_fields_save(self):
        resp = self.api(self.clerk).patch(
            f"/api/v1/masters/brokers/{self.broker.id}/", {"defaultValue": 4, "phone": "9999999999"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertIn("pendingApproval", resp.json())
        self.broker.refresh_from_db()
        self.assertEqual(self.broker.default_value, Decimal("2"))
        self.assertEqual(self.broker.phone, "9999999999")

        req = ApprovalRequest.objects.get(document_id=str(self.broker.id), status="Pending")
        self.assertEqual(self.decide(self.user_in("OWNER"), req.id).status_code, 200)
        self.broker.refresh_from_db()
        self.assertEqual(self.broker.default_value, Decimal("4"))


class IncentiveAdjustmentTests(ApprovalPointBase):
    def setUp(self):
        super().setUp()
        from apps.hrms.models import Employee, SalespersonIncentiveAccrual

        employee = Employee.objects.create(
            client=self.tenant, employee_code=uid("EMP"), name="Suresh", email=f"{uid('e')}@example.com",
            joining_date="2026-01-01", employment_type="Regular", status="Active",
        )
        self.accrual = SalespersonIncentiveAccrual.objects.create(
            client=self.tenant, employee=employee, period_type="QUARTERLY", period_name="Q2 FY27",
            start_date="2026-07-01", end_date="2026-09-30", calculated_incentive=Decimal("5000"),
            status="ACCRUED",
        )
        self.hr = self.user_with("view_staff", "edit_staff")

    def test_manual_amount_change_waits_for_approval(self):
        resp = self.api(self.hr).patch(
            f"/api/v1/hrms/incentives/{self.accrual.id}/",
            {"calculatedIncentive": 6500, "adjustmentReason": "Festival target bonus"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        req_id = resp.json()["pendingApproval"]["approvalRequestId"]
        self.accrual.refresh_from_db()
        self.assertEqual(self.accrual.calculated_incentive, Decimal("5000"))

        self.assertEqual(self.decide(self.user_in("ACCOUNTS_MGR"), req_id).status_code, 200)
        self.accrual.refresh_from_db()
        self.assertEqual(self.accrual.calculated_incentive, Decimal("6500"))

    def test_status_is_not_patchable_and_approve_needs_payroll_permission(self):
        resp = self.api(self.hr).patch(
            f"/api/v1/hrms/incentives/{self.accrual.id}/", {"status": "PAID"}, format="json"
        )
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        resp = self.api(self.hr).post(f"/api/v1/hrms/incentives/{self.accrual.id}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 403, resp.content[:300])

    def test_production_incentive_actions_need_verify_production(self):
        viewer = self.user_with("view_production")
        resp = self.api(viewer).post(f"/api/v1/production/incentive-calcs/{uuid.uuid4()}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 403, resp.content[:300])


class StandaloneNoteApprovalTests(ApprovalPointBase):
    def create_note(self, user):
        return self.api(user).post("/api/v1/accounts/standalone-notes/", {
            "partyId": str(self.customer.id), "noteType": "CREDIT_NOTE", "taxableAmount": 1000,
            "againstDocumentType": "MANUAL", "reasonNotes": "Rate difference",
        }, format="json")

    def test_auditor_cannot_raise_a_note(self):
        self.assertEqual(self.create_note(self.user_in("AUDITOR")).status_code, 403)

    def test_note_must_be_approved_before_posting(self):
        clerk = self.user_with("view_ledger", "manage_journal_entries")
        resp = self.create_note(clerk)
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        note_id = resp.json()["id"]

        resp = self.api(clerk).post(f"/api/v1/accounts/standalone-notes/{note_id}/post/", {}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content[:300])

        resp = self.api(clerk).post(f"/api/v1/accounts/standalone-notes/{note_id}/submit/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertEqual(resp.json()["status"], "Pending Approval")
        req_id = resp.json()["approvalRequestId"]

        self.assertEqual(self.decide(self.user_in("SALES_MGR"), req_id).status_code, 403)
        self.assertEqual(self.decide(self.user_in("ACCOUNTS_MGR"), req_id).status_code, 200)

        resp = self.api(clerk).post(f"/api/v1/accounts/standalone-notes/{note_id}/post/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertEqual(resp.json()["status"], "Posted")


class VendorBillMismatchTests(ApprovalPointBase):
    def make_mismatched_bill(self):
        from apps.purchase.models import PurchaseOrder, PurchaseOrderLine, VendorBill, VendorBillLine

        po = PurchaseOrder.objects.create(
            client=self.tenant, po_number=uid("PO"), party=self.vendor, doc_date=self.today, status="Approved",
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=po, line_no=1, item=self.item,
            qty=Decimal("1000"), rate=Decimal("45"), tax_pct=Decimal("5"), uom="Mtr",
        )
        bill = VendorBill.objects.create(
            client=self.tenant, vendor_bill_number=uid("VB"), bill_date=self.today, party=self.vendor,
            party_name=self.vendor.name, purchase_order=po, gst_pct=Decimal("5"), subtotal=Decimal("50000"),
            gst_amount=Decimal("2500"), total=Decimal("52500"), status="Pending",
        )
        VendorBillLine.objects.create(
            client=self.tenant, vendor_bill=bill, line_no=1, item=self.item, item_name=self.item.name,
            qty=Decimal("1000"), rate=Decimal("50"), amount=Decimal("50000"), line_total=Decimal("52500"),
        )
        return bill

    def test_mismatch_needs_accounts_manager(self):
        bill = self.make_mismatched_bill()
        clerk = self.user_with("view_purchase", "create_bill")
        url = f"/api/v1/purchase/vendor-bills/{bill.id}/approve/"

        resp = self.api(clerk).post(url, {"remarks": "Rate agreed on call"}, format="json")
        self.assertEqual(resp.status_code, 409, resp.content[:400])
        self.assertEqual(resp.json()["code"], "APPROVAL_REQUIRED")
        bill.refresh_from_db()
        self.assertEqual(bill.status, "Pending")

        req_id = resp.json()["payload"]["approvalRequestId"]
        self.assertEqual(self.decide(self.user_in("ACCOUNTS_MGR"), req_id).status_code, 200)

        resp = self.api(clerk).post(url, {"remarks": "Rate agreed on call"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        bill.refresh_from_db()
        self.assertEqual(bill.status, "Approved")

    def test_holder_of_approve_bill_mismatch_approves_directly(self):
        bill = self.make_mismatched_bill()
        resp = self.api(self.user_in("ACCOUNTS_MGR")).post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/approve/", {"remarks": "Checked with supplier"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
