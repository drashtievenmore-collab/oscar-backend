"""Purchase endpoints (api.md §6)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core import idempotency
from apps.core.money import ZERO, D, ROUNDING_TOLERANCE, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.printing import PdfNotAvailable, print_payload, send_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.inventory import services as stock
from apps.masters import uom
from apps.sales.views import PostedReturnGuardMixin, SalesDocumentViewSet, _clone_document

from . import services
from .models import (
    PURCHASE_TYPE_FINAL_JOB_WORK,
    PURCHASE_TYPE_VENDOR_A,
    Expense,
    GoodsReceipt,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
    SupplierDelivery,
    VendorBill,
    VendorBillLine,
)
from .serializers import (
    ExpenseSerializer,
    GoodsReceiptSerializer,
    PaymentOutSerializer,
    PurchaseBillSerializer,
    PurchaseOrderSerializer,
    PurchaseReturnSerializer,
    QcSerializer,
    ReceiveGoodsSerializer,
    SupplierDeliverySerializer,
    VendorBillSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


# ---------------------------------------------------------------------------
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrderViewSet(SalesDocumentViewSet):
    queryset = PurchaseOrder.objects.select_related("project")
    serializer_class = PurchaseOrderSerializer
    audit_entity_type = "PurchaseOrder"
    audit_label_field = "po_number"
    status_field = "status"
    print_title = "Purchase Order"
    filter_map = {
        "vendorId": "party_id",
        "purchaseOrderId": "purchase_order_id",
        "poId": "purchase_order_id",
        "po": "purchase_order_id",
        "vendor_id": "party_id",
        "purchaseType": "purchase_type",
        "projectId": "project_id",
    }
    permission_map = {"read": ["view_purchase"], "write": ["create_purchase_order"]}
    draft_values = ("Draft", "Issued")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["include_billed_status"] = self.action == "retrieve"
        return context

    def perform_create(self, serializer):
        serializer.validated_data["po_number"] = allocate_number(
            self.request.user.client, "PO", serializer.validated_data.get("doc_date")
        )
        instance = super().perform_create(serializer)
        from apps.masters.rate_services import check_po_rate_tolerance
        check_po_rate_tolerance(instance, user=self.request.user)
        return instance

    def perform_update(self, serializer):
        instance = super().perform_update(serializer)
        from apps.masters.rate_services import check_po_rate_tolerance
        check_po_rate_tolerance(instance, user=self.request.user)
        return instance

    @action(detail=True, methods=["get"], url_path="rate-check")
    def rate_check(self, request, pk=None):
        order = self.get_object()
        from apps.masters.rate_services import evaluate_po_rates
        return Response(evaluate_po_rates(order))

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    @transaction.atomic
    def submit_for_approval(self, request, pk=None):
        from apps.core import approval_services
        res = self._transition(request, from_statuses=("Draft", "Issued"), to_status="Pending Approval")
        order = PurchaseOrder.objects.get(pk=self.get_object().pk)
        approval_services.create_approval_request(
            client_id=order.client_id,
            document_type="purchase_order",
            document_id=order.id,
            document_number=order.po_number,
            requested_by=request.user,
            reason=request.data.get("remarks") or f"Purchase Order {order.po_number} submitted for approval",
            force=True,
        )
        return res

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        from apps.core.permissions import require_permission, PermissionDenied
        from apps.core.models import ApprovalRequest
        from apps.core.approval_services import assert_not_pending_approval
        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)
        assert_not_pending_approval(order.client_id, "purchase_order_rate", order.id, "approve this purchase order")
        if order.created_by_id and order.created_by_id == getattr(request.user, "id", None) and not getattr(request.user, "is_superuser", False):
            raise PermissionDenied(
                "Separation of duties: You cannot approve a Purchase Order you created.",
                code="SELF_APPROVAL_NOT_PERMITTED",
            )
        try:
            require_permission(request.user, "approve_purchase_order")
        except PermissionDenied:
            require_permission(request.user, "manage_approvals", message="You do not have permission to approve Purchase Orders.")

        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Approved")
        appr_req = ApprovalRequest.objects.filter(
            client_id=order.client_id,
            document_type="purchase_order",
            document_id=str(order.id),
            status="Pending",
        ).first()
        if appr_req:
            appr_req.status = "Approved"
            appr_req.decided_by = request.user
            appr_req.decided_at = timezone.now()
            appr_req.comments = request.data.get("remarks", "")
            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])
        return res

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def reject(self, request, pk=None):
        from apps.core.permissions import require_permission, PermissionDenied
        from apps.core.models import ApprovalRequest
        try:
            require_permission(request.user, "approve_purchase_order")
        except PermissionDenied:
            require_permission(request.user, "manage_approvals", message="You do not have permission to reject Purchase Orders.")

        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)
        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Rejected")
        appr_req = ApprovalRequest.objects.filter(
            client_id=order.client_id,
            document_type="purchase_order",
            document_id=str(order.id),
            status="Pending",
        ).first()
        if appr_req:
            appr_req.status = "Rejected"
            appr_req.decided_by = request.user
            appr_req.decided_at = timezone.now()
            appr_req.comments = request.data.get("remarks", "")
            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])
        return res

    @action(detail=True, methods=["post"], url_path="return-for-correction")
    @transaction.atomic
    def return_correction(self, request, pk=None):
        from apps.core.permissions import require_permission, PermissionDenied
        from apps.core.models import ApprovalRequest
        try:
            require_permission(request.user, "approve_purchase_order")
        except PermissionDenied:
            require_permission(request.user, "manage_approvals", message="You do not have permission to return Purchase Orders for correction.")

        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)
        res = self._transition(request, from_statuses=("Pending Approval",), to_status="Draft")
        appr_req = ApprovalRequest.objects.filter(
            client_id=order.client_id,
            document_type="purchase_order",
            document_id=str(order.id),
            status="Pending",
        ).first()
        if appr_req:
            appr_req.status = "Returned"
            appr_req.decided_by = request.user
            appr_req.decided_at = timezone.now()
            appr_req.comments = request.data.get("remarks", "")
            appr_req.save(update_fields=["status", "decided_by", "decided_at", "comments"])
        return res

    def _transition(self, request, *, from_statuses, to_status):
        order = PurchaseOrder.objects.select_for_update().get(pk=self.get_object().pk)
        if order.status == to_status:
            return Response(self.get_serializer(order).data)
        if order.status not in from_statuses:
            raise Conflict(
                f"{order.po_number} is {order.status}; it cannot move to {to_status}.",
                code=Codes.DRAFT_ONLY,
            )
        if not order.line_items.filter(deleted_at__isnull=True).exists():
            raise ValidationFailed(
                "Add at least one line before submitting the purchase order.",
                field_errors={"lineItems": ["Add at least one line."]},
            )
        previous = order.status
        order.status = to_status
        order.save(update_fields=["status", "updated_at"])
        self.write_audit(
            "status_change", order,
            description=f"{previous} -> {to_status}" + (f": {request.data.get('remarks')}" if request.data.get("remarks") else ""),
        )
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["get"], url_path="billed-status")
    def billed_status(self, request, pk=None):
        """``getPoBilledStatus`` moved server-side (api.md §6.2)."""
        return Response(services.po_billed_status(self.get_object()))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status == "Cancelled":
            raise Conflict("This order is already cancelled.", code=Codes.ALREADY_CANCELLED)

        # api.md §6.2 -- a PO with any active bill cannot be cancelled.
        services.assert_po_cancellable(order)

        reason = request.data.get("reason")
        order.status = "Cancelled"
        order.cancelled_at = timezone.now()
        order.cancelled_by = request.user
        order.cancellation_reason = reason
        order.save()
        self.write_audit("cancel", order, description=reason)
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"], url_path="convert-to-bill")
    @transaction.atomic
    def convert_to_bill(self, request, pk=None):
        order = self.get_object()
        if order.status == "Cancelled":
            raise Conflict("This order is cancelled.", code=Codes.ALREADY_CANCELLED)

        bill = _clone_document(
            order,
            PurchaseBill,
            {"purchase_order": order, "status": "Draft", "location": order.location},
            number_field="bill_number",
            series="BILL",
            line_model_name="PurchaseBillLine",
            line_fk="purchase_bill",
            line_filter=lambda line: D(line.qty) > D(line.billed_qty),
            line_overrides=lambda line: {
                "qty": D(line.qty) - D(line.billed_qty),
                "purchase_order_line": line,
            },
        )
        self.write_audit("convert", order, description=f"Bill {bill.bill_number} created")
        return Response(
            PurchaseBillSerializer(bill, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="start-production")
    @transaction.atomic
    def start_production(self, request, pk=None):
        po = self.get_object()
        from apps.pms.models import ProductionInstruction, Project
        from apps.hrms.models import Employee
        from apps.masters.models import Agency
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        # Only a Vendor A production PO enters the production / daily-entry
        # cycle; a ready grey stock purchase is received through a GRN.
        if po.purchase_type != PURCHASE_TYPE_VENDOR_A:
            raise Conflict(
                f"{po.po_number} is a {po.purchase_type}; only a Vendor A Production PO can start production. "
                "Receive ready grey fabric through a GRN instead.",
                code="WRONG_PURCHASE_TYPE",
            )
        if po.status not in ("Approved", "In Production"):
            raise Conflict(
                f"Approve {po.po_number} before starting production (it is {po.status}).",
                code=Codes.DRAFT_ONLY,
            )

        po.status = "In Production"
        po.save(update_fields=["status", "updated_at"])

        existing_pi = ProductionInstruction.objects.filter(
            client_id=po.client_id,
            order_reference=po.po_number,
            deleted_at__isnull=True,
        ).first()

        if existing_pi:
            if existing_pi.status in ("Draft", "Not Started", "Pending"):
                existing_pi.status = "Running"
                existing_pi.save(update_fields=["status", "updated_at"])
            instruction = existing_pi
        else:
            first_line = po.line_items.first()
            total_qty = sum((fl.qty for fl in po.line_items.all()), Decimal("0.00"))
            fabric_name = (
                first_line.item.name
                if (first_line and first_line.item)
                else getattr(po, "notes", "Grey Fabric") or "Grey Fabric"
            )
            unit_rate = first_line.rate if first_line else Decimal("0.00")

            agency_name = po.party_name or (po.party.name if po.party else "Production Agency")
            agency_obj = Agency.objects.filter(
                client_id=po.client_id,
                name__iexact=agency_name.strip(),
                deleted_at__isnull=True,
            ).first()

            employee = Employee.objects.filter(
                client_id=po.client_id,
                status__in=["Active", "Probation"],
                deleted_at__isnull=True,
            ).first()

            instruction = ProductionInstruction.objects.create(
                client=po.client,
                instruction_number=allocate_number(po.client, "PROD"),
                agency=agency_obj,
                agency_name=agency_name,
                order_reference=po.po_number,
                order_meter=total_qty,
                tentative_rate=unit_rate,
                agreed_rate=unit_rate,
                rate_uom="Meter",
                expected_start_date=po.expected_date or timezone.localdate(),
                pi_date=po.doc_date or timezone.localdate(),
                fabric=fabric_name,
                process_type="Processing",
                employee=employee,
                status="Running",
                notes=f"Auto-generated from Purchase Order {po.po_number}",
                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            )

        project = po.project
        if project is None and po.reference_number:
            project = Project.objects.filter(
                client_id=po.client_id,
                code=po.reference_number,
                deleted_at__isnull=True,
            ).first()
        if project:
            sync_project_pipeline_stages(project)

        self.write_audit("start_production", po, description=f"Production Instruction {instruction.instruction_number} created.")

        return Response({
            "success": True,
            "message": f"Production Instruction {instruction.instruction_number} generated for {instruction.agency_name}. Status moved to In Production.",
            "instructionId": str(instruction.id),
            "instructionNumber": instruction.instruction_number,
            "status": po.status,
            "po": self.get_serializer(po).data,
        })

    @action(detail=True, methods=["get", "post"], url_path="production-instruction")
    @transaction.atomic
    def production_instruction(self, request, pk=None):
        po = self.get_object()
        from apps.pms.models import ProductionInstruction, Project
        from apps.hrms.models import Employee
        from apps.masters.models import Agency
        from apps.core.numbering import peek_next, allocate_number
        from apps.pms.production_services import instruction_completion
        from apps.pms.pipeline_services import sync_project_pipeline_stages

        if po.purchase_type != PURCHASE_TYPE_VENDOR_A:
            raise Conflict(
                f"{po.po_number} is {po.purchase_type}; Production Instructions apply only to Vendor A Production POs.",
                code="WRONG_PURCHASE_TYPE",
            )

        lines = po.line_items.filter(deleted_at__isnull=True)
        total_qty = sum((D(l.qty) for l in lines), ZERO)
        total_amount = sum((D(l.amount or (l.qty * l.rate) or 0) for l in lines), ZERO)
        fabric_names = [
            (l.item.name if l.item else (l.description or "Grey Fabric"))
            for l in lines
        ]
        unique_fabrics = list(dict.fromkeys(fabric_names))
        fabric_name = ", ".join(unique_fabrics) if unique_fabrics else (po.notes or "Grey Fabric")
        first_line = lines.first()
        unit_rate = (total_amount / total_qty) if total_qty > 0 else (first_line.rate if first_line else ZERO)
        vendor_name = po.party_name or (po.party.name if po.party else "")

        all_pis = list(ProductionInstruction.objects.filter(
            client_id=po.client_id,
            order_reference=po.po_number,
            deleted_at__isnull=True,
        ).order_by("created_at"))

        req_pi_id = request.GET.get("instructionId")
        if req_pi_id:
            existing_pi = next((p for p in all_pis if str(p.id) == str(req_pi_id)), None)
            if not existing_pi:
                existing_pi = ProductionInstruction.objects.filter(
                    client_id=po.client_id, id=req_pi_id, deleted_at__isnull=True
                ).first()
        else:
            existing_pi = all_pis[-1] if all_pis else None

        if request.method == "GET":
            instructions_list = []
            for pi_item in all_pis:
                c = instruction_completion(pi_item)
                sup = pi_item.supervisor or pi_item.employee
                instructions_list.append({
                    "id": str(pi_item.id),
                    "instructionNumber": pi_item.instruction_number,
                    "status": pi_item.status,
                    "fabric": pi_item.fabric,
                    "orderMeter": pi_item.order_meter,
                    "supervisorId": str(sup.id) if sup else None,
                    "supervisorName": getattr(sup, "name", "") if sup else "",
                    "startDate": pi_item.expected_start_date or pi_item.pi_date,
                    "expectedCompletionDate": pi_item.expected_completion_date,
                    "completion": {
                        "poQuantity": pi_item.order_meter,
                        "totalProduced": c["totalProduced"],
                        "acceptedQty": c["acceptedQty"],
                        "rejectedQty": c["rejectedQty"],
                        "reprocessQty": c.get("reprocessQty", 0),
                        "balanceQty": c.get("balanceQty", 0),
                        "completionPercentage": c.get("completionPercentage", 0),
                        "finalAmount": c["finalAmount"],
                    },
                    "entriesCount": len(c.get("rows", [])),
                })

            pi_data = None
            if existing_pi:
                comp = instruction_completion(existing_pi)
                supervisor = existing_pi.supervisor or existing_pi.employee
                pi_data = {
                    "id": str(existing_pi.id),
                    "instructionNumber": existing_pi.instruction_number,
                    "status": existing_pi.status,
                    "employeeId": str(existing_pi.employee_id) if existing_pi.employee_id else (str(supervisor.id) if supervisor else None),
                    "supervisorId": str(supervisor.id) if supervisor else None,
                    "supervisorName": getattr(supervisor, "name", "") if supervisor else "",
                    "startDate": existing_pi.expected_start_date or existing_pi.pi_date,
                    "expectedCompletionDate": existing_pi.expected_completion_date,
                    "remarks": existing_pi.notes or "",
                    "orderMeter": existing_pi.order_meter,
                    "fabric": existing_pi.fabric,
                    "agencyName": existing_pi.agency_name,
                    "finalPoNumber": getattr(existing_pi.final_po, "po_number", None) if existing_pi.final_po else None,
                    "finalPoId": str(existing_pi.final_po_id) if existing_pi.final_po_id else None,
                    "isVerified": existing_pi.status in ("Verified", "PO Pending", "PO Created", "Closed"),
                    "completion": {
                        "poQuantity": existing_pi.order_meter,
                        "totalProduced": comp["totalProduced"],
                        "acceptedQty": comp["acceptedQty"],
                        "rejectedQty": comp["rejectedQty"],
                        "reprocessQty": comp.get("reprocessQty", 0),
                        "balanceQty": comp.get("balanceQty", 0),
                        "completionPercentage": comp.get("completionPercentage", 0),
                        "finalAmount": comp["finalAmount"],
                    },
                    "entries": comp.get("rows", []),
                }

            preview_num = (
                existing_pi.instruction_number
                if existing_pi
                else peek_next(po.client, "PROD")
            )

            return Response({
                "po": {
                    "id": str(po.id),
                    "poNumber": po.po_number,
                    "purchaseType": po.purchase_type,
                    "status": po.status,
                    "vendorName": vendor_name,
                    "fabricName": fabric_name,
                    "poQuantity": total_qty,
                    "rate": round2(unit_rate),
                    "poAmount": round2(total_amount),
                    "lineCount": lines.count(),
                    "lines": [
                        {
                            "id": str(l.id),
                            "name": l.item.name if l.item else (l.description or "Grey Fabric"),
                            "qty": l.qty,
                            "rate": l.rate,
                            "amount": l.amount,
                        }
                        for l in lines
                    ],
                },
                "nextInstructionNumber": preview_num,
                "instruction": pi_data,
                "instructions": instructions_list,
            })

        # POST: Save Draft or Submit for Approval
        from django.utils.dateparse import parse_date
        data = request.data or {}
        action_type = data.get("action", "save_draft")
        supervisor_id = data.get("supervisorId")
        raw_start_date = data.get("startDate")
        raw_completion_date = data.get("expectedCompletionDate")
        start_date = parse_date(str(raw_start_date)) if raw_start_date else None
        expected_completion_date = parse_date(str(raw_completion_date)) if raw_completion_date else None
        remarks = data.get("remarks", "")

        field_errors = {}
        if not supervisor_id:
            field_errors["supervisorId"] = ["Assigned Supervisor is required."]
        if not start_date:
            field_errors["startDate"] = ["Production Start Date is required."]
        if not expected_completion_date:
            field_errors["expectedCompletionDate"] = ["Expected Completion Date is required."]

        if field_errors:
            raise ValidationFailed("Please fill all required fields.", field_errors=field_errors)

        supervisor = Employee.objects.filter(
            client_id=po.client_id,
            id=supervisor_id,
            deleted_at__isnull=True,
        ).first()
        if not supervisor:
            raise ValidationFailed("Assigned Supervisor not found or invalid.", field_errors={"supervisorId": ["Invalid supervisor."]})

        agency_name = po.party_name or (po.party.name if po.party else "Production Agency")
        agency_obj = Agency.objects.filter(
            client_id=po.client_id,
            name__iexact=agency_name.strip(),
            deleted_at__isnull=True,
        ).first()

        if existing_pi:
            instruction = existing_pi
            instruction.supervisor = supervisor
            instruction.employee = supervisor
            instruction.expected_start_date = start_date
            instruction.expected_completion_date = expected_completion_date
            instruction.notes = remarks
            instruction.order_meter = total_qty
            instruction.fabric = fabric_name
            instruction.tentative_rate = unit_rate
            instruction.agreed_rate = unit_rate
            instruction.agreed_job_rate = unit_rate
            instruction.updated_by = request.user if getattr(request.user, "is_authenticated", False) else None

            if action_type == "submit_approval" and instruction.status in ("Draft", "Not Started"):
                instruction.status = "Approved"

            instruction.save()
            audit_action = "production_instruction_submit" if action_type == "submit_approval" else "production_instruction_update"
            self.write_audit(audit_action, po, description=f"Production Instruction {instruction.instruction_number} updated ({action_type}).")
        else:
            initial_status = "Approved" if action_type == "submit_approval" else "Draft"
            instruction = ProductionInstruction.objects.create(
                client=po.client,
                instruction_number=allocate_number(po.client, "PROD"),
                agency=agency_obj,
                agency_name=agency_name,
                order_reference=po.po_number,
                order_meter=total_qty,
                tentative_rate=unit_rate,
                agreed_rate=unit_rate,
                agreed_job_rate=unit_rate,
                rate_uom="Meter",
                expected_start_date=start_date,
                expected_completion_date=expected_completion_date,
                pi_date=start_date or timezone.localdate(),
                fabric=fabric_name,
                process_type="Processing",
                employee=supervisor,
                supervisor=supervisor,
                status=initial_status,
                notes=remarks,
                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                updated_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            )
            audit_action = "production_instruction_create_and_submit" if action_type == "submit_approval" else "production_instruction_create_draft"
            self.write_audit(audit_action, po, description=f"Production Instruction {instruction.instruction_number} created ({action_type}).")

        if action_type == "submit_approval":
            if po.status in ("Approved", "Draft"):
                po.status = "In Production"
                po.save(update_fields=["status", "updated_at"])
                self.write_audit("status_change", po, description=f"Moved to In Production via Instruction {instruction.instruction_number}")

            project = po.project
            if project is None and po.reference_number:
                project = Project.objects.filter(
                    client_id=po.client_id,
                    code=po.reference_number,
                    deleted_at__isnull=True,
                ).first()
            if project:
                sync_project_pipeline_stages(project)

        comp = instruction_completion(instruction)
        return Response({
            "success": True,
            "message": f"Production Instruction {instruction.instruction_number} {'submitted for approval' if action_type == 'submit_approval' else 'saved as draft'}.",
            "instruction": {
                "id": str(instruction.id),
                "instructionNumber": instruction.instruction_number,
                "status": instruction.status,
                "supervisorId": str(supervisor.id),
                "supervisorName": supervisor.name,
                "startDate": instruction.expected_start_date or instruction.pi_date,
                "expectedCompletionDate": instruction.expected_completion_date,
                "remarks": instruction.notes or "",
                "orderMeter": instruction.order_meter,
                "finalPoNumber": getattr(instruction.final_po, "po_number", None) if instruction.final_po else None,
                "finalPoId": str(instruction.final_po_id) if instruction.final_po_id else None,
                "isVerified": instruction.status in ("Verified", "PO Pending", "PO Created", "Closed"),
                "completion": {
                    "totalProduced": comp["totalProduced"],
                    "acceptedQty": comp["acceptedQty"],
                    "rejectedQty": comp["rejectedQty"],
                    "shortageQty": comp["shortageQty"],
                    "finalAmount": comp["finalAmount"],
                },
            },
            "po": self.get_serializer(po).data,
        })

    @action(detail=True, methods=["get", "post", "delete"], url_path="qc-inspection")
    def qc_inspection(self, request, pk=None):
        po = self.get_object()
        from apps.pms.models import ProductionInstruction, DailyProductionEntry
        from apps.inventory.models import QCInspection
        from apps.hrms.models import Employee
        from apps.core.numbering import allocate_number
        from datetime import timedelta
        from django.utils.dateparse import parse_date

        lines = po.line_items.filter(deleted_at__isnull=True)
        total_po_qty = sum((D(l.qty) for l in lines), ZERO)
        fabric_names = [(l.item.name if l.item else (l.description or "Grey Fabric")) for l in lines]
        unique_fabrics = list(dict.fromkeys(fabric_names))
        fabric_name = ", ".join(unique_fabrics) if unique_fabrics else (po.notes or "Grey Fabric")
        vendor_name = po.party_name or (po.party.name if po.party else "")

        all_pis = list(ProductionInstruction.objects.filter(
            client_id=po.client_id,
            order_reference=po.po_number,
            deleted_at__isnull=True,
        ).order_by("created_at"))

        instruction_id = request.GET.get("instructionId") or (request.data.get("instructionId") if request.method == "POST" else None)
        active_pi = None
        if instruction_id:
            active_pi = next((p for p in all_pis if str(p.id) == str(instruction_id)), None)
            if not active_pi:
                active_pi = ProductionInstruction.objects.filter(
                    client_id=po.client_id, id=instruction_id, deleted_at__isnull=True
                ).first()
        if not active_pi and all_pis:
            active_pi = all_pis[-1]

        if active_pi:
            total_produced = DailyProductionEntry.objects.filter(
                instruction=active_pi,
                deleted_at__isnull=True,
            ).exclude(status="Cancelled").aggregate(value=Sum("meters"))["value"] or ZERO
        else:
            total_produced = ZERO

        if request.method == "DELETE":
            entry_id = request.GET.get("entryId") or request.GET.get("entry_id") or request.data.get("entryId") or request.data.get("entry_id")
            if not entry_id:
                raise ValidationFailed("Entry ID required for deletion.")
            qc_entry = QCInspection.objects.filter(
                client_id=po.client_id,
                id=entry_id,
                deleted_at__isnull=True,
            ).first()
            if not qc_entry:
                raise ValidationFailed("QC Inspection record not found.")
            if qc_entry.status not in ("Draft", "Pending"):
                raise Conflict("Only Draft QC entries can be deleted.")
            qc_entry.deleted_at = timezone.now()
            qc_entry.save(update_fields=["deleted_at", "updated_at"])
            return Response({"success": True, "message": "QC entry deleted successfully."})

        created_qc_entry = None
        if request.method == "POST":
            data = request.data or {}
            action_type = data.get("action") or ("save_draft" if data.get("status") == "Draft" else "submit")
            entry_id = data.get("entryId") or data.get("entry_id")

            if action_type == "submitDraft" and entry_id:
                qc_entry = QCInspection.objects.filter(
                    client_id=po.client_id, id=entry_id, deleted_at__isnull=True
                ).first()
                if not qc_entry:
                    raise ValidationFailed("QC Inspection entry not found.")
                qc_entry.status = "Approved"
                if request.user.is_authenticated:
                    qc_entry.updated_by = request.user
                qc_entry.save(update_fields=["status", "updated_at"])
                return Response({"success": True, "message": f"QC Entry {qc_entry.inspection_number} submitted successfully."})

            raw_date = data.get("inspectionDate") or data.get("inspection_date")
            insp_date = parse_date(str(raw_date)) if raw_date else timezone.localdate()
            if not insp_date:
                insp_date = timezone.localdate()
            if insp_date > timezone.localdate():
                raise ValidationFailed("Future inspection dates are not permitted.", field_errors={"inspectionDate": ["Future dates not allowed."]})

            inspector_id = data.get("inspectorId") or data.get("inspector_id") or data.get("inspector")
            if not inspector_id:
                raise ValidationFailed("Inspector / QC Incharge is required.", field_errors={"inspectorId": ["Select an inspector."]})

            inspector_emp = Employee.objects.filter(
                client_id=po.client_id, id=inspector_id, deleted_at__isnull=True
            ).first()
            inspector_user = None
            if not inspector_emp:
                from apps.accounts.models import User
                inspector_user = User.objects.filter(client_id=po.client_id, id=inspector_id).first()
                if not inspector_user:
                    raise ValidationFailed("Selected inspector does not exist.", field_errors={"inspectorId": ["Invalid inspector."]})
            else:
                inspector_user = getattr(inspector_emp, "user", None)

            inspected_qty = D(data.get("inspectedQty") or data.get("inspected_quantity") or 0)
            if inspected_qty <= ZERO:
                raise ValidationFailed("Inspected Quantity must be greater than zero.", field_errors={"inspectedQty": ["Enter a valid inspected quantity."]})

            accepted_qty = D(data.get("acceptedQty") or data.get("accepted_quantity") or 0)
            rejected_qty = D(data.get("rejectedQty") or data.get("rejected_quantity") or 0)
            reprocess_qty = D(data.get("reprocessQty") or data.get("reprocess_quantity") or 0)
            hold_qty = D(data.get("holdQty") or data.get("hold_quantity") or 0)

            if accepted_qty < ZERO or rejected_qty < ZERO or reprocess_qty < ZERO or hold_qty < ZERO:
                raise ValidationFailed("Quantity result values cannot be negative.")

            reconciled = accepted_qty + rejected_qty + reprocess_qty + hold_qty
            if reconciled != inspected_qty:
                raise ValidationFailed(
                    f"Quantity reconciliation mismatch: breakdown sum ({reconciled} m) does not equal Inspected Quantity ({inspected_qty} m).",
                    field_errors={"acceptedQty": ["Breakdown sum must match Inspected Quantity."]}
                )

            existing_qc = QCInspection.objects.filter(
                client_id=po.client_id,
                deleted_at__isnull=True,
            ).filter(
                Q(purchase_order=po) | (Q(production_instruction=active_pi) if active_pi else Q(source_document_number=po.po_number))
            ).exclude(status="Draft")

            already_inspected = existing_qc.aggregate(val=Sum("total_inspected_qty"))["val"] or ZERO
            eligible_for_qc = max(ZERO, D(total_produced) - D(already_inspected))

            if action_type != "save_draft" and inspected_qty > eligible_for_qc and total_produced > ZERO:
                raise ValidationFailed(
                    f"Inspected quantity ({inspected_qty} m) exceeds pending produced quantity eligible for QC ({eligible_for_qc} m).",
                    field_errors={"inspectedQty": [f"Cannot inspect more than eligible pending production ({eligible_for_qc} m)."]}
                )

            recent = QCInspection.objects.filter(
                client_id=po.client_id,
                purchase_order=po,
                total_inspected_qty=inspected_qty,
                created_at__gte=timezone.now() - timedelta(seconds=5),
            ).first()
            if recent:
                raise Conflict("A duplicate QC entry was just submitted. Please wait a moment.")

            is_draft = (action_type == "save_draft")
            inspection_number = allocate_number(po.client, "QC", insp_date)

            status_val = "Draft" if is_draft else "Approved"
            if data.get("status") and data.get("status") not in ("Draft",):
                status_val = "Approved"

            created_qc_entry = QCInspection.objects.create(
                client=po.client,
                purchase_order=po,
                production_instruction=active_pi,
                source_document_type="PO",
                source_document_id=po.id,
                source_document_number=po.po_number,
                inspection_number=inspection_number,
                inspection_date=insp_date,
                inspector=inspector_user if inspector_user else (request.user if request.user.is_authenticated else None),
                inspector_employee=inspector_emp,
                status=status_val,
                total_received_qty=inspected_qty,
                total_inspected_qty=inspected_qty,
                passed_qty=accepted_qty,
                rejected_qty=rejected_qty,
                reprocess_qty=reprocess_qty,
                hold_qty=hold_qty,
                inspection_type=data.get("inspectionType") or data.get("inspection_type") or "Grey Fabric QC",
                inspection_method=data.get("inspectionMethod") or data.get("inspection_method") or "Visual + Measurement",
                gsm=data.get("gsm") or None,
                width=data.get("width") or None,
                defects_found=data.get("defectsFound") or data.get("defects_found") or "",
                quality_grade=data.get("qualityGrade") or data.get("quality_grade") or "Grade A",
                notes=data.get("remarks") or "",
                photos=data.get("photos") or [],
                created_by=request.user if request.user.is_authenticated else None,
            )

            self.write_audit("qc_inspection_create", po, description=f"QC Entry {inspection_number} created ({'Draft' if is_draft else 'Submitted'}).")

        qc_qs = QCInspection.objects.filter(
            client_id=po.client_id,
            deleted_at__isnull=True,
        ).filter(
            Q(purchase_order=po) | (Q(production_instruction=active_pi) if active_pi else Q(source_document_number=po.po_number))
        ).order_by("-inspection_date", "-created_at")

        valid_qc = [q for q in qc_qs if q.status != "Draft"]
        tot_inspected = sum((D(q.total_inspected_qty) for q in valid_qc), ZERO)
        acc_qty = sum((D(q.passed_qty) for q in valid_qc), ZERO)
        rej_qty = sum((D(q.rejected_qty) for q in valid_qc), ZERO)
        rep_qty = sum((D(q.reprocess_qty) for q in valid_qc), ZERO)
        hld_qty = sum((D(q.hold_qty) for q in valid_qc), ZERO)

        pending_for_qc = max(ZERO, D(total_produced) - D(tot_inspected))
        completion_pct = round(float((tot_inspected / total_produced) * 100), 1) if total_produced > ZERO else (100.0 if tot_inspected > ZERO else 0.0)

        entries_list = []
        for q in qc_qs:
            insp_name = getattr(q.inspector_employee, "name", None) or getattr(q.inspector, "name", None) or getattr(q.inspector, "email", "Inspector")
            entries_list.append({
                "id": str(q.id),
                "inspectionNumber": q.inspection_number,
                "inspection_number": q.inspection_number,
                "date": str(q.inspection_date),
                "inspectionDate": str(q.inspection_date),
                "inspectedQty": float(q.total_inspected_qty),
                "inspectedQuantity": float(q.total_inspected_qty),
                "acceptedQty": float(q.passed_qty),
                "acceptedQuantity": float(q.passed_qty),
                "rejectedQty": float(q.rejected_qty),
                "rejectedQuantity": float(q.rejected_qty),
                "reprocessQty": float(q.reprocess_qty),
                "reprocessQuantity": float(q.reprocess_qty),
                "holdQty": float(q.hold_qty),
                "holdQuantity": float(q.hold_qty),
                "inspectorName": insp_name,
                "inspectorId": str(q.inspector_employee_id or q.inspector_id or ""),
                "grade": q.quality_grade or "Grade A",
                "qualityGrade": q.quality_grade or "Grade A",
                "defectsFound": q.defects_found or "None / Clear",
                "inspectionType": q.inspection_type or "Grey Fabric QC",
                "inspectionMethod": q.inspection_method or "Visual + Measurement",
                "gsm": float(q.gsm) if q.gsm is not None else None,
                "width": float(q.width) if q.width is not None else None,
                "remarks": q.notes or "",
                "photos": q.photos or [],
                "status": q.status,
                "createdAt": q.created_at.isoformat() if q.created_at else None,
            })

        active_inspectors = [
            {"id": str(e.id), "name": e.name, "code": e.employee_code}
            for e in Employee.objects.filter(client_id=po.client_id, status__in=["Active", "Probation"], deleted_at__isnull=True).order_by("name")
        ]
        if not active_inspectors:
            from apps.accounts.models import User
            active_inspectors = [
                {"id": str(u.id), "name": u.name or u.email, "code": u.email.split("@")[0]}
                for u in User.objects.filter(client_id=po.client_id, status="Active").order_by("name")
            ]

        fabric_quality = (active_pi.fabric if (active_pi and active_pi.fabric) else fabric_name)

        resp_payload = {
            "success": True,
            "linkedDetails": {
                "poNumber": po.po_number,
                "instructionNumber": active_pi.instruction_number if active_pi else "PI-Pending",
                "instructionId": str(active_pi.id) if active_pi else None,
                "vendorName": vendor_name,
                "vendorState": getattr(po.party, "billing_state", "Maharashtra") if po.party else "Maharashtra",
                "fabricQuality": fabric_quality,
                "poQuantity": float(total_po_qty),
                "poQuantityMtr": float(total_po_qty),
                "totalProduced": float(total_produced),
                "totalProducedMtr": float(total_produced),
                "eligibleForQc": float(pending_for_qc),
                "pendingForQcMtr": float(pending_for_qc),
            },
            "summary": {
                "totalProduced": float(total_produced),
                "totalProducedMtr": float(total_produced),
                "totalInspected": float(tot_inspected),
                "totalInspectedMtr": float(tot_inspected),
                "acceptedQty": float(acc_qty),
                "totalAcceptedMtr": float(acc_qty),
                "rejectedQty": float(rej_qty),
                "totalRejectedMtr": float(rej_qty),
                "reprocessQty": float(rep_qty),
                "totalReprocessMtr": float(rep_qty),
                "holdQty": float(hld_qty),
                "totalHoldMtr": float(hld_qty),
                "pendingForInspection": float(pending_for_qc),
                "pendingForInspectionMtr": float(pending_for_qc),
                "completionPercentage": completion_pct,
            },
            "entries": entries_list,
            "inspectors": active_inspectors,
        }
        if created_qc_entry:
            resp_payload["entry"] = next((e for e in entries_list if e["id"] == str(created_qc_entry.id)), None)
            return Response(resp_payload, status=status.HTTP_201_CREATED)

        return Response(resp_payload)

    @action(detail=False, methods=["get"], url_path="next-jwpo-number")
    def next_jwpo_number(self, request):
        from apps.core.numbering import financial_year_label
        client = getattr(request.user, "client", None)
        year_str = financial_year_label(client, reset_policy="fy") if client else str(timezone.localdate().year)

        last_po = PurchaseOrder.objects.filter(
            client_id=request.client_id,
        ).filter(
            models.Q(po_number__startswith="FJWPO") | models.Q(po_number__startswith="JWPO")
        ).order_by("-po_number").first()

        seq = 1
        if last_po and last_po.po_number:
            parts = last_po.po_number.split("-")
            if len(parts) >= 3 and parts[-1].isdigit():
                seq = int(parts[-1]) + 1

        next_no = f"FJWPO-{year_str}-{seq:04d}"
        return Response({"nextPoNumber": next_no, "poNo": next_no})

    @action(detail=False, methods=["post"], url_path="generate-final-job-work")
    @transaction.atomic
    def generate_final_job_work(self, request):
        from apps.pms import production_services
        from apps.pms.models import ProductionInstruction

        instruction_id = request.data.get("instructionId") or request.data.get("instruction_id")
        if instruction_id:
            instruction = ProductionInstruction.objects.filter(
                client_id=request.client_id, id=instruction_id, deleted_at__isnull=True
            ).first()
            if not instruction:
                raise ValidationFailed(f"Production instruction {instruction_id} not found.")

            instruction = production_services.generate_final_job_work_po(
                instruction, user=request.user, custom_data=request.data
            )
            po = instruction.final_po
            return Response(self.get_serializer(po).data, status=status.HTTP_201_CREATED)
        else:
            from apps.masters.models import Party, Item
            vendor_id = request.data.get("vendorId")
            vendor = Party.objects.filter(client_id=request.client_id, id=vendor_id).first() if vendor_id else None
            if not vendor:
                vendor = Party.objects.filter(client_id=request.client_id, type__in=["Vendor", "Both"]).first()

            po_no = request.data.get("finalPoNo") or allocate_number(request.user.client, "JWPO")
            po = PurchaseOrder(
                client=request.user.client,
                po_number=po_no,
                party=vendor,
                doc_date=request.data.get("poDate") or timezone.localdate(),
                status="Pending Approval" if request.data.get("sendToApproval", True) else "Approved",
                notes=request.data.get("remarks") or "Final Job Work Purchase Order",
                purchase_type=PURCHASE_TYPE_FINAL_JOB_WORK,
                other_charges=Decimal(str(request.data.get("otherCharges") or 0)),
                created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
            )
            po.freeze_party_snapshot()
            po.save()

            items = request.data.get("items") or []
            default_item = Item.objects.filter(client_id=request.client_id, deleted_at__isnull=True).exclude(item_kind="Service").first()
            from apps.sales.services import recalculate_document
            for idx, it in enumerate(items, start=1):
                qty = Decimal(str(it.get("billableQty") or it.get("qty") or 100))
                rate = Decimal(str(it.get("rate") or 0))
                tax_pct = Decimal(str(it.get("gstPct") or 5))
                line = PurchaseOrderLine(
                    client=request.user.client,
                    purchase_order=po,
                    line_no=idx,
                    item=default_item,
                    description=it.get("description") or f"Job Work - {default_item.name if default_item else 'Processing'}",
                    qty=qty,
                    rate=rate,
                    tax_pct=tax_pct,
                    uom=it.get("uom") or "Meter",
                )
                line.freeze_item_snapshot()
                line.save()
            recalculate_document(po)
            return Response(self.get_serializer(po).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="final-job-work-list")
    def final_job_work_list(self, request):
        qs = self.get_queryset().filter(
            Q(po_number__startswith="JWPO")
            | Q(reference_number__startswith="PROD-")
            | Q(production_instructions__isnull=False)
            | Q(notes__icontains="Job Work")
        ).distinct()
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="auto-suggestions")
    def auto_suggestions(self, request):
        """``GET /purchase/orders/auto-suggestions/`` -- the AutoPOModal feed."""
        return Response(envelope(services.auto_po_suggestions(request.client_id)))


# ---------------------------------------------------------------------------
# Purchase bills (api.md §6.4)
# ---------------------------------------------------------------------------
class PurchaseBillViewSet(SalesDocumentViewSet):
    queryset = PurchaseBill.objects.all()
    serializer_class = PurchaseBillSerializer
    audit_entity_type = "PurchaseBill"
    audit_label_field = "bill_number"
    status_field = "status"
    print_title = "Purchase Bill"
    filter_map = {
        "vendorId": "party_id",
        "purchaseOrderId": "purchase_order_id",
        "poId": "purchase_order_id",
        "po": "purchase_order_id",
        "goodsReceived": "goods_received",
        "qcStatus": "qc_status",
    }
    permission_map = {"read": ["view_purchase"], "write": ["create_bill"]}

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalValue=money_sum("total"),
            paid=money_sum("amount_paid"),
            awaitingReceipt=Count("id", filter=Q(goods_received=False)),
        )
        rows["outstanding"] = round2(rows["totalValue"] - rows["paid"])
        return rows

    def perform_create(self, serializer):
        serializer.validated_data["bill_number"] = allocate_number(
            self.request.user.client, "BILL", serializer.validated_data.get("doc_date")
        )
        bill = super().perform_create(serializer)
        return bill

    def check_draft_only(self, instance, verb):
        if instance.goods_received:
            raise Conflict(
                f"Goods have been received against this bill, so it cannot be {verb}ed.",
                code=Codes.DRAFT_ONLY,
                detail="Cancel the bill instead, which reverses the stock and the ledger.",
            )
        super().check_draft_only(instance, verb)

    @action(detail=True, methods=["post"], url_path="receive-goods")
    def receive_goods(self, request, pk=None):
        """The weight-variance receive (api.md §6.3) -- ``receivePurchaseBillGoods``."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "receive_goods")

        serializer = ReceiveGoodsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = services.receive_bill_goods(
            self.get_object(),
            lines_payload=data["lines"],
            qc_status=data.get("qcStatus"),
            user=request.user,
            location=data.get("locationId"),
        )
        bill = result["bill"]
        self.write_audit(
            "receive",
            bill,
            description=(
                f"Goods received, QC {result['qcStatus']}"
                + (" (weight variance)" if result["varianceBreach"] else "")
            ),
        )
        return Response(
            {
                "bill": self.get_serializer(bill).data,
                "receipt": GoodsReceiptSerializer(result["receipt"]).data,
                "qcStatus": result["qcStatus"],
                "varianceBreach": result["varianceBreach"],
            }
        )

    @action(detail=True, methods=["post"])
    def qc(self, request, pk=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "approve_qc")

        serializer = QcSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bill = services.update_qc_status(
            self.get_object(),
            serializer.validated_data["status"],
            note=serializer.validated_data.get("note"),
            user=request.user,
        )
        self.write_audit(
            "qc", bill, description=f"QC set to {serializer.validated_data['status']}"
        )
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "cancel_purchase_document")

        bill = services.cancel_bill(
            self.get_object(), reason=request.data.get("reason"), user=request.user
        )
        self.write_audit("cancel", bill, description=request.data.get("reason"))
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["get"])
    def outstanding(self, request, pk=None):
        return Response(services.bill_outstanding(self.get_object()))

    @action(detail=True, methods=["post"], url_path="apply-advance")
    @transaction.atomic
    def apply_advance(self, request, pk=None):
        """``{ amount }`` from vendor advances (api.md §6.4)."""
        from apps.sales.models import PaymentAllocation

        bill = PurchaseBill.objects.select_for_update().get(pk=self.get_object().pk)
        amount = round2(request.data.get("amount"))
        if amount <= ZERO:
            raise ValidationFailed(
                "Enter an amount to apply.",
                field_errors={"amount": ["Must be greater than zero."]},
            )
        if bill.status in ("Draft", "Cancelled") or bill.posted_at is None:
            raise BusinessRuleViolation(
                "An advance can only be applied to a posted, open bill.",
                code=Codes.PAYMENT_ON_DRAFT if bill.status != "Cancelled" else Codes.PAYMENT_ON_CANCELLED,
            )
        outstanding = services.bill_outstanding(bill)["outstanding"]
        if amount > outstanding + ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                f"Only {outstanding} is outstanding on this bill.",
                code=Codes.PAYMENT_EXCEEDS_BALANCE,
            )

        advance = services.vendor_advance_balance(request.client_id, bill.party_id)
        if amount > D(advance["advanceBalance"]):
            raise BusinessRuleViolation(
                f"Only {advance['advanceBalance']} is available as an advance.",
                code=Codes.PAYMENT_EXCEEDS_BALANCE,
                payload=advance,
            )

        remaining = amount
        payments = PaymentOut.objects.select_for_update().filter(
            client_id=request.client_id, party_id=bill.party_id, status="Active",
            deleted_at__isnull=True,
        ).filter(allocated_amount__lt=F("amount")).order_by("payment_date")

        for payment in payments:
            if remaining <= ZERO:
                break
            available = min(payment.unallocated_amount, remaining)
            if available <= ZERO:
                continue
            PaymentAllocation.objects.create(
                client_id=request.client_id,
                payment_id=payment.id,
                payment_side="out",
                document_type="PurchaseBill",
                document_id=bill.id,
                amount=available,
                allocated_by=request.user,
            )
            payment.allocated_amount = round2(D(payment.allocated_amount) + available)
            payment.save(update_fields=["allocated_amount", "updated_at"])
            remaining -= available

        services.refresh_bill_payment_status(bill)
        bill.refresh_from_db()
        return Response(self.get_serializer(bill).data)


