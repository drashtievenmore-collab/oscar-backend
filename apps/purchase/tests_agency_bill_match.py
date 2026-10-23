"""Tests for Agency Bill Matching (Original PO + Final Job Work PO + Vendor Invoice).

Run with::

    python manage.py test apps.purchase.tests_agency_bill_match --keepdb
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.exceptions import PermissionDenied, ValidationFailed
from apps.masters.models import FabricQuality, Item, Location, Party
from apps.pms.models import ProductionInstruction
from apps.pms import production_services
from apps.purchase import services
from apps.purchase.models import PurchaseBill, PurchaseOrder, PurchaseOrderLine, VendorBill, VendorBillLine


class AgencyBillMatchTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Oscar Textiles Client", slug="oscar-tex")
        self.admin = User.objects.create_user(
            email="admin@example.com", password="secretpassword",
            client=self.tenant, is_superuser=True, name="Admin User"
        )
        self.accounts_exec = User.objects.create_user(
            email="accounts@example.com", password="secretpassword",
            client=self.tenant, is_superuser=False, name="Accounts Exec"
        )
        from apps.accounts.models import Permission, Role, RolePermission
        acct_role = Role.objects.create(client=self.tenant, code="acct_exec", name="Accounts Executive")
        p_create, _ = Permission.objects.get_or_create(id="create_bill", defaults={"label": "Create Bill", "module": "purchase", "group": "purchase"})
        p_view, _ = Permission.objects.get_or_create(id="view_purchase", defaults={"label": "View Purchase", "module": "purchase", "group": "purchase"})
        RolePermission.objects.create(role=acct_role, permission=p_create)
        RolePermission.objects.create(role=acct_role, permission=p_view)
        self.accounts_exec.role = acct_role
        self.accounts_exec.save()
        from apps.accounting.services import seed_chart_of_accounts
        seed_chart_of_accounts(self.tenant)

        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        self.quality = FabricQuality.objects.create(client=self.tenant, name="Velvet Grey", code="VG01")
        self.fabric = Item.objects.create(
            client=self.tenant, sku="GREY-VG01", name="Velvet Grey Fabric", uom="Meter",
            item_kind="Standalone", fabric_quality=self.quality, cost_price=Decimal("5.50"),
        )
        self.agency = Party.objects.create(
            client=self.tenant, name="Vendor A External Agency", code="AG-VEND-A", type="Vendor",
        )
        self.godown = Location.objects.create(
            client=self.tenant, name="Main Godown", code="WH-MAIN", type="Warehouse",
        )

        # 1. Create Original PO: 25,000 M @ ₹5.50/M
        self.orig_po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number="PO-2026-0099",
            party=self.agency,
            doc_date="2026-10-01",
            purchase_type="Vendor A Production",
            status="Approved",
            created_by=self.admin,
        )
        self.orig_line = PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=self.orig_po,
            line_no=1,
            item=self.fabric,
            description="Velvet Grey Fabric",
            qty=Decimal("25000.00"),
            rate=Decimal("5.50"),
            tax_pct=Decimal("5.00"),
            uom="Meter",
        )
        from apps.sales.services import recalculate_document
        recalculate_document(self.orig_po)

        # 2. Create Production Instruction
        from apps.hrms.models import Employee
        self.employee = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-001",
            name="Ramesh Kumar",
            email="ramesh@example.com",
            joining_date="2026-01-01",
            employment_type="Regular",
            status="Active",
        )
        from apps.masters.models import Agency
        self.agency_obj = Agency.objects.create(
            client=self.tenant, name="Vendor A External Agency", code="AG-VEND-A"
        )
        self.pi = ProductionInstruction.objects.create(
            client=self.tenant,
            instruction_number="PROD-2026-0099",
            agency_name=self.agency.name,
            agency=self.agency_obj,
            order_reference=self.orig_po.po_number,
            order_meter=Decimal("25000.00"),
            fabric=self.fabric.name,
            agreed_job_rate=Decimal("5.50"),
            employee=self.employee,
            status="Verified",
        )

        # 3. Create Final Job Work PO: verified accepted quantity = 24,640 M (shortage: 360 M)
        self.final_po = PurchaseOrder.objects.create(
            client=self.tenant,
            po_number="FJWPO-2026-0099",
            party=self.agency,
            doc_date="2026-10-15",
            purchase_type="Final Job Work",
            reference_number=self.pi.instruction_number,
            status="Approved",
            created_by=self.admin,
        )
        self.final_line = PurchaseOrderLine.objects.create(
            client=self.tenant,
            purchase_order=self.final_po,
            line_no=1,
            item=self.fabric,
            description="Processing Job Work - Velvet Grey Fabric",
            qty=Decimal("24640.00"),
            rate=Decimal("5.50"),
            tax_pct=Decimal("5.00"),
            uom="Meter",
        )
        recalculate_document(self.final_po)
        self.pi.final_po = self.final_po
        self.pi.status = "PO Created"
        self.pi.save()

    def test_agency_three_way_match_perfect_match(self):
        """Invoice exactly matching Final Job Work PO: 24,640 M @ ₹5.50 + 5% GST."""
        bill = VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number="INV-AGENCY-001",
            bill_date="2026-10-16",
            party=self.agency,
            party_name=self.agency.name,
            purchase_order=self.orig_po,
            gst_pct=Decimal("5.00"),
            subtotal=Decimal("135520.00"),  # 24,640 * 5.50
            gst_amount=Decimal("6776.00"),  # 5% of 135,520
            total=Decimal("142296.00"),     # 135,520 + 6,776
            status="Pending",
        )
        VendorBillLine.objects.create(
            client=self.tenant,
            vendor_bill=bill,
            line_no=1,
            item=self.fabric,
            item_name=self.fabric.name,
            qty=Decimal("24640.00"),
            rate=Decimal("5.50"),
            amount=Decimal("135520.00"),
            line_total=Decimal("142296.00"),
        )

        res = services.compute_vendor_bill_matching(self.tenant.id, bill)
        self.assertTrue(res["isAgencyMatch"])
        self.assertEqual(res["overallStatus"], "Match")
        self.assertTrue(res["summary"]["qtyMatch"])
        self.assertTrue(res["summary"]["rateMatch"])
        self.assertTrue(res["summary"]["totalMatch"])
        # Legitimate production shortage: 25,000 - 24,640 = 360 M
        self.assertEqual(res["summary"]["productionShortage"], 360.0)
        self.assertTrue(res["summary"]["isLegitimateShortage"])

        # Check 14-field comparison rows
        fields = [r["field"] for r in res["threeWayReconciliation"]]
        self.assertIn("Vendor / Agency", fields)
        self.assertIn("Original PO Reference", fields)
        self.assertIn("Final Job Work PO Reference", fields)
        self.assertIn("Originally Instructed Quantity", fields)
        self.assertIn("Final PO Quantity", fields)
        self.assertIn("Invoice Quantity", fields)
        self.assertIn("Invoice Rate", fields)
        self.assertIn("Invoice Total", fields)

    def test_agency_three_way_match_quantity_mismatch(self):
        """Agency invoices for 25,000 M instead of the verified 24,640 M on Final PO."""
        bill = VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number="INV-AGENCY-MISMATCH-QTY",
            bill_date="2026-10-16",
            party=self.agency,
            party_name=self.agency.name,
            purchase_order=self.orig_po,
            gst_pct=Decimal("5.00"),
            subtotal=Decimal("137500.00"),  # 25,000 * 5.50
            gst_amount=Decimal("6875.00"),
            total=Decimal("144375.00"),
            status="Pending",
        )
        VendorBillLine.objects.create(
            client=self.tenant,
            vendor_bill=bill,
            line_no=1,
            item=self.fabric,
            qty=Decimal("25000.00"),  # Mismatched!
            rate=Decimal("5.50"),
            amount=Decimal("137500.00"),
            line_total=Decimal("144375.00"),
        )

        res = services.compute_vendor_bill_matching(self.tenant.id, bill)
        self.assertEqual(res["overallStatus"], "Mismatch")
        self.assertFalse(res["summary"]["qtyMatch"])

    def test_duplicate_invoice_prevention(self):
        """Preventing duplicate vendor invoice number for the same agency."""
        VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number="INV-DUPLICATE-CHECK",
            bill_date="2026-10-16",
            party=self.agency,
            status="Pending",
        )

        # Attempt to create duplicate via agency_match endpoint
        resp = self.api.post("/api/v1/purchase/vendor-bills/agency-match/", {
            "purchaseOrderId": str(self.orig_po.id),
            "vendorBillNumber": "INV-DUPLICATE-CHECK",
            "billDate": "2026-10-16",
            "qty": 24640,
            "rate": 5.5,
            "action": "save_draft",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(resp.data))

    def test_approve_creates_payable_linked_to_final_po(self):
        """Approving creates a PurchaseBill payable linked to the Final Job Work PO."""
        bill = VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number="INV-PAYABLE-TEST",
            bill_date="2026-10-16",
            party=self.agency,
            party_name=self.agency.name,
            purchase_order=self.orig_po,
            gst_pct=Decimal("5.00"),
            subtotal=Decimal("135520.00"),
            gst_amount=Decimal("6776.00"),
            total=Decimal("142296.00"),
            status="Pending",
        )
        VendorBillLine.objects.create(
            client=self.tenant,
            vendor_bill=bill,
            line_no=1,
            item=self.fabric,
            qty=Decimal("24640.00"),
            rate=Decimal("5.50"),
            amount=Decimal("135520.00"),
            line_total=Decimal("142296.00"),
        )

        resp = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/approve/", {"remarks": "Matched with Final PO", "createPayable": True})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        bill.refresh_from_db()
        self.assertEqual(bill.status, "Approved")
        self.assertIsNotNone(bill.purchase_bill)
        self.assertEqual(bill.purchase_bill.status, "Unpaid")
        self.assertEqual(bill.purchase_bill.total, Decimal("142296.00"))
        # Linked to Final Job Work PO
        self.assertEqual(bill.purchase_bill.purchase_order_id, self.final_po.id)

    def test_separation_of_duties_prevents_self_approval(self):
        """A user cannot approve a vendor bill they created unless superuser."""
        bill = VendorBill.objects.create(
            client=self.tenant,
            vendor_bill_number="INV-SELF-APP-TEST",
            bill_date="2026-10-16",
            party=self.agency,
            status="Pending",
            created_by=self.accounts_exec,
        )
        VendorBillLine.objects.create(
            client=self.tenant,
            vendor_bill=bill,
            line_no=1,
            qty=Decimal("100"),
            rate=Decimal("5.00"),
            amount=Decimal("500.00"),
        )

        # Authenticate as accounts_exec (non-superuser)
        exec_client = APIClient()
        exec_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.accounts_exec)['access']}")

        resp = exec_client.post(f"/api/v1/purchase/vendor-bills/{bill.id}/approve/", {"remarks": "Approve mine"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Separation of duties", str(resp.data))

    def test_agency_match_approve_action_creates_payable(self):
        """Agency match endpoint with action='approve' approves bill and generates payable without 500."""
        resp = self.api.post("/api/v1/purchase/vendor-bills/agency-match/", {
            "purchaseOrderId": str(self.orig_po.id),
            "vendorBillNumber": "INV-AGENCY-APPROVE-ENDPOINT",
            "billDate": "2026-10-16",
            "qty": 24640,
            "rate": 5.5,
            "gstPct": 5.0,
            "remarks": "Approved through agency match endpoint",
            "action": "approve",
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "Approved")
        self.assertIsNotNone(resp.data.get("purchaseBillId") or resp.data.get("purchaseBillNumber"))
