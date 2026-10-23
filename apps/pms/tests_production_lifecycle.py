"""PMS <-> Production Lifecycle integration (8 stages, one shared read model).

Run with::

    python manage.py test apps.pms.tests_production_lifecycle
"""
import json
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.hrms.models import Employee
from apps.inventory.models import QCInspection
from apps.masters.models import Agency, FabricQuality, Item, Party
from apps.pms import production_services
from apps.pms.lifecycle_services import production_lifecycle
from apps.pms.models import DailyProductionEntry, Project, ProductionInstruction, ProjectStage
from apps.pms.pipeline_services import sync_project_pipeline_stages
from apps.purchase.models import PurchaseBill, PurchaseOrder, PurchaseOrderLine, VendorBill
from apps.sales.services import recalculate_document


def _states(lifecycle):
    return {s["key"]: s["state"] for s in lifecycle["stages"]}


class ProductionLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Lifecycle Tenant", slug="lifecycle-tenant")
        self.admin = User.objects.create_user(
            email="lc-admin@example.com", password="secret", client=self.tenant,
            is_superuser=True, name="LC Admin",
        )
        from apps.accounting.services import seed_chart_of_accounts
        seed_chart_of_accounts(self.tenant)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        quality = FabricQuality.objects.create(client=self.tenant, name="Velvet Grey", code="VG01")
        self.fabric = Item.objects.create(
            client=self.tenant, sku="GREY-VG01", name="Velvet Grey Fabric", uom="Meter",
            item_kind="Standalone", fabric_quality=quality, cost_price=Decimal("5.50"),
        )
        self.agency_party = Party.objects.create(
            client=self.tenant, name="Vendor A Agency", code="AG-A", type="Vendor",
        )
        self.agency = Agency.objects.create(client=self.tenant, name="Vendor A Agency", code="AG-A")
        self.employee = Employee.objects.create(
            client=self.tenant, employee_code="EMP-LC1", name="Ramesh Kumar",
            status="Active", joining_date=date(2026, 1, 1),
        )
        self.project = Project.objects.create(
            client=self.tenant, code="PRJ-LC-001", quantity=Decimal("1000"),
            requirement_meters=Decimal("1000"), status="In Progress",
            specifications=json.dumps({"shortage_action": "PRODUCTION_ORDER"}),
        )
        self.po = PurchaseOrder.objects.create(
            client=self.tenant, po_number="PO-LC-0001", party=self.agency_party,
            doc_date="2026-10-01", purchase_type="Vendor A Production",
            status="Draft", project=self.project, created_by=self.admin,
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=self.po, line_no=1, item=self.fabric,
            description="Velvet Grey Fabric", qty=Decimal("1000"), rate=Decimal("5.50"),
            tax_pct=Decimal("5.00"), uom="Meter",
        )
        recalculate_document(self.po)

    # -- helpers -------------------------------------------------------------
    def lifecycle(self):
        self.po.refresh_from_db()
        return production_lifecycle(self.po)

    def make_instruction(self, status="Approved"):
        self.pi = ProductionInstruction.objects.create(
            client=self.tenant, instruction_number="PI-LC-0001", agency_name=self.agency.name,
            agency=self.agency, order_reference=self.po.po_number, order_meter=Decimal("1000"),
            fabric=self.fabric.name, agreed_job_rate=Decimal("5.50"), employee=self.employee,
            status=status,
        )
        return self.pi

    def produce(self, meters, status="Submitted"):
        return DailyProductionEntry.objects.create(
            client=self.tenant, instruction=self.pi, entry_date=date(2026, 10, 5),
            meters=Decimal(meters), good_meter=Decimal(meters), status=status,
            entered_by_employee=self.employee,
        )

    def qc(self, inspected, status="Approved"):
        return QCInspection.objects.create(
            client=self.tenant, inspection_number=f"QC-LC-{QCInspection.objects.count() + 1}",
            source_document_type="GRN", status=status, total_inspected_qty=Decimal(inspected),
            passed_qty=Decimal(inspected), purchase_order=self.po, production_instruction=self.pi,
        )

    def verify(self):
        self.pi.status = "PO Pending"
        self.pi.verified_at = timezone.now()
        self.pi.verified_by = self.admin
        self.pi.save()

    def make_final_po(self, status="Approved", qty="1000"):
        self.final_po = PurchaseOrder.objects.create(
            client=self.tenant, po_number="FJWPO-LC-0001", party=self.agency_party,
            doc_date="2026-10-15", purchase_type="Final Job Work",
            reference_number=self.pi.instruction_number, status=status, created_by=self.admin,
        )
        PurchaseOrderLine.objects.create(
            client=self.tenant, purchase_order=self.final_po, line_no=1, item=self.fabric,
            description="Job Work - Velvet Grey", qty=Decimal(qty), rate=Decimal("5.50"),
            tax_pct=Decimal("5.00"), uom="Meter",
        )
        recalculate_document(self.final_po)
        self.pi.final_po = self.final_po
        self.pi.status = "PO Created" if status == "Approved" else "PO Pending"
        self.pi.save()

    def agency_match(self, action, bill_id=None, qty=1000, number="INV-LC-001"):
        payload = {
            "purchaseOrderId": str(self.po.id), "vendorBillNumber": number,
            "billDate": "2026-10-16", "qty": qty, "rate": 5.5, "gstPct": 5.0,
            "remarks": "lifecycle test", "action": action,
        }
        if bill_id:
            payload["billId"] = bill_id
        return self.api.post("/api/v1/purchase/vendor-bills/agency-match/", payload, format="json")

    def run_to_final_po(self):
        self.po.status = "Approved"
        self.po.save()
        self.make_instruction()
        self.produce("1000")
        self.qc("1000")
        self.verify()
        self.make_final_po()

    # -- the 8 stages ----------------------------------------------------------
    def test_stages_progress_from_saved_records_only(self):
        lc = self.lifecycle()
        self.assertEqual([s["key"] for s in lc["stages"]], [
            "purchase_order", "production_instruction", "daily_production_entry", "grey_fabric_qc",
            "completion_verification", "final_job_work_po", "agency_bill_match", "disbursement_payment",
        ])
        self.assertEqual(_states(lc)["purchase_order"], "active")
        self.assertEqual(lc["stages"][0]["docNumber"], "PO-LC-0001")
        self.assertEqual(lc["stages"][0]["amount"], float(self.po.total))
        self.assertEqual(lc["stages"][0]["quantity"], 1000.0)
        self.assertTrue(all(s["state"] == "pending" for s in lc["stages"][1:]))

        # 1. A PO awaiting approval is not completed.
        self.po.status = "Pending Approval"
        self.po.save()
        self.assertEqual(_states(self.lifecycle())["purchase_order"], "active")
        self.po.status = "Approved"
        self.po.save()
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["purchase_order"], "completed")
        self.assertEqual(lc["currentStage"], "production_instruction")

        # 2. Draft instruction is active, approved is completed.
        self.make_instruction(status="Draft")
        self.assertEqual(_states(self.lifecycle())["production_instruction"], "active")
        self.pi.status = "Approved"
        self.pi.save()
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["production_instruction"], "completed")
        self.assertEqual(lc["stages"][1]["docNumber"], "PI-LC-0001")

        # 3. Partial production is active; draft entries do not count.
        self.produce("400")
        self.produce("600", status="Draft")
        lc = self.lifecycle()
        daily = lc["stages"][2]
        self.assertEqual(daily["state"], "active")
        self.assertEqual(daily["quantity"], 400.0)
        self.assertEqual(daily["balanceQuantity"], 600.0)
        self.assertEqual(daily["draftEntryCount"], 1)
        self.produce("600")
        self.assertEqual(_states(self.lifecycle())["daily_production_entry"], "completed")

        # 4. QC completes only when the produced quantity is inspected and passed.
        self.qc("500")
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["grey_fabric_qc"], "active")
        self.assertEqual(lc["stages"][3]["pendingQuantity"], 500.0)
        self.qc("500")
        self.assertEqual(_states(self.lifecycle())["grey_fabric_qc"], "completed")

        # 5. Verification Pending is active; final approval completes.
        self.pi.status = "Verification Pending"
        self.pi.save()
        self.assertEqual(_states(self.lifecycle())["completion_verification"], "active")
        self.verify()
        self.assertEqual(_states(self.lifecycle())["completion_verification"], "completed")

        # 6. A final PO pending approval is active; approved completes.
        self.make_final_po(status="Pending Approval")
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["final_job_work_po"], "active")
        self.assertEqual(lc["stages"][5]["docNumber"], "FJWPO-LC-0001")
        self.final_po.status = "Approved"
        self.final_po.save()
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["final_job_work_po"], "completed")
        self.assertEqual(lc["currentStage"], "agency_bill_match")
        self.assertEqual(_states(lc)["disbursement_payment"], "pending")

    def test_agency_bill_match_and_payment_transitions(self):
        self.run_to_final_po()

        # 7. Submit for approval -> Pending Approval (active), approve -> completed.
        res = self.agency_match("submit_approval")
        self.assertEqual(res.status_code, 200, res.data)
        bill = VendorBill.objects.get(vendor_bill_number="INV-LC-001")
        self.assertEqual(bill.status, "Pending Approval")
        self.assertEqual(bill.match_status, "Matched")
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["agency_bill_match"], "active")
        self.assertEqual(lc["stages"][6]["matchResult"], "Match")
        self.assertEqual(_states(lc)["disbursement_payment"], "pending")

        res = self.api.post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/approve/",
            {"remarks": "ok", "createPayable": True}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "Approved")
        payable = bill.purchase_bill
        self.assertEqual(payable.purchase_order_id, self.final_po.id)
        self.assertIsNotNone(payable.journal_entry_id)

        # 8. Payable Unpaid -> active, never completed before Paid.
        lc = self.lifecycle()
        self.assertEqual(_states(lc)["agency_bill_match"], "completed")
        pay = lc["stages"][7]
        self.assertEqual(pay["state"], "active")
        self.assertEqual(pay["approvalStatus"], "Approved")
        self.assertEqual(pay["payableStatus"], "Unpaid")
        self.assertEqual(pay["balanceDue"], float(payable.total))

        from apps.accounting.models import BankAccount
        from apps.purchase.services import record_payment_out

        bank = BankAccount.objects.filter(client=self.tenant).first()
        mode = "Bank" if bank else "Cash"
        half = (payable.total / 2).quantize(Decimal("0.01"))
        record_payment_out(
            client=self.tenant, party=payable.party, amount=half, payment_date=date(2026, 10, 20),
            mode=mode, bank_account=bank, bill=payable, user=self.admin,
        )
        lc = self.lifecycle()
        pay = lc["stages"][7]
        self.assertEqual(pay["state"], "active")
        self.assertEqual(pay["payableStatus"], "Partially Paid")
        self.assertEqual(len(pay["payments"]), 1)
        self.assertFalse(lc["isComplete"])

        payable.refresh_from_db()
        record_payment_out(
            client=self.tenant, party=payable.party, amount=payable.total - payable.amount_paid,
            payment_date=date(2026, 10, 21), mode=mode, bank_account=bank, bill=payable, user=self.admin,
        )
        lc = self.lifecycle()
        self.assertEqual(lc["stages"][7]["state"], "completed")
        self.assertEqual(lc["stages"][7]["payableStatus"], "Paid")
        self.assertEqual(lc["stages"][7]["balanceDue"], 0.0)
        self.assertTrue(lc["isComplete"])

    def test_agency_match_draft_then_approve_no_longer_dead_ends(self):
        self.run_to_final_po()
        res = self.agency_match("save_draft")
        self.assertEqual(res.status_code, 200, res.data)
        bill = VendorBill.objects.get(vendor_bill_number="INV-LC-001")
        self.assertEqual(bill.status, "Draft")
        self.assertEqual(_states(self.lifecycle())["agency_bill_match"], "active")

        res = self.agency_match("approve", bill_id=str(bill.id))
        self.assertEqual(res.status_code, 200, res.data)
        bill.refresh_from_db()
        self.assertEqual(bill.status, "Approved")
        self.assertIsNotNone(bill.purchase_bill_id)

    def test_mismatch_still_requires_remarks_and_reject_works_from_pending_approval(self):
        self.run_to_final_po()
        res = self.agency_match("submit_approval", qty=1200, number="INV-LC-MM")
        self.assertEqual(res.status_code, 200, res.data)
        bill = VendorBill.objects.get(vendor_bill_number="INV-LC-MM")
        self.assertEqual(bill.match_status, "Mismatch")
        # Mismatch without remarks is still refused.
        res = self.api.post(f"/api/v1/purchase/vendor-bills/{bill.id}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.api.post(
            f"/api/v1/purchase/vendor-bills/{bill.id}/reject/", {"remarks": "qty over"}, format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        stage = self.lifecycle()["stages"][6]
        self.assertEqual(stage["status"], "Rejected")
        self.assertTrue(stage["blocked"])
        self.assertEqual(stage["state"], "active")

    # -- endpoints, PMS sync and PO paths --------------------------------------
    # -- PMS project view: client PDF section 05 flow -------------------------
    def po_pi(self):
        """Production Work Instruction raised from the approved Vendor A PO."""
        res = self.api.post("/api/v1/production/instructions/", {
            "agencyId": str(self.agency.id), "agencyName": self.agency.name,
            "orderReference": self.po.po_number, "orderMeter": 1000,
            "employeeId": str(self.employee.id), "fabric": self.fabric.name, "processType": "Weaving",
            "agreedJobRate": 5.5, "expectedCompletionDate": "2026-11-30",
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        pi = ProductionInstruction.objects.get(id=res.data["id"])
        self.assertEqual(str(pi.expected_completion_date), "2026-11-30")
        return pi

    def pms_states(self):
        res = self.api.get(f"/api/v1/pms/projects/{self.project.id}/production-lifecycle/")
        self.assertEqual(res.status_code, 200)
        return res.data["lifecycles"]

    def pms_state_map(self):
        return {s["key"]: s for s in self.pms_states()[0]["stages"]}

    def test_pms_flow_po_first_then_pdf_stages_to_payable(self):
        lc = self.pms_states()[0]
        self.assertEqual(lc["purchaseOrderId"], str(self.po.id))
        self.assertEqual([s["label"] for s in lc["stages"]], [
            "Purchase Order", "Production Work Instruction", "External Agency Assignment",
            "Daily Production Entry", "Production Completion", "Final Quantity Verification",
            "Final Job Work PO", "Agency Bill Match", "Approval & Payable",
        ])
        st = self.pms_state_map()
        self.assertEqual(st["purchase_order"]["state"], "active")  # Draft PO
        self.assertEqual(st["production_work_instruction"]["state"], "pending")

        self.po.status = "Approved"
        self.po.save()
        st = self.pms_state_map()
        self.assertEqual(st["purchase_order"]["state"], "completed")
        self.assertEqual(st["production_work_instruction"]["state"], "active")

        pi = self.po_pi()
        self.assertEqual(self.pms_states()[0]["instructionId"], str(pi.id))
        self.assertEqual(self.api.post(f"/api/v1/production/instructions/{pi.id}/approve/").status_code, 200)
        self.assertEqual(self.api.post(f"/api/v1/production/instructions/{pi.id}/start/").status_code, 200)
        self.pi = pi
        self.produce("600")
        st = self.pms_state_map()
        self.assertEqual(st["production_work_instruction"]["state"], "completed")
        self.assertEqual(st["external_agency_assignment"]["state"], "completed")
        self.assertEqual(st["daily_production_entry"]["state"], "active")
        self.produce("400")
        res = self.api.post(f"/api/v1/production/instructions/{pi.id}/complete/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.pms_state_map()["production_completion"]["state"], "completed")

        pi.refresh_from_db()
        self.pi = pi
        self.verify()
        instruction = production_services.generate_final_job_work_po(
            pi, user=self.admin,
            custom_data={"vendorId": str(self.agency_party.id), "agreedJobRate": 5.5, "action": "approve_generate"},
        )
        final_po = PurchaseOrder.objects.get(id=instruction.final_po_id)
        st = self.pms_state_map()
        self.assertEqual(st["final_quantity_verification"]["state"], "completed")
        self.assertEqual(st["final_job_work_po"]["state"], "completed")
        self.assertEqual(st["agency_bill_match"]["state"], "active")

        # Bill matched on the ORIGINAL PO: the server joins PO + Job Work PO + bill.
        line = final_po.line_items.first()
        res = self.api.post("/api/v1/purchase/vendor-bills/agency-match/", {
            "purchaseOrderId": str(self.po.id), "vendorBillNumber": "INV-PDF-1", "billDate": "2026-10-16",
            "qty": float(line.qty), "rate": float(line.rate), "gstPct": float(line.tax_pct),
            "action": "submit_approval",
        }, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        matching = res.data["matching"]
        self.assertTrue(matching["isAgencyMatch"])
        fields = [r["field"] for r in matching["threeWayReconciliation"]]
        self.assertIn("Original PO Reference", fields)
        self.assertIn("Final Job Work PO Reference", fields)
        lc = self.pms_states()[0]
        st = {s["key"]: s for s in lc["stages"]}
        self.assertEqual(st["agency_bill_match"]["state"], "completed", st["agency_bill_match"])
        self.assertEqual(st["approval_payable"]["state"], "active")

        res = self.api.post(f"/api/v1/purchase/vendor-bills/{lc['vendorBillId']}/approve/",
                            {"remarks": "ok", "createPayable": True}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        lc = self.pms_states()[0]
        payable = [s for s in lc["stages"] if s["key"] == "approval_payable"][0]
        self.assertEqual(payable["state"], "completed")
        self.assertEqual(payable["payableStatus"], "Unpaid")
        self.assertTrue(lc["isComplete"])

        # PMS pipeline: the same 9 stages, all completed.
        result = sync_project_pipeline_stages(self.project)
        production = {s["name"]: s["status"] for s in result["stages"] if s["branch"] == "branch_production"}
        self.assertEqual(list(production), [s["label"] for s in lc["stages"]])
        self.assertTrue(all(v == "Completed" for v in production.values()), production)

    def test_renamed_production_stages_keep_their_tasks(self):
        from apps.pms.models import Task
        old_a = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Generate Final Job Work PO", sequence=7,
        )
        old_b = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Production / Job Work PO", sequence=8,
        )
        Task.objects.create(client=self.tenant, project=self.project, stage=old_a, task_name="Draft PO")
        Task.objects.create(client=self.tenant, project=self.project, stage=old_b, task_name="Send PO")
        sync_project_pipeline_stages(self.project)
        stage = ProjectStage.objects.get(project=self.project, name="Final Job Work PO")
        self.assertEqual(
            sorted(Task.objects.filter(stage=stage).values_list("task_name", flat=True)),
            ["Draft PO", "Send PO"],
        )
        self.assertFalse(ProjectStage.objects.filter(project=self.project, name__in=[
            "Generate Final Job Work PO", "Production / Job Work PO",
        ]).exists())

    def test_purchase_po_endpoint_unchanged_for_vendor_a_po(self):
        self.run_to_final_po()
        po_res = self.api.get(f"/api/v1/purchase/orders/{self.po.id}/production-lifecycle/")
        self.assertEqual(po_res.status_code, 200)
        self.assertTrue(po_res.data["applicable"])
        res = self.api.get(f"/api/v1/purchase/orders/{self.final_po.id}/production-lifecycle/")
        self.assertFalse(res.data["applicable"])
        # The instruction of a project Vendor A PO also shows in the PMS flow.
        self.assertEqual([lc["instructionId"] for lc in self.pms_states()], [str(self.pi.id)])

    def test_generated_final_po_carries_project_but_stays_out_of_purchase_branch_list(self):
        self.po.status = "Approved"
        self.po.save()
        self.make_instruction()
        self.produce("1000")
        self.verify()
        instruction = production_services.generate_final_job_work_po(
            self.pi, user=self.admin,
            custom_data={"vendorId": str(self.agency_party.id), "agreedJobRate": 5.5, "action": "approve_generate"},
        )
        po = PurchaseOrder.objects.get(id=instruction.final_po_id)
        self.assertEqual(po.project_id, self.project.id)
        self.assertEqual(po.purchase_type, "Final Job Work")
        res = self.api.get(f"/api/v1/pms/projects/{self.project.id}/purchase-orders/")
        self.assertEqual(res.status_code, 200)
        listed = res.data if isinstance(res.data, list) else res.data.get("data", res.data.get("results", []))
        self.assertNotIn(str(po.id), [str(r.get("id")) for r in listed])
        self.assertEqual(_states(self.lifecycle())["final_job_work_po"], "completed")

    def test_next_jwpo_number_endpoint_works(self):
        res = self.api.get("/api/v1/purchase/orders/next-jwpo-number/")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))

    def test_pms_method_1_grey_purchase_po_unchanged(self):
        res = self.api.post(
            f"/api/v1/pms/projects/{self.project.id}/purchase-orders/",
            {"vendorId": str(self.agency_party.id), "itemId": str(self.fabric.id), "quantity": 500, "rate": 45},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        po = PurchaseOrder.objects.get(id=res.data["id"])
        self.assertEqual(po.purchase_type, "Grey Stock Purchase")
        self.assertEqual(po.status, "Approved")
        self.assertEqual(po.project_id, self.project.id)
        # A grey purchase PO is not part of the production lifecycle.
        self.assertEqual([lc["purchaseOrderId"] for lc in self.pms_states()], [str(self.po.id)])

    def test_payable_bill_is_not_duplicated(self):
        self.run_to_final_po()
        self.agency_match("approve")
        self.agency_match("approve")  # duplicate invoice number refused
        self.assertEqual(VendorBill.objects.filter(client=self.tenant).count(), 1)
        self.assertEqual(
            PurchaseBill.objects.filter(client=self.tenant, purchase_order=self.final_po).count(), 1
        )

    def test_processing_plan_stage_needs_an_approved_plan(self):
        from apps.jobwork.models import ProcessPlan

        def plan_stage():
            result = sync_project_pipeline_stages(self.project)
            return {s["name"]: s for s in result["stages"]}["Processing Plan & Vendor Assignment"]

        self.assertEqual(plan_stage()["status"], "Pending")
        res = self.api.post("/api/v1/jobwork/process-plans/", {
            "date": "2026-10-20", "process": "Dyeing", "vendor": "Shree Processors",
            "fabricItem": self.fabric.name, "expectedQty": 1000, "expectedLoss": 2,
            "expectedReturnQty": 980, "expectedCompletionDate": "2026-11-05",
            "status": "Draft", "remarks": f"Project {self.project.code}",
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(plan_stage()["status"], "In Progress")
        res = self.api.post(f"/api/v1/jobwork/process-plans/{res.data['id']}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(plan_stage()["status"], "Completed")
        # The downstream stage is named "Processing" (no hard-coded dyeing).
        names = [s["name"] for s in sync_project_pipeline_stages(self.project)["stages"]]
        self.assertIn("Processing", names)
        self.assertNotIn("Processing (Dyeing)", names)
        self.assertEqual(ProcessPlan.objects.filter(client=self.tenant).count(), 1)
