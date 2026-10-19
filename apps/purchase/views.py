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
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.printing import PdfNotAvailable, print_payload, send_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.inventory import services as stock
from apps.masters import uom
from apps.sales.views import SalesDocumentViewSet, _clone_document

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
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    @transaction.atomic
    def submit_for_approval(self, request, pk=None):
        """Draft -> Pending Approval."""
        return self._transition(request, from_statuses=("Draft", "Issued"), to_status="Pending Approval")

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        """Pending Approval -> Approved. Only an approved PO can be received
        (grey stock) or start production (Vendor A)."""
        return self._transition(request, from_statuses=("Pending Approval",), to_status="Approved")

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

    @action(detail=False, methods=["get"], url_path="next-jwpo-number")
    def next_jwpo_number(self, request):
        from apps.core.numbering import financial_year_label
        client = getattr(request.user, "client", None)
        year_str = financial_year_label(client, reset_policy="fy") if client else str(timezone.localdate().year)

        last_po = PurchaseOrder.objects.filter(
            client_id=request.client_id,
            po_number__startswith="JWPO",
        ).order_by("-po_number").first()

        seq = 1
        if last_po and last_po.po_number:
            parts = last_po.po_number.split("-")
            if len(parts) >= 3 and parts[-1].isdigit():
                seq = int(parts[-1]) + 1

        next_no = f"JWPO-{year_str}-{seq:04d}"
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

        bill = self.get_object()
        amount = round2(request.data.get("amount"))
        if amount <= ZERO:
            raise ValidationFailed(
                "Enter an amount to apply.",
                field_errors={"amount": ["Must be greater than zero."]},
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


class PurchaseReturnViewSet(SalesDocumentViewSet):
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

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        bill = self.get_object()
        if bill.status != "Pending":
            raise Conflict(
                f"Only a pending bill can be approved. This bill is {bill.status}.",
                code=Codes.DRAFT_ONLY,
            )
        remarks = request.data.get("remarks")
        # Approving releases the payable even though PO / GRN / bill disagree,
        # so the variance and the reason are put on record. Nothing on the PO,
        # GRN or bill is changed to force a match.
        matching = services.compute_vendor_bill_matching(request.client_id, bill)
        is_mismatch = matching.get("overallStatus") == "Mismatch"
        if is_mismatch and not (remarks or "").strip():
            raise ValidationFailed(
                "This bill does not match the PO / GRN. Enter approval remarks explaining the variance.",
                field_errors={"remarks": ["Required to approve a mismatched bill."]},
                payload={"matching": matching},
            )
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
        bill.save(
            update_fields=[
                "status", "approval_remarks", "approved_by", "approved_at",
                "approval_history", "match_result", "updated_at",
            ]
        )
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