class GoodsReceiptViewSet(TenantModelViewSet):
    """``GET /purchase/receipts/`` and ``POST /purchase/receipts/`` (api.md §6.3, Phase 2.2).

    Supports standalone GRN creation against POs or suppliers, worklist filtering,
    reconciliation report, and supplier ledger.
    """

    queryset = GoodsReceipt.objects.select_related("party", "purchase_bill", "purchase_order", "location").prefetch_related("lines", "lines__item")
    serializer_class = GoodsReceiptSerializer
    required_permissions = ["view_purchase"]
    permission_map = {"read": ["view_purchase"], "write": ["receive_goods"]}
    status_field = "qc_status"
    default_date_field = "receipt_date"
    ordering = ["-receipt_date", "-created_at"]
    filter_map = {
        "partyId": "party_id",
        "vendorId": "party_id",
        "purchaseOrderId": "purchase_order_id",
        "poId": "purchase_order_id",
        "po": "purchase_order_id",
        "qcStatus": "qc_status",
    }

    def list(self, request, *args, **kwargs):
        if request.query_params.get("worklist") == "true":
            pending_bills = PurchaseBill.objects.filter(
                client_id=request.client_id, deleted_at__isnull=True, goods_received=False
            ).exclude(status="Cancelled").select_related("party")

            held_receipts = self.filter_queryset(self.get_queryset()).exclude(
                qc_status="Approved"
            )

            rows = [
                {
                    "kind": "bill",
                    "id": str(bill.id),
                    "billId": str(bill.id),
                    "billNumber": bill.bill_number,
                    "vendorName": bill.party_name or bill.party.name,
                    "date": bill.doc_date,
                    "total": round2(bill.total),
                    "qcStatus": bill.qc_status,
                    "goodsReceived": False,
                }
                for bill in pending_bills
            ] + [
                {
                    "kind": "receipt",
                    "id": str(receipt.id),
                    "billId": str(receipt.purchase_bill_id) if receipt.purchase_bill_id else None,
                    "billNumber": (
                        receipt.purchase_bill.bill_number if receipt.purchase_bill_id else None
                    ),
                    "grnNumber": receipt.grn_number,
                    "vendorName": receipt.party.name,
                    "date": receipt.receipt_date,
                    "qcStatus": receipt.qc_status,
                    "goodsReceived": True,
                }
                for receipt in held_receipts
            ]

            return Response(
                envelope(
                    rows,
                    aggregates={
                        "awaitingReceipt": pending_bills.count(),
                        "heldAtQc": held_receipts.count(),
                    },
                )
            )
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        from apps.core.permissions import require_permission

        require_permission(request.user, "receive_goods")

        data = request.data
        party_id = data.get("party") or data.get("partyId") or data.get("vendorId")
        location_id = data.get("location") or data.get("locationId")
        receipt_date = data.get("receipt_date") or data.get("receiptDate") or data.get("date")
        supplier_challan_no = data.get("supplier_challan_no") or data.get("supplierChallanNo")
        supplier_challan_date = data.get("supplier_challan_date") or data.get("supplierChallanDate")
        purchase_order_id = data.get("purchase_order") or data.get("purchaseOrderId") or data.get("poId")
        lines = data.get("lines") or []

        # A double-clicked Save replays the first response instead of posting
        # the stock twice (api.md §1.8).
        try:
            record = idempotency.begin(request, request.client_id, request.path, request.data)
        except idempotency.Replay as replay:
            response = Response(replay.body, status=replay.status)
            response["Idempotency-Replayed"] = "true"
            return response
        try:
            with transaction.atomic():
                receipt = services.create_standalone_grn(
                    request.user.client,
                    party_id=party_id,
                    location_id=location_id,
                    receipt_date=receipt_date,
                    supplier_challan_no=supplier_challan_no,
                    supplier_challan_date=supplier_challan_date,
                    purchase_order_id=purchase_order_id,
                    lines_payload=lines,
                    user=request.user,
                )
                self.write_audit("create", receipt, description=f"Standalone GRN created: {receipt.grn_number}")
        except Exception:
            idempotency.fail(record)
            raise
        serializer = self.get_serializer(receipt)
        return idempotency.finish(
            record, Response(serializer.data, status=status.HTTP_201_CREATED), receipt
        )

    @action(detail=False, methods=["get"], url_path="reconciliation")
    def reconciliation(self, request):
        vendor_id = request.query_params.get("vendorId") or request.query_params.get("vendor_id")
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        data = services.supplier_reconciliation(
            request.client_id,
            vendor_id=vendor_id,
            date_from=date_from,
            date_to=date_to,
        )
        return Response(data)

    @action(detail=False, methods=["get"], url_path="supplier-ledger")
    def supplier_ledger(self, request):
        vendor_id = request.query_params.get("vendorId") or request.query_params.get("vendor_id")
        if not vendor_id:
            raise ValidationFailed("vendorId query parameter is required.", field_errors={"vendorId": ["Required."]})
        date_from = request.query_params.get("dateFrom") or request.query_params.get("date_from")
        date_to = request.query_params.get("dateTo") or request.query_params.get("date_to")
        data = services.supplier_ledger(
            request.client_id,
            vendor_id=vendor_id,
            date_from=date_from,
            date_to=date_to,
        )
        return Response(data)

    @action(detail=True, methods=["post"])
    def qc(self, request, pk=None):
        """``POST /purchase/receipts/{id}/qc/`` -- ``{ status, note }``."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "approve_qc")

        receipt = self.get_object()
        serializer = QcSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # The verdict moves the goods out of QC Hold with real movements and
        # updates receipt, bill and taka statuses together (Phase 1.4).
        from apps.inventory import qc as qc_service

        qc_service.dispose_all(
            receipt,
            serializer.validated_data["status"],
            user=request.user,
            note=serializer.validated_data.get("note"),
        )
        receipt.refresh_from_db()

        return Response(self.get_serializer(receipt).data)


# ---------------------------------------------------------------------------
# Payments out, returns, expenses (api.md §6.5)
# ---------------------------------------------------------------------------
class PaymentOutViewSet(TenantModelViewSet):
    queryset = PaymentOut.objects.select_related("party", "bank_account")
    serializer_class = PaymentOutSerializer
    audit_entity_type = "PaymentOut"
    audit_label_field = "payment_number"
    idempotent_create = True
    status_field = "status"
    default_date_field = "payment_date"
    search_fields = ["payment_number", "reference_number", "party__name"]
    ordering = ["-payment_date", "-created_at"]
    filter_map = {"vendorId": "party_id", "mode": "mode"}
    permission_map = {"read": ["view_purchase"], "write": ["record_payment_out"]}
    # A payment is posted on create; it is changed only by /cancel/, never
    # edited or deleted (its ledger entry and allocations would not follow).
    http_method_names = ["get", "post", "head", "options"]

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalPaid=money_sum("amount"),
            allocated=money_sum("allocated_amount"),
        )
        rows["advances"] = round2(rows["totalPaid"] - rows["allocated"])
        return rows

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        bill = None
        bill_id = self.request.data.get("billId")
        if bill_id:
            bill = PurchaseBill.objects.filter(
                pk=bill_id, client_id=self.get_client_id(), deleted_at__isnull=True
            ).first()
            if bill is None:
                raise NotFound("That bill no longer exists.")

        payment = services.record_payment_out(
            client=self.request.user.client,
            party=data["party"],
            amount=data["amount"],
            payment_date=data["payment_date"],
            mode=data["mode"],
            bank_account=data.get("bank_account"),
            reference_number=data.get("reference_number"),
            notes=data.get("notes"),
            allocations=self.request.data.get("allocations") or [],
            bill=bill,
            user=self.request.user,
        )
        serializer.instance = payment
        self._created_instance = payment
        self._concurrency_instance = payment
        self.write_audit("create", payment, description="Payment recorded")
        return payment

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        payment = services.cancel_payment_out(
            self.get_object(), reason=request.data.get("reason"), user=request.user
        )
        self.write_audit("cancel", payment, description=request.data.get("reason"))
        return Response(self.get_serializer(payment).data)


class PurchaseReturnViewSet(PostedReturnGuardMixin, SalesDocumentViewSet):
    queryset = PurchaseReturn.objects.select_related("purchase_bill")
    serializer_class = PurchaseReturnSerializer
    audit_entity_type = "PurchaseReturn"
    audit_label_field = "return_number"
    status_field = "status"
    print_title = "Debit Note"
    draft_only_writes = False
    filter_map = {"vendorId": "party_id", "purchaseBillId": "purchase_bill_id"}
    permission_map = {"read": ["view_purchase"], "write": ["cancel_purchase_document"]}

    @transaction.atomic
    def perform_create(self, serializer):
        from apps.accounting import services as ledger

        bill = serializer.validated_data["purchase_bill"]
        if bill.status == "Cancelled":
            raise BusinessRuleViolation(
                "Cannot return against a cancelled bill.",
                code=Codes.PAYMENT_ON_CANCELLED,
            )

        serializer.validated_data["return_number"] = allocate_number(
            self.request.user.client, "PR", serializer.validated_data.get("doc_date")
        )
        serializer.validated_data["debit_note_number"] = serializer.validated_data[
            "return_number"
        ]
        serializer.validated_data.setdefault("party", bill.party)

        purchase_return = super().perform_create(serializer)
        location_id = purchase_return.location_id or bill.location_id

        for line in purchase_return.line_items.select_related(
            "purchase_bill_line", "item"
        ).all():
            bill_line = line.purchase_bill_line
            remaining = D(bill_line.qty) - D(bill_line.returned_qty)
            if D(line.returned_qty) > remaining + Decimal("0.0001"):
                raise BusinessRuleViolation(
                    f"{line.item_name}: only {round4(remaining)} remains returnable.",
                    code=Codes.OVER_RETURN,
                    payload={"billLineId": str(bill_line.id)},
                )
            bill_line.returned_qty = round4(
                D(bill_line.returned_qty) + D(line.returned_qty)
            )
            bill_line.save(update_fields=["returned_qty", "updated_at"])

            if line.item_id and line.item.holds_stock and location_id:
                # returned_qty is counted in the bill line's unit.
                factor = D(bill_line.uom_factor or 1)
                stock_qty = uom.to_stock_qty(line.returned_qty, factor)
                stock.assert_sufficient_stock(
                    purchase_return.client_id, line.item, stock_qty,
                    location_id, line.item_name,
                )
                stock.post_movement(
                    client_id=purchase_return.client_id,
                    item=line.item,
                    location=location_id,
                    type="PURCHASE_RETURN",
                    quantity=-stock_qty,
                    unit_cost=(
                        D(bill_line.landed_unit_cost) / factor
                        if bill_line.landed_unit_cost else line.item.cost_price
                    ),
                    reference_type="PurchaseReturn",
                    reference_id=purchase_return.id,
                    reference_number=purchase_return.return_number,
                    movement_date=purchase_return.doc_date,
                    user=self.request.user,
                )

        entry = ledger.post_purchase_return(purchase_return, user=self.request.user)
        if entry is not None:
            purchase_return.journal_entry = entry
            purchase_return.save(update_fields=["journal_entry", "updated_at"])
        services.refresh_bill_payment_status(bill)

        # Handle lot/taka selection on return (Phase 2.2)
        taka_ids = self.request.data.get("taka_ids") or self.request.data.get("takaIds") or []
        if taka_ids:
            from apps.inventory.models import LotTaka
            for tid in taka_ids:
                taka = LotTaka.objects.filter(pk=tid, client_id=self.get_client_id()).first()
                if taka and taka.available_quantity > ZERO:
                    stock.post_movement(
                        client_id=purchase_return.client_id,
                        item=taka.item,
                        location=taka.location_id,
                        type="PURCHASE_RETURN",
                        quantity=-taka.available_quantity,
                        lot_taka=taka,
                        batch_number=taka.lot_number,
                        reference_type="PurchaseReturn",
                        reference_id=purchase_return.id,
                        reference_number=purchase_return.return_number,
                        movement_date=purchase_return.doc_date,
                        user=self.request.user,
                    )
                    taka.status = "Returned"
                    taka.available_quantity = ZERO
                    taka.save(update_fields=["status", "available_quantity", "updated_at"])

        return purchase_return

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        from apps.sales.services import _reverse_document

        purchase_return = self.get_object()
        if purchase_return.status == "Cancelled":
            raise Conflict("This return is already cancelled.", code=Codes.ALREADY_CANCELLED)

        reason = request.data.get("reason")
        _reverse_document(purchase_return, "PurchaseReturn", user=request.user, reason=reason)

        for line in purchase_return.line_items.select_related("purchase_bill_line").all():
            bill_line = line.purchase_bill_line
            bill_line.returned_qty = max(
                round4(D(bill_line.returned_qty) - D(line.returned_qty)), ZERO
            )
            bill_line.save(update_fields=["returned_qty", "updated_at"])

        self.write_audit("cancel", purchase_return, description=reason)
        return Response(self.get_serializer(purchase_return).data)


class ExpenseViewSet(TenantModelViewSet):
    queryset = Expense.objects.select_related("category", "party", "bank_account")
    serializer_class = ExpenseSerializer
    audit_entity_type = "Expense"
    audit_label_field = "expense_number"
    status_field = "status"
    default_date_field = "expense_date"
    search_fields = ["expense_number", "notes", "reference_number"]
    ordering = ["-expense_date"]
    filter_map = {"categoryId": "category_id", "vendorId": "party_id", "paymentMode": "payment_mode"}
    permission_map = {"read": ["view_purchase"], "write": ["view_purchase"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(count=Count("id"), totalValue=money_sum("total"))

    @transaction.atomic
    def perform_create(self, serializer):
        from apps.accounting import services as ledger

        data = serializer.validated_data
        data["expense_number"] = allocate_number(
            self.request.user.client, "EXP", data.get("expense_date")
        )
        data["total"] = round2(D(data.get("amount")) + D(data.get("tax_amount")))
        expense = super().perform_create(serializer)

        entry = ledger.post_expense(expense, user=self.request.user)
        if entry is not None:
            expense.journal_entry = entry
            expense.save(update_fields=["journal_entry", "updated_at"])
        return expense

    @transaction.atomic
    def perform_update(self, serializer):
        from apps.accounting import services as ledger

        expense = serializer.instance
        if expense.journal_entry_id:
            # A posted expense is corrected by reversing and re-posting, never
            # by editing the entry (db.md §8).
            ledger.reverse_entry(expense.journal_entry, user=self.request.user)

        serializer.validated_data["total"] = round2(
            D(serializer.validated_data.get("amount", expense.amount))
            + D(serializer.validated_data.get("tax_amount", expense.tax_amount))
        )
        expense = super().perform_update(serializer)

        entry = ledger.post_expense(expense, user=self.request.user)
        expense.journal_entry = entry
        expense.save(update_fields=["journal_entry", "updated_at"])
        return expense

    @transaction.atomic
    def perform_destroy(self, instance):
        from apps.accounting import services as ledger

        super().perform_destroy(instance)
        # A deleted expense leaves the books too (reversed, never erased).
        ledger.reverse_document_entries(
            client_id=instance.client_id,
            source_document_type="Expense",
            source_document_id=instance.id,
            user=self.request.user,
        )


class VendorBillViewSet(TenantModelViewSet):
    """Manual vendor-bill entry, matched later to a real ``PurchaseBill``.

    Creating or editing a vendor bill only captures the vendor's invoice --
    it never posts stock or ledger entries. ``send-for-matching`` creates the
    ``PurchaseBill`` Draft; the bill's own receive/cancel flow posts from
    there.
    """

    queryset = VendorBill.objects.all()
    serializer_class = VendorBillSerializer
    audit_entity_type = "VendorBill"
    audit_label_field = "vendor_bill_number"
    status_field = "status"
    default_date_field = "bill_date"
    allowed_date_fields = ("bill_date", "created_at")
    search_fields = ["vendor_bill_number", "party_name", "remarks"]
    ordering = ["-bill_date", "-created_at"]
    filter_map = {
        "vendorId": "party_id",
        "purchaseOrderId": "purchase_order_id",
        "poId": "purchase_order_id",
        "po": "purchase_order_id",
        "status": "status",
        "matchStatus": "match_status",
    }
    permission_map = {"read": ["view_purchase"], "write": ["create_bill"]}

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "party", "purchase_order", "goods_receipt", "purchase_bill",
                "approved_by",
            )
            .prefetch_related("line_items__item")
        )

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            count=Count("id"), totalValue=money_sum("total")
        )

    @action(detail=True, methods=["post"], url_path="send-for-matching")
    @transaction.atomic
    def send_for_matching(self, request, pk=None):
        """Create the ``PurchaseBill`` Draft from this vendor bill.

        Carries party, lines (qty/rate), the vendor bill number and the PO
        link (plus the PO's location); the Draft itself posts nothing.
        """
        bill = self.get_object()
        if bill.status == "Rejected":
            raise Conflict(
                "A rejected vendor bill cannot be sent for matching.",
                code=Codes.DRAFT_ONLY,
            )
        if bill.match_status == "Matched" or bill.purchase_bill_id:
            raise Conflict(
                "This vendor bill is already matched.",
                code=Codes.ALREADY_DONE,
            )

        lines = list(
            bill.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        )
        if not lines:
            raise ValidationFailed(
                "Add at least one line before matching.",
                field_errors={"lineItems": ["Add at least one line."]},
            )

        if bill.purchase_order_id is not None and bill.status != "Approved":
            matching = services.compute_vendor_bill_matching(
                request.client_id, bill
            )
            if matching.get("overallStatus") == "Mismatch":
                raise Conflict(
                    "Quantities or rates do not match the purchase order. "
                    "Approve the vendor bill before sending it for matching.",
                    code="BILL_MISMATCH",
                    payload={"matching": matching},
                )

        client = request.user.client
        purchase_order = bill.purchase_order
        purchase_bill = PurchaseBill(
            client_id=request.client_id,
            party=bill.party,
            doc_date=bill.bill_date,
            vendor_bill_number=bill.vendor_bill_number,
            purchase_order=purchase_order,
            location=purchase_order.location if purchase_order else None,
            status="Draft",
            bill_number=allocate_number(client, "BILL", bill.bill_date),
        )
        purchase_bill.freeze_party_snapshot()
        purchase_bill.save()

        PurchaseBillLine.objects.bulk_create(
            [
                PurchaseBillLine(
                    client_id=request.client_id,
                    purchase_bill=purchase_bill,
                    line_no=line.line_no,
                    item=line.item,
                    sku=line.sku or (line.item.sku if line.item else None),
                    item_name=line.item_name or (line.item.name if line.item else None),
                    uom=line.uom or (line.item.uom if line.item else None),
                    uom_factor=uom.line_factor(
                        request.client_id, line.item,
                        line.uom or (line.item.uom if line.item else None),
                    ),
                    qty=line.qty,
                    rate=line.rate,
                    tax_pct=bill.gst_pct,
                )
                for line in lines
            ]
        )

        from apps.sales.services import recalculate_document

        recalculate_document(purchase_bill)

        bill.purchase_bill = purchase_bill
        bill.match_status = "Matched"
        history = list(bill.approval_history or [])
        history.append(
            {
                "at": timezone.now().isoformat(),
                "by": getattr(request.user, "name", None)
                or getattr(request.user, "email", ""),
                "action": "Sent for Approval",
                "remarks": "",
            }
        )
        bill.approval_history = history
        bill.save(
            update_fields=[
                "purchase_bill", "match_status", "approval_history", "updated_at",
            ]
        )
        self.write_audit(
            "match", bill,
            description=f"PurchaseBill {purchase_bill.bill_number} created",
        )
        return Response(
            {
                "vendorBill": self.get_serializer(bill).data,
                "purchaseBillId": str(purchase_bill.id),
                "billNumber": purchase_bill.bill_number,
            },
            status=status.HTTP_201_CREATED,
        )

    MISMATCH_TYPE = "vendor_bill_mismatch"
    MISMATCH_APPROVER_ROLE_CODES = ["ACCOUNTS_MGR", "ACCOUNTS", "OWNER"]

    def _mismatch_gate(self, bill, matching, remarks):
        """None when this mismatched bill may be approved now; otherwise a 409
        with the approval request that was opened for it."""
        from apps.core.approval_services import raise_approval
        from apps.core.models import ApprovalRequest
        from apps.core.permissions import has_permission

        if has_permission(self.request.user, "approve_bill_mismatch"):
            return None
        summary = matching.get("summary") or {}
        fingerprint = {"billTotal": str(D(bill.total or 0)), "amountDiff": str(summary.get("amountDiff"))}
        approved = ApprovalRequest.objects.filter(
            client_id=bill.client_id,
            document_type=self.MISMATCH_TYPE,
            document_id=str(bill.id),
            status="Approved",
            new_values__system=True,
        )
        # An approval covers the bill only as it was when approved.
        if any(
            (req.new_values or {}).get("billTotal") == fingerprint["billTotal"]
            and (req.new_values or {}).get("amountDiff") == fingerprint["amountDiff"]
            for req in approved
        ):
            return None
        req = raise_approval(
            client_id=bill.client_id,
            document_type=self.MISMATCH_TYPE,
            document_id=bill.id,
            document_number=bill.vendor_bill_number,
            requested_by=self.request.user,
            reason=(
                f"Vendor bill {bill.vendor_bill_number} does not match PO/GRN: "
                + ("; ".join((matching.get("reasons") or [])[:3]) or "variance")
                + (f". Remarks: {remarks}" if remarks else "")
            ),
            approver_role_code=self.MISMATCH_APPROVER_ROLE_CODES[0],
            allowed_role_codes=self.MISMATCH_APPROVER_ROLE_CODES,
            new_values={**fingerprint, "reasons": matching.get("reasons") or []},
            variance_amount=D(str(summary.get("amountDiff") or 0)),
        )
        return Response(
            {
                "message": (
                    f"Bill {bill.vendor_bill_number} does not match and needs approval by accounts "
                    "management. The request is in the approvals inbox; approve the bill again once it is approved."
                ),
                "code": "APPROVAL_REQUIRED",
                "payload": {
                    "approvalRequestId": str(req.id),
                    "approverRoleCodes": self.MISMATCH_APPROVER_ROLE_CODES,
                    "matching": matching,
                },
            },
            status=status.HTTP_409_CONFLICT,
        )

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        # Lock the row: two simultaneous approvals must not create two payables.
        bill = VendorBill.objects.select_for_update().get(pk=self.get_object().pk)
        if bill.status != "Pending":
            raise Conflict(
                f"Only a pending bill can be approved. This bill is {bill.status}.",
                code=Codes.DRAFT_ONLY,
            )
        # Separation of duties: Creator cannot approve their own bill unless superuser
        if getattr(bill, "created_by_id", None) and bill.created_by_id == getattr(request.user, "id", None) and not getattr(request.user, "is_superuser", False):
            from apps.core.permissions import PermissionDenied
            raise PermissionDenied(
                "Separation of duties: You cannot approve a vendor bill you created.",
                code="SELF_APPROVAL_NOT_PERMITTED",
            )

        remarks = request.data.get("remarks")
        # Approving releases the payable even though PO / GRN / bill disagree,
        # so the variance and the reason are put on record. Nothing on the PO,
        # GRN or bill is changed to force a match.
        matching = services.compute_vendor_bill_matching(request.client_id, bill)
        is_mismatch = matching.get("overallStatus") == "Mismatch"
        if is_mismatch and not (remarks or "").strip():
            raise ValidationFailed(
                "This bill does not match the commercial records. Enter approval remarks explaining the variance.",
                field_errors={"remarks": ["Required to approve a mismatched bill."]},
                payload={"matching": matching},
            )
        if is_mismatch:
            # A mismatched bill releases a payable the records do not support,
            # so it needs approve_bill_mismatch (PDF §16) -- either held by the
            # user approving now, or given through the approvals inbox.
            blocked = self._mismatch_gate(bill, matching, remarks)
            if blocked is not None:
                return blocked
        now = timezone.now()
        bill.status = "Approved"
        if remarks is not None:
            bill.approval_remarks = remarks
        bill.approved_by = request.user
        bill.approved_at = now
        bill.match_result = matching
        history = list(bill.approval_history or [])
        history.append(
            {
                "at": now.isoformat(),
                "by": getattr(request.user, "name", None) or str(request.user),
                "action": "Approved with variance" if is_mismatch else "Approved",
                "remarks": remarks,
                "matchStatus": matching.get("overallStatus"),
                "variances": matching.get("reasons") or [],
            }
        )
        bill.approval_history = history

        auto_create_pb = request.data.get("createPayable", False)
        if auto_create_pb and bill.purchase_bill_id is None:
            final_po_data = matching.get("finalJobWorkPo")
            final_po_id = final_po_data.get("id") if final_po_data else None
            effective_po = PurchaseOrder.objects.filter(pk=final_po_id, client_id=bill.client_id).first() if final_po_id else bill.purchase_order
            lines = bill.line_items.filter(deleted_at__isnull=True).order_by("line_no")
            purchase_bill = PurchaseBill.objects.create(
                client=bill.client,
                party=bill.party,
                purchase_order=effective_po,
                doc_date=bill.bill_date,
                status="Unpaid",
                bill_number=allocate_number(bill.client, "BILL", bill.bill_date),
                vendor_bill_number=bill.vendor_bill_number,
                location_id=getattr(effective_po, "location_id", None),
                notes=remarks or bill.remarks or f"Recorded from Vendor Bill {bill.vendor_bill_number}",
            )
            for line in lines:
                PurchaseBillLine.objects.create(
                    client=bill.client,
                    purchase_bill=purchase_bill,
                    line_no=line.line_no,
                    item=line.item,
                    description=line.item_name or (line.item.name if line.item else "Fabric"),
                    qty=line.qty,
                    rate=line.rate,
                    tax_pct=bill.gst_pct,
                    uom=line.uom or "Meter",
                )
            from apps.sales.services import recalculate_document
            from apps.accounting import services as ledger
            from .models import PURCHASE_TYPE_FINAL_JOB_WORK

            purchase_bill.freeze_party_snapshot()
            recalculate_document(purchase_bill)
            # The payable is real from here on, so it is posted like any other
            # bill: Dr Inventory/Purchases + GST Input | Cr Creditors.
            purchase_bill.posted_at = now
            purchase_bill.posted_by = request.user if getattr(request.user, "is_authenticated", False) else None
            # Job-work fabric is stocked at production verification and GRN-linked
            # goods are already in stock; "Receive goods" on this bill would
            # post them a second time.
            if bill.goods_receipt_id or getattr(effective_po, "purchase_type", None) == PURCHASE_TYPE_FINAL_JOB_WORK:
                purchase_bill.goods_received = True
                purchase_bill.received_date = timezone.localdate()
            purchase_bill.save()
            entry = ledger.post_purchase_bill(purchase_bill, user=request.user)
            if entry is not None:
                purchase_bill.journal_entry = entry
                purchase_bill.save(update_fields=["journal_entry", "updated_at"])
            bill.purchase_bill = purchase_bill
            bill.match_status = "Matched"

        save_fields = [
            "status", "approval_remarks", "approved_by", "approved_at",
            "approval_history", "match_result", "updated_at",
        ]
        if bill.purchase_bill_id is not None:
            save_fields.extend(["purchase_bill", "match_status"])
        bill.save(update_fields=save_fields)
        description = remarks
        if is_mismatch:
            description = f"Approved with variance: {remarks}. " + "; ".join(matching.get("reasons") or [])
        self.write_audit("approve", bill, description=description)
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def reject(self, request, pk=None):
        bill = self.get_object()
        if bill.status != "Pending":
            raise Conflict(
                f"Only a pending bill can be rejected. This bill is {bill.status}.",
                code=Codes.DRAFT_ONLY,
            )
        remarks = request.data.get("remarks", request.data.get("reason"))
        now = timezone.now()
        bill.status = "Rejected"
        if remarks is not None:
            bill.approval_remarks = remarks
        bill.approved_by = request.user
        bill.approved_at = now
        history = list(bill.approval_history or [])
        history.append(
            {
                "at": now.isoformat(),
                "by": getattr(request.user, "name", None) or str(request.user),
                "action": "Rejected",
                "remarks": remarks,
            }
        )
        bill.approval_history = history
        bill.save(
            update_fields=[
                "status", "approval_remarks", "approved_by", "approved_at",
                "approval_history", "updated_at",
            ]
        )
        self.write_audit("reject", bill, description=remarks)
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["get"], url_path="matching")
    def matching(self, request, pk=None):
        """Live PO/GRN comparison for this vendor bill (no DB writes)."""
        bill = self.get_object()
        return Response(
            services.compute_vendor_bill_matching(request.client_id, bill)
        )

    @action(detail=True, methods=["post"], url_path="recheck-matching")
    @transaction.atomic
    def recheck_matching(self, request, pk=None):
        """Recompute the comparison and persist it on ``match_result``."""
        bill = self.get_object()
        result = services.compute_vendor_bill_matching(request.client_id, bill)
        bill.match_result = result
        bill.save(update_fields=["match_result", "updated_at"])
        return Response(result)

    @action(detail=True, methods=["get"], url_path="approval-history")
    def approval_history(self, request, pk=None):
        bill = self.get_object()
        return Response({"history": bill.approval_history or []})

    @action(detail=False, methods=["post"], url_path="agency-match")
    @transaction.atomic
    def agency_match(self, request):
        """Unified Agency Bill Match action: Original PO + Final Job Work PO + Vendor Invoice.
        
        Handles preview, save_draft, validate, submit_approval, approve, and reject.
        Enforces duplicate invoice prevention, 3-document reconciliation, and payable generation.
        """
        po_id = request.data.get("purchaseOrderId") or request.data.get("poId")
        if not po_id:
            raise ValidationFailed("purchaseOrderId is required.", field_errors={"purchaseOrderId": ["Required."]})

        po = PurchaseOrder.objects.filter(client_id=request.client_id, pk=po_id, deleted_at__isnull=True).first()
        if not po:
            raise ValidationFailed("Purchase Order not found.", field_errors={"purchaseOrderId": ["Not found."]})

        req_action = request.data.get("action", "preview")
        bill_id = request.data.get("billId") or request.data.get("vendorBillId")
        inv_number = (request.data.get("vendorBillNumber") or "").strip()
        inv_date = request.data.get("billDate") or timezone.now().date().isoformat()
        qty = round4(D(str(request.data.get("qty") or 0)))
        rate = round2(D(str(request.data.get("rate") or 0)))
        gst_pct = round2(D(str(request.data.get("gstPct") or 5)))
        remarks = request.data.get("remarks", "")
        reason = request.data.get("reason", remarks)

        # Find or initialize bill
        bill = None
        if bill_id:
            bill = VendorBill.objects.filter(client_id=request.client_id, pk=bill_id, deleted_at__isnull=True).first()

        if req_action in ["save_draft", "validate", "submit_approval", "approve"]:
            if not inv_number:
                raise ValidationFailed("Vendor Invoice Number is required.", field_errors={"vendorBillNumber": ["Required."]})

            # Enforce duplicate invoice submission prevention
            dup_qs = VendorBill.objects.filter(
                client_id=request.client_id,
                party_id=po.party_id,
                vendor_bill_number=inv_number,
                deleted_at__isnull=True,
            )
            if bill:
                dup_qs = dup_qs.exclude(pk=bill.pk)
            if dup_qs.exists():
                raise ValidationFailed(
                    f"A vendor bill with invoice number '{inv_number}' already exists for this agency.",
                    field_errors={"vendorBillNumber": ["Duplicate invoice number."]},
                )

            taxable = round2(qty * rate)
            gst_amount = round2(taxable * gst_pct / Decimal("100"))
            total = round2(taxable + gst_amount)

            item = po.line_items.filter(deleted_at__isnull=True).first()
            item_obj = item.item if item else None

            if bill is None:
                bill = VendorBill.objects.create(
                    client=po.client,
                    vendor_bill_number=inv_number,
                    bill_date=inv_date,
                    party=po.party,
                    party_name=po.party_name or (po.party.name if po.party else ""),
                    purchase_order=po,
                    gst_pct=gst_pct,
                    subtotal=taxable,
                    gst_amount=gst_amount,
                    total=total,
                    remarks=remarks,
                    status="Draft" if req_action == "save_draft" else "Pending",
                    created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
                )
                VendorBillLine.objects.create(
                    client=po.client,
                    vendor_bill=bill,
                    line_no=1,
                    item=item_obj,
                    item_name=item.description if item else (item_obj.name if item_obj else "Grey Fabric"),
                    sku=item_obj.sku if item_obj else None,
                    uom=item.uom if item else "Meter",
                    qty=qty,
                    rate=rate,
                    amount=taxable,
                    line_total=total,
                )
            else:
                bill.vendor_bill_number = inv_number
                bill.bill_date = inv_date
                bill.gst_pct = gst_pct
                bill.subtotal = taxable
                bill.gst_amount = gst_amount
                bill.total = total
                if remarks:
                    bill.remarks = remarks
                if req_action == "save_draft":
                    bill.status = "Draft"
                bill.save()

                line = bill.line_items.filter(deleted_at__isnull=True).first()
                if line:
                    line.qty = qty
                    line.rate = rate
                    line.amount = taxable
                    line.line_total = total
                    line.save()
                else:
                    VendorBillLine.objects.create(
                        client=po.client,
                        vendor_bill=bill,
                        line_no=1,
                        item=item_obj,
                        item_name=item.description if item else "Grey Fabric",
                        qty=qty,
                        rate=rate,
                        amount=taxable,
                        line_total=total,
                    )

        if bill:
            matching = services.compute_vendor_bill_matching(request.client_id, bill)
            bill.match_result = matching
            bill.match_status = "Matched" if matching.get("overallStatus") == "Match" else "Mismatch"
            bill.save(update_fields=["match_result", "match_status", "updated_at"])

            if req_action == "submit_approval":
                bill.status = "Pending Approval"
                history = list(bill.approval_history or [])
                history.append({
                    "at": timezone.now().isoformat(),
                    "by": getattr(request.user, "name", None) or str(request.user),
                    "action": "Submitted for Approval",
                    "remarks": remarks,
                })
                bill.approval_history = history
                bill.save(update_fields=["status", "approval_history", "updated_at"])

            elif req_action == "approve":
                request.data["createPayable"] = True
                appr_res = self.approve(request, pk=bill.pk)
                if bill.purchase_bill_id is None:
                    try:
                        self.send_for_matching(request, pk=bill.pk)
                        bill.refresh_from_db()
                    except Exception as e:
                        pass
                data = self.get_serializer(bill).data
                data["matching"] = services.compute_vendor_bill_matching(request.client_id, bill)
                return Response(data)

            elif req_action == "reject":
                return self.reject(request, pk=bill.pk)

            data = self.get_serializer(bill).data
            data["matching"] = matching
            return Response(data)

        # Live preview calculation without creating a DB record
        temp_bill = VendorBill(
            client=po.client,
            vendor_bill_number=inv_number or "PREVIEW",
            bill_date=inv_date,
            party=po.party,
            party_name=po.party_name,
            purchase_order=po,
            gst_pct=gst_pct,
            subtotal=round2(qty * rate),
            gst_amount=round2(round2(qty * rate) * gst_pct / Decimal("100")),
            total=round2(round2(qty * rate) + round2(round2(qty * rate) * gst_pct / Decimal("100"))),
            remarks=remarks,
            status="Draft",
        )
        temp_line = VendorBillLine(
            vendor_bill=temp_bill,
            line_no=1,
            qty=qty,
            rate=rate,
            amount=round2(qty * rate),
            item_name="Grey Fabric",
        )
        temp_bill._preloaded_lines = [temp_line]

        # Use helper
        preview_res = services.compute_vendor_bill_matching(request.client_id, temp_bill)
        return Response({"matching": preview_res})


class VendorLookupViewSet(ReadOnlyTenantViewSet):
    """``/purchase/vendors/{id}/…`` helpers (api.md §6.5)."""

    from apps.masters.models import Party

    queryset = Party.objects.filter(type__in=["Vendor", "Both"])
    required_permissions = ["view_purchase"]
    status_field = "status"

    def get_serializer_class(self):
        from apps.masters.serializers import PartySerializer

        return PartySerializer

    @action(detail=True, methods=["get"], url_path="advance-balance")
    def advance_balance(self, request, pk=None):
        self.get_object()
        return Response(services.vendor_advance_balance(request.client_id, pk))

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        from apps.accounting.services import party_ledger

        self.get_object()
        return Response(
            party_ledger(
                request.client_id,
                pk,
                date_from=request.query_params.get("date_from"),
                date_to=request.query_params.get("date_to"),
            )
        )


class SupplierDeliveryViewSet(TenantModelViewSet):
    queryset = SupplierDelivery.objects.select_related("purchase_order", "purchase_order__party").prefetch_related("purchase_order__line_items")
    serializer_class = SupplierDeliverySerializer
    required_permissions = ["view_purchase"]
    permission_map = {"read": ["view_purchase"], "write": ["create_purchase_order"]}
    status_field = "status"
    default_date_field = "dispatch_date"
    ordering = ["-dispatch_date", "-created_at"]
    filter_map = {
        "purchaseOrderId": "purchase_order_id",
        "poId": "purchase_order_id",
        "status": "status",
    }

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get("projectId") or self.request.query_params.get("project_id")
        if project_id:
            qs = qs.filter(purchase_order__project_id=project_id)
        supplier_id = self.request.query_params.get("supplierId") or self.request.query_params.get("vendorId")
        if supplier_id:
            qs = qs.filter(purchase_order__party_id=supplier_id)
        return qs

    def perform_create(self, serializer):
        from apps.core.numbering import allocate_number
        serializer.validated_data["delivery_number"] = allocate_number(
            self.request.user.client, "SD", serializer.validated_data.get("dispatch_date")
        )
        return super().perform_create(serializer)

