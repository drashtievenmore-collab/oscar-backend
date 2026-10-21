from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import ApprovalRequest, ApprovalRule
from apps.core import approval_services
from apps.masters.models import Party, Item
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine


class RBACWorkflowsTestCase(TestCase):
    def setUp(self):
        self.client_entity = Client.objects.create(name="Oscar Textiles Ltd", slug="oscar-textiles")
        sync_permissions()
        seed_roles(self.client_entity)

        self.owner_role = Role.objects.get(client=self.client_entity, code="OWNER")
        self.admin_role = Role.objects.get(client=self.client_entity, code="SYS_ADMIN")
        self.purchase_officer_role = Role.objects.get(client=self.client_entity, code="PURCHASE_OFFICER")
        self.purchase_mgr_role = Role.objects.get(client=self.client_entity, code="PURCHASE_MGR")
        self.store_incharge_role = Role.objects.get(client=self.client_entity, code="STORE_INCHARGE")
        self.prod_coord_role = Role.objects.get(client=self.client_entity, code="PROD_COORD")
        self.prod_staff_role = Role.objects.get(client=self.client_entity, code="PROD_STAFF")
        self.qc_inspector_role = Role.objects.get(client=self.client_entity, code="QC_INSPECTOR")
        self.jobwork_mgr_role = Role.objects.get(client=self.client_entity, code="JOBWORK_MGR")
        self.accounts_exec_role = Role.objects.get(client=self.client_entity, code="ACCOUNTS_EXEC")
        self.accounts_mgr_role = Role.objects.get(client=self.client_entity, code="ACCOUNTS_MGR")
        self.sales_exec_role = Role.objects.get(client=self.client_entity, code="SALES_EXEC")
        self.logistics_role = Role.objects.get(client=self.client_entity, code="LOGISTICS")
        self.auditor_role = Role.objects.get(client=self.client_entity, code="AUDITOR")

        self.purchase_officer = User.objects.create_user(
            email="po@oscar.com",
            password="Password123!",
            name="Purchase Officer John",
            client=self.client_entity,
            role=self.purchase_officer_role,
        )

        self.purchase_mgr = User.objects.create_user(
            email="pm@oscar.com",
            password="Password123!",
            name="Purchase Manager Sarah",
            client=self.client_entity,
            role=self.purchase_mgr_role,
        )

        self.sys_admin = User.objects.create_user(
            email="admin@oscar.com",
            password="Password123!",
            name="System Admin Alex",
            client=self.client_entity,
            role=self.admin_role,
        )

        self.inactive_user = User.objects.create_user(
            email="inactive@oscar.com",
            password="Password123!",
            name="Inactive Employee",
            client=self.client_entity,
            role=self.purchase_officer_role,
            status="Inactive",
        )

        self.vendor = Party.objects.create(
            client=self.client_entity,
            name="Kiran Weaving Mills",
            type="Vendor",
        )
        self.fabric_item = Item.objects.create(
            client=self.client_entity,
            name="Cotton Grey 60x60",
            sku="GREY-6060",
            uom="MTR",
        )

    def test_fourteen_roles_seeded(self):
        expected_roles = [
            "SYS_ADMIN",
            "OWNER",
            "PURCHASE_OFFICER",
            "PURCHASE_MGR",
            "STORE_INCHARGE",
            "PROD_COORD",
            "PROD_STAFF",
            "QC_INSPECTOR",
            "JOBWORK_MGR",
            "ACCOUNTS_EXEC",
            "ACCOUNTS_MGR",
            "SALES_EXEC",
            "LOGISTICS",
            "AUDITOR",
        ]
        existing_codes = list(Role.objects.filter(client=self.client_entity).values_list("code", flat=True))
        for code in expected_roles:
            self.assertIn(code, existing_codes)

    def test_inactive_user_cannot_login(self):
        api = APIClient()
        res = api.post("/api/v1/auth/login/", {"email": "inactive@oscar.com", "password": "Password123!"})
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(res.data.get("code"), "ACCOUNT_INACTIVE")

    def test_active_user_login_and_me_hydration(self):
        api = APIClient()
        res = api.post("/api/v1/auth/login/", {"email": "po@oscar.com", "password": "Password123!"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        token = res.data.get("access")
        self.assertTrue(token)

        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        me_res = api.get("/api/v1/auth/me/")
        self.assertEqual(me_res.status_code, status.HTTP_200_OK)
        user_data = me_res.data.get("user")
        self.assertEqual(user_data.get("email"), "po@oscar.com")
        self.assertEqual(user_data.get("role", {}).get("code"), "PURCHASE_OFFICER")
        permissions = me_res.data.get("permissions")
        self.assertIn("create_purchase_order", permissions)
        self.assertNotIn("approve_purchase_order", permissions)

    def test_po_workflow_submit_and_separation_of_duties(self):
        po = PurchaseOrder.objects.create(
            client=self.client_entity,
            po_number="PO-TEST-001",
            party=self.vendor,
            doc_date=timezone.localdate(),
            status="Draft",
            created_by=self.purchase_officer,
        )
        PurchaseOrderLine.objects.create(
            client=self.client_entity,
            purchase_order=po,
            item=self.fabric_item,
            qty=Decimal("1000.00"),
            rate=Decimal("50.00"),
        )

        api_po = APIClient()
        login_po = api_po.post("/api/v1/auth/login/", {"email": "po@oscar.com", "password": "Password123!"})
        api_po.credentials(HTTP_AUTHORIZATION=f"Bearer {login_po.data['access']}")

        sub_res = api_po.post(f"/api/v1/purchase/orders/{po.id}/submit-for-approval/")
        self.assertEqual(sub_res.status_code, status.HTTP_200_OK)
        po.refresh_from_db()
        self.assertEqual(po.status, "Pending Approval")

        req = ApprovalRequest.objects.filter(
            client=self.client_entity,
            document_type="purchase_order",
            document_id=str(po.id),
        ).first()
        self.assertIsNotNone(req)
        self.assertEqual(req.status, "Pending")

        self_appr = api_po.post(f"/api/v1/purchase/orders/{po.id}/approve/")
        self.assertEqual(self_appr.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self_appr.data.get("code"), "SELF_APPROVAL_NOT_PERMITTED")

        api_pm = APIClient()
        login_pm = api_pm.post("/api/v1/auth/login/", {"email": "pm@oscar.com", "password": "Password123!"})
        api_pm.credentials(HTTP_AUTHORIZATION=f"Bearer {login_pm.data['access']}")

        pm_appr = api_pm.post(f"/api/v1/purchase/orders/{po.id}/approve/", {"remarks": "Verified and approved"})
        self.assertEqual(pm_appr.status_code, status.HTTP_200_OK)
        po.refresh_from_db()
        self.assertEqual(po.status, "Approved")

        req.refresh_from_db()
        self.assertEqual(req.status, "Approved")
        self.assertEqual(req.decided_by, self.purchase_mgr)

    def test_return_po_for_correction(self):
        po = PurchaseOrder.objects.create(
            client=self.client_entity,
            po_number="PO-TEST-002",
            party=self.vendor,
            doc_date=timezone.localdate(),
            status="Pending Approval",
            created_by=self.purchase_officer,
        )
        PurchaseOrderLine.objects.create(
            client=self.client_entity,
            purchase_order=po,
            item=self.fabric_item,
            qty=Decimal("500.00"),
            rate=Decimal("45.00"),
        )
        ApprovalRequest.objects.create(
            client=self.client_entity,
            document_type="purchase_order",
            document_id=str(po.id),
            document_number=po.po_number,
            requested_by=self.purchase_officer,
            status="Pending",
            reason="Initial submission",
        )

        api_pm = APIClient()
        login_pm = api_pm.post("/api/v1/auth/login/", {"email": "pm@oscar.com", "password": "Password123!"})
        api_pm.credentials(HTTP_AUTHORIZATION=f"Bearer {login_pm.data['access']}")

        ret_res = api_pm.post(
            f"/api/v1/purchase/orders/{po.id}/return-for-correction/",
            {"remarks": "Rate is too high, re-negotiate with supplier."},
        )
        self.assertEqual(ret_res.status_code, status.HTTP_200_OK)
        po.refresh_from_db()
        self.assertEqual(po.status, "Draft")

        req = ApprovalRequest.objects.get(
            client=self.client_entity,
            document_type="purchase_order",
            document_id=str(po.id),
        )
        self.assertEqual(req.status, "Returned")
        self.assertEqual(req.decided_by, self.purchase_mgr)

    def test_central_approval_inbox_endpoints(self):
        req = ApprovalRequest.objects.create(
            client=self.client_entity,
            document_type="purchase_order",
            document_id="TEST-PO-CENTRAL",
            document_number="PO-CENTRAL-001",
            requested_by=self.purchase_officer,
            status="Pending",
            reason="High value purchase order",
        )

        api_pm = APIClient()
        login_pm = api_pm.post("/api/v1/auth/login/", {"email": "pm@oscar.com", "password": "Password123!"})
        api_pm.credentials(HTTP_AUTHORIZATION=f"Bearer {login_pm.data['access']}")

        badge_res = api_pm.get("/api/v1/approvals/requests/badge/")
        self.assertEqual(badge_res.status_code, status.HTTP_200_OK)

        ret_res = api_pm.post(f"/api/v1/approvals/requests/{req.id}/return-for-correction/", {"comments": "Needs correction"})
        self.assertEqual(ret_res.status_code, status.HTTP_200_OK)
        req.refresh_from_db()
        self.assertEqual(req.status, "Returned")

    def test_system_admin_approval_restriction(self):
        po = PurchaseOrder.objects.create(
            client=self.client_entity,
            po_number="PO-TEST-003",
            party=self.vendor,
            doc_date=timezone.localdate(),
            status="Pending Approval",
            created_by=self.purchase_officer,
        )
        PurchaseOrderLine.objects.create(
            client=self.client_entity,
            purchase_order=po,
            item=self.fabric_item,
            qty=Decimal("200.00"),
            rate=Decimal("60.00"),
        )

        api_admin = APIClient()
        login_admin = api_admin.post("/api/v1/auth/login/", {"email": "admin@oscar.com", "password": "Password123!"})
        api_admin.credentials(HTTP_AUTHORIZATION=f"Bearer {login_admin.data['access']}")

        res = api_admin.post(f"/api/v1/purchase/orders/{po.id}/approve/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
