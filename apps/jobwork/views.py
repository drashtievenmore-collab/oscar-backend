"""Jobwork endpoints — process plans and job work orders.

Child rows are managed one at a time through per-row endpoints; a header
PATCH never creates, edits or deletes them (see services.py).
"""
from decimal import Decimal
import uuid
from datetime import date

from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.audit import snapshot
from apps.core.exceptions import Conflict, NotFound
from apps.core.money import D, ZERO, round4
from apps.core.numbering import allocate_number
from apps.core.permissions import HasModulePermission, require_permission
from apps.core.viewsets import TenantModelViewSet

from .models import JobWorkOrder, PlanCharge, ProcessPlan
from .serializers import (
    JobWorkOrderSerializer,
    JWOLossEntrySerializer,
    PlanChargeSerializer,
    ProcessPlanSerializer,
)
from .services import (
    ORDER_NESTED_KEYS,
    ORDER_ROW_KIND_PATTERN,
    ORDER_ROW_KINDS,
    PLAN_NESTED_KEYS,
    create_order_rows,
    delete_order_row,
    delete_plan_charge,
    line_amount,
    live,
    reject_nested_on_update,
    save_order_row,
    save_plan_charge,
)


def _parse_date(value):
    """A YYYY-MM-DD query parameter as a date, or None when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        from apps.core.exceptions import ValidationFailed

        raise ValidationFailed("Dates must be YYYY-MM-DD.", field_errors={"date": [f"Not a date: {value}"]})


class ProcessPlanViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
        # Approval points (approve_jobwork): approving a draft plan and
        # marking a plan Completed.
        "approve": ["approve_jobwork"],
        "complete": ["approve_jobwork"],
    }
    queryset = ProcessPlan.objects.prefetch_related("charges")
    serializer_class = ProcessPlanSerializer
    audit_entity_type = "ProcessPlan"
    audit_label_field = "plan_no"
    status_field = "status"
    default_date_field = "date"
    search_fields = ["plan_no", "process", "vendor_name", "fabric_item"]
    ordering = ["-date", "-created_at"]
    idempotent_create = True

    def get_queryset(self):
        qs = super().get_queryset()
        proj = self.request.query_params.get("project") or self.request.query_params.get("projectId")
        if proj:
            qs = qs.filter(project_id=proj)
        return qs

    #: Plan statuses only an approver may move a plan into or out of.
    APPROVAL_STATUSES = ("Completed",)

    @transaction.atomic
    def perform_create(self, serializer):
        charges = serializer.validated_data.pop("charges", [])
        if serializer.validated_data.get("status") in self.APPROVAL_STATUSES:
            require_permission(
                self.request.user, "approve_jobwork",
                "Only an approver can create a Completed process plan.",
            )
        serializer.validated_data["plan_no"] = allocate_number(
            self.request.user.client, "PP",
            serializer.validated_data.get("date"),
        )
        plan = super().perform_create(serializer)
        for charge in charges:
            charge = dict(charge)
            charge.pop("id", None)
            charge["amount"] = line_amount(charge)
            PlanCharge.objects.create(
                client_id=self.get_client_id(), process_plan=plan, **charge
            )
        return plan

    def update(self, request, *args, **kwargs):
        # A header PATCH never touches charge rows (use /charges/ instead).
        reject_nested_on_update(request.data, PLAN_NESTED_KEYS)
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def perform_update(self, serializer):
        plan = serializer.instance
        new_status = serializer.validated_data.get("status", plan.status)
        if new_status != plan.status and (
            new_status in self.APPROVAL_STATUSES or plan.status in self.APPROVAL_STATUSES
        ):
            require_permission(
                self.request.user, "approve_jobwork",
                "Only an approver can complete or reopen a process plan.",
            )
        return super().perform_update(serializer)

    def _status_change(self, request, to_status, allowed_from, verb):
        plan = self.get_object()
        if plan.status not in allowed_from:
            raise Conflict(
                f"Process plan {plan.plan_no} is {plan.status}; it cannot be {verb}.",
                code="JOBWORK_PLAN_STATE",
            )
        before = snapshot(plan)
        old = plan.status
        plan.status = to_status
        plan.approver = getattr(request.user, "name", None) or getattr(request.user, "email", "")
        plan.updated_by = request.user
        plan.save(update_fields=["status", "approver", "updated_by", "updated_at"])
        self.write_audit(
            verb, plan, before=before, after=snapshot(plan),
            from_value=old, to_value=to_status,
            comments=(request.data or {}).get("remarks") if isinstance(request.data, dict) else None,
        )
        self._concurrency_instance = plan
        return Response(self.get_serializer(self.get_queryset().get(pk=plan.pk)).data)

    @action(detail=True, methods=["post"], url_path="approve")
    @transaction.atomic
    def approve(self, request, pk=None):
        """Draft → Active, stamped with the approver."""
        return self._status_change(request, "Active", ("Draft",), "approved")

    @action(detail=True, methods=["post"], url_path="complete")
    @transaction.atomic
    def complete(self, request, pk=None):
        """Active → Completed (approval point)."""
        return self._status_change(request, "Completed", ("Active",), "completed")

    @action(detail=True, methods=["post"], url_path="create-jwo")
    @transaction.atomic
    def create_jwo(self, request, pk=None):
        """Create a Stage 12 Job Work Order from this Stage 11 Process Plan.
        Idempotent: if a JWO already exists for this plan, returns the existing JWO.
        """
        plan = self.get_object()
        require_permission(request.user, "manage_jobwork", "Permission required to create Job Work Order.")

        existing = JobWorkOrder.objects.filter(
            client_id=plan.client_id, process_plan=plan, deleted_at__isnull=True
        ).first()
        if existing:
            ser = JobWorkOrderSerializer(existing, context=self.get_serializer_context())
            return Response(ser.data, status=status.HTTP_200_OK)

        from apps.masters.models import Party
        vendor_obj = Party.objects.filter(
            client_id=plan.client_id, name__iexact=plan.vendor_name, deleted_at__isnull=True
        ).first() if plan.vendor_name else None

        today = timezone.localdate()
        jwo_no = allocate_number(plan.client, "JWO", today)

        first_charge = plan.charges.filter(deleted_at__isnull=True).first()
        plan_rate = first_charge.rate if first_charge else ZERO

        order = JobWorkOrder.objects.create(
            client_id=plan.client_id,
            process_plan=plan,
            project=plan.project,
            project_destination=plan.project_destination or "Customer Project",
            jwo_no=jwo_no,
            vendor=vendor_obj,
            vendor_name=plan.vendor_name or (vendor_obj.name if vendor_obj else ""),
            process=plan.process or "",
            order_date=today,
            expected_completion=plan.expected_completion_date,
            expected_loss_pct=plan.expected_loss or ZERO,
            rate=plan_rate,
            status="Draft",
            remarks=f"Created from Process Plan {plan.plan_no}",
            created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
        )

        from .models import JWOMaterial
        takas = plan.selected_takas if isinstance(plan.selected_takas, list) else []
        if takas:
            groups = {}
            for t in takas:
                key = (
                    t.get("fabricItem") or t.get("fabricName") or plan.fabric_item or "",
                    t.get("fabricQuality") or plan.fabric_quality or "",
                    t.get("shade") or plan.shade or "",
                    t.get("lotNumber") or plan.grey_lot_no or "",
                )
                m = D(str(t.get("meters") or t.get("usable") or t.get("totalStock") or t.get("selectedQty") or 0))
                groups[key] = groups.get(key, ZERO) + m

            for (f_item, f_qual, f_shade, f_lot), qty in groups.items():
                JWOMaterial.objects.create(
                    client_id=plan.client_id,
                    job_work_order=order,
                    fabric_item=f_item,
                    fabric_quality=f_qual,
                    shade=f_shade,
                    lot_no=f_lot,
                    qty=qty,
                    rate=plan_rate,
                    amount=round4(qty * plan_rate),
                )
        else:
            qty = D(str(plan.expected_qty or 0))
            JWOMaterial.objects.create(
                client_id=plan.client_id,
                job_work_order=order,
                fabric_item=plan.fabric_item or "",
                fabric_quality=plan.fabric_quality or "",
                shade=plan.shade or "",
                lot_no=plan.grey_lot_no or "",
                qty=qty,
                rate=plan_rate,
                amount=round4(qty * plan_rate),
            )

        from .services import recompute_order_totals
        recompute_order_totals(order)

        if plan.status == "Draft":
            plan.status = "Active"
            plan.save(update_fields=["status", "updated_at"])

        if plan.project:
            from apps.pms.pipeline_services import sync_project_pipeline_stages
            sync_project_pipeline_stages(plan.project)

        ser = JobWorkOrderSerializer(order, context=self.get_serializer_context())
        return Response(ser.data, status=status.HTTP_201_CREATED)

    # -- charge rows: /process-plans/{id}/charges/[{rowId}/] -----------------
    @action(detail=True, methods=["get", "post"], url_path="charges", url_name="charges")
    @transaction.atomic
    def charges(self, request, pk=None):
        plan = self.get_object()
        if request.method == "GET":
            return Response({"results": PlanChargeSerializer(live(plan.charges), many=True).data})
        serializer = PlanChargeSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        row = save_plan_charge(request, plan, serializer)
        return Response(PlanChargeSerializer(row).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["get", "patch", "delete"],
        url_path=r"charges/(?P<row_id>[^/.]+)", url_name="charge-detail",
    )
    @transaction.atomic
    def charge_detail(self, request, pk=None, row_id=None):
        plan = self.get_object()
        row = plan.charges.filter(pk=_uuid_or_404(row_id), deleted_at__isnull=True).first()
        if row is None:
            raise NotFound()
        if request.method == "GET":
            return Response(PlanChargeSerializer(row).data)
        if request.method == "DELETE":
            delete_plan_charge(request, plan, row)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = PlanChargeSerializer(
            row, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        row = save_plan_charge(request, plan, serializer, instance=row)
        return Response(PlanChargeSerializer(row).data)


class JobWorkOrderViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
        # Approval point: closing an order freezes its rows.
        "close": ["approve_jobwork"],
        "approve_loss": ["approve_jobwork"],
    }
    queryset = (
        JobWorkOrder.objects.select_related("process_plan")
        .prefetch_related(
            "materials", "outwards", "inwards", "charges", "reprocesses", "documents__file"
        )
    )
    serializer_class = JobWorkOrderSerializer
    audit_entity_type = "JobWorkOrder"
    audit_label_field = "jwo_no"
    status_field = "status"
    default_date_field = "order_date"
    search_fields = ["jwo_no", "process", "vendor_name"]
    ordering = ["-order_date", "-created_at"]
    idempotent_create = True

    def get_queryset(self):
        qs = super().get_queryset()
        proj = self.request.query_params.get("project") or self.request.query_params.get("projectId")
        if proj:
            qs = qs.filter(project_id=proj)
        return qs

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            totalOrdered=Coalesce(Sum("planned_qty"), Value(ZERO), output_field=DecimalField(max_digits=18, decimal_places=4)),
            totalAmount=Coalesce(Sum("total_amount"), Value(ZERO), output_field=DecimalField(max_digits=18, decimal_places=2)),
            count=Count("id"),
        )

    @action(detail=False, methods=["get"], url_path="pending-register")
    def pending_register(self, request):
        """Derived Pending Register — no separate table.

        One row per JWO with material sent out, computed live from the
        JWO's outwards/inwards (same records the detail tabs read/write):

            pendingQty = Σ outwards.qty − Σ inwards.qty

        Status: pendingQty = 0 → Completed; else expected_completion
        passed → Overdue; else Pending.
        """
        today = date.today().isoformat()
        queryset = (
            self.filter_queryset(self.get_queryset())
            .prefetch_related("materials", "outwards", "inwards")
            .order_by("jwo_no")
        )
        rows = []
        for order in queryset:
            total_outward = sum((o.qty or ZERO for o in live(order.outwards)), ZERO)
            total_inward = sum((i.qty or ZERO for i in live(order.inwards)), ZERO)
            if total_outward <= ZERO:
                continue
            pending = total_outward - total_inward
            if pending < ZERO:
                pending = ZERO
            expected = order.expected_completion.isoformat() if order.expected_completion else None
            if pending <= ZERO:
                register_status = "Completed"
            elif expected and today > expected:
                register_status = "Overdue"
            else:
                register_status = "Pending"
            outward_dates = sorted(
                o.date.isoformat() for o in live(order.outwards) if o.date
            )
            pending_since = outward_dates[0] if outward_dates else (
                order.order_date.isoformat() if order.order_date else None
            )
            days_pending = 0
            if register_status != "Completed" and pending_since:
                days_pending = max(
                    0, (date.today() - date.fromisoformat(pending_since[:10])).days
                )
            mat = next(iter(live(order.materials)), None)
            rows.append({
                "jwoId": str(order.id),
                "jwoNo": order.jwo_no,
                "vendor": order.vendor_name or "",
                "process": order.process or "",
                "fabricItem": getattr(mat, "fabric_item", "") or "",
                "fabricQuality": getattr(mat, "fabric_quality", "") or "",
                "shade": getattr(mat, "shade", "") or "",
                "totalOutward": float(total_outward),
                "totalInward": float(total_inward),
                "pendingQty": float(pending),
                "expectedReturnDate": expected,
                "daysPending": days_pending,
                "status": register_status,
            })
        status_filter = request.query_params.get("status")
        if status_filter and status_filter != "All":
            rows = [r for r in rows if r["status"] == status_filter]
        return Response({"results": rows, "count": len(rows)})

    @action(detail=False, methods=["get"], url_path="vendor-reconciliation")
    def vendor_reconciliation(self, request):
        """Derived Vendor Reconciliation — no separate table.

        One row per JWO with material sent out, computed live from the
        JWO's outwards/inwards (same records the detail tabs read/write):

            totalOutward    = Σ outwards.qty
            totalInward     = Σ inwards.qty
            acceptedQty     = Σ inwards.accepted
            rejectedQty     = Σ inwards.rejected
            approvedLossQty = Σ approved loss entries
            pendingQty      = totalOutward − totalInward − approvedLossQty (PDF §07)
            difference      = pendingQty (the unreconciled balance)

        Status: pendingQty = 0 → Reconciled; else expected_completion
        passed → Mismatch; else Pending.
        """
        today = date.today().isoformat()
        queryset = (
            self.filter_queryset(self.get_queryset())
            .prefetch_related("materials", "outwards", "inwards", "loss_entries")
            .order_by("jwo_no")
        )
        rows = []
        for order in queryset:
            total_outward = sum((o.qty or ZERO for o in live(order.outwards)), ZERO)
            total_inward = sum((i.qty or ZERO for i in live(order.inwards)), ZERO)
            if total_outward <= ZERO:
                continue
            accepted = sum((i.accepted or ZERO for i in live(order.inwards)), ZERO)
            rejected = sum((i.rejected or ZERO for i in live(order.inwards)), ZERO)
            approved_loss = sum(
                (l.qty or ZERO for l in order.loss_entries.all() if l.deleted_at is None and l.status == "Approved"),
                ZERO,
            )
            pending = total_outward - total_inward - approved_loss
            if pending < ZERO:
                pending = ZERO
            expected = order.expected_completion.isoformat() if order.expected_completion else None
            if pending <= ZERO:
                recon_status = "Reconciled"
            elif expected and today > expected:
                recon_status = "Mismatch"
            else:
                recon_status = "Pending"
            mat = next(iter(live(order.materials)), None)
            rows.append({
                "jwoId": str(order.id),
                "jwoNo": order.jwo_no,
                "orderDate": order.order_date.isoformat() if order.order_date else None,
                "vendor": order.vendor_name or "",
                "process": order.process or "",
                "fabricItem": getattr(mat, "fabric_item", "") or "",
                "fabricQuality": getattr(mat, "fabric_quality", "") or "",
                "shade": getattr(mat, "shade", "") or "",
                "totalOutward": float(total_outward),
                "totalInward": float(total_inward),
                "acceptedQty": float(accepted),
                "rejectedQty": float(rejected),
                "approvedLossQty": float(approved_loss),
                "pendingQty": float(pending),
                "difference": float(pending),
                "status": recon_status,
            })
        status_filter = request.query_params.get("status")
        if status_filter and status_filter != "All":
            rows = [r for r in rows if r["status"] == status_filter]
        return Response({"results": rows, "count": len(rows)})

    @transaction.atomic
    def perform_create(self, serializer):
        rows_by_kind = {
            kind: serializer.validated_data.pop(kind, [])
            for kind in ("materials", "outwards", "inwards", "charges", "reprocesses")
        }
        if serializer.validated_data.get("status") in JobWorkOrder.APPROVAL_STATUSES:
            require_permission(
                self.request.user, "approve_jobwork",
                "Only an approver can create a Completed / Closed job work order.",
            )
        serializer.validated_data["jwo_no"] = allocate_number(
            self.request.user.client, "JWO",
            serializer.validated_data.get("order_date"),
        )
        order = super().perform_create(serializer)
        create_order_rows(order, rows_by_kind, self.request.user.client)
        return order

    def update(self, request, *args, **kwargs):
        # A header PATCH never touches child rows -- they have their own
        # endpoints (/orders/{id}/<kind>/[{rowId}/]).
        reject_nested_on_update(request.data, ORDER_NESTED_KEYS)
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def perform_update(self, serializer):
        order = serializer.instance
        new_status = serializer.validated_data.get("status", order.status)
        status_changes = new_status != order.status
        if status_changes and (
            new_status in JobWorkOrder.APPROVAL_STATUSES
            or order.status in JobWorkOrder.APPROVAL_STATUSES
        ):
            require_permission(
                self.request.user, "approve_jobwork",
                "Only an approver can complete, close or reopen a job work order.",
            )
        if order.status in JobWorkOrder.LOCKED_STATUSES:
            other = [k for k in serializer.validated_data if k not in ("status", "remarks")]
            if other:
                raise Conflict(
                    f"Job work order {order.jwo_no} is {order.status}; reopen it before editing.",
                    code="JOBWORK_ORDER_CLOSED",
                )
        return super().perform_update(serializer)

    def check_delete_allowed(self, instance):
        if instance.status in JobWorkOrder.LOCKED_STATUSES:
            raise Conflict(
                f"Job work order {instance.jwo_no} is {instance.status} and cannot be deleted.",
                code="JOBWORK_ORDER_CLOSED",
            )
        if live(instance.outwards) or live(instance.inwards):
            raise Conflict(
                f"Job work order {instance.jwo_no} already has outward / inward movements "
                "and cannot be deleted.",
                code="JOBWORK_ROW_IN_USE",
            )

    @action(detail=False, methods=["get"], url_path="lookup-rate")
    def lookup_rate(self, request):
        processor_id = request.query_params.get("processorId") or request.query_params.get("vendorId")
        process_id = request.query_params.get("processId")
        quality_id = request.query_params.get("fabricQualityId")

        from apps.masters.models import JobWorkRateMaster
        today = date.today()
        qs = JobWorkRateMaster.objects.filter(
            client_id=request.client_id,
            is_active=True,
            effective_from__lte=today,
        ).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=today)
        )
        if processor_id:
            qs = qs.filter(processor_id=processor_id)
        if process_id:
            qs = qs.filter(process_id=process_id)
        if quality_id:
            qs = qs.filter(fabric_quality_id=quality_id)

        rate_obj = qs.order_by("-effective_from").first()
        if rate_obj:
            return Response({
                "rateMasterId": str(rate_obj.id),
                "rate": float(rate_obj.rate),
                "rateUom": rate_obj.rate_uom,
            })
        return Response({"rateMasterId": None, "rate": None, "rateUom": "Meter"})

    @action(detail=False, methods=["get"], url_path="processor-ledger")
    def processor_ledger(self, request):
        processor_id = request.query_params.get("processorId") or request.query_params.get("vendorId")
        from apps.masters.models import Party

        """Material ledger per processor (PDF §07):

            Opening + Sent − Received − Approved Loss = Closing

        ``fromDate`` / ``toDate`` (YYYY-MM-DD) bound the period; movements
        before ``fromDate`` make up the opening balance. Entries are sorted by
        date before the running balance is taken.
        """
        client_id = request.client_id
        date_from = _parse_date(request.query_params.get("fromDate") or request.query_params.get("date_from"))
        date_to = _parse_date(request.query_params.get("toDate") or request.query_params.get("date_to"))

        parties = Party.objects.filter(client_id=client_id, deleted_at__isnull=True)
        if processor_id:
            parties = parties.filter(id=processor_id)
        else:
            parties = parties.filter(
                Q(type__in=["Vendor", "Both", "Processor"]) | Q(is_processor=True)
            )

        # Same day: material goes out before it can come back or be written off.
        doc_order = {"Outward": 0, "Inward": 1, "Loss": 2}
        results = []
        for p in parties:
            orders = JobWorkOrder.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
            ).filter(
                Q(vendor=p) | Q(vendor_name=p.name)
            ).prefetch_related("outwards", "inwards", "loss_entries")

            raw = []
            for o in orders:
                for ow in live(o.outwards):
                    raw.append((ow.date, "Outward", o.jwo_no, ow.no, ow.challan_no, D(ow.qty or 0)))
                for iw in live(o.inwards):
                    raw.append((iw.date, "Inward", o.jwo_no, iw.no, iw.challan_no, D(iw.qty or 0)))
                for loss in o.loss_entries.filter(deleted_at__isnull=True, status="Approved"):
                    on = loss.approved_at.date() if loss.approved_at else None
                    raw.append((on, "Loss", o.jwo_no, f"LOSS-{str(loss.id)[:8]}", None, D(loss.qty or 0)))
            raw.sort(key=lambda r: (r[0] or date.min, doc_order[r[1]], r[2] or "", r[3] or ""))

            def signed(row):
                return row[5] if row[1] == "Outward" else -row[5]

            opening = sum((signed(r) for r in raw if date_from and r[0] and r[0] < date_from), ZERO)
            running = opening
            entries = []
            sent = received = lost = ZERO
            for row in raw:
                on, doc_type, jwo_no, doc_no, challan, qty = row
                if date_from and on and on < date_from:
                    continue
                if date_to and on and on > date_to:
                    continue
                running += signed(row)
                sent += qty if doc_type == "Outward" else ZERO
                received += qty if doc_type == "Inward" else ZERO
                lost += qty if doc_type == "Loss" else ZERO
                entries.append({
                    "date": on.isoformat() if on else None,
                    "jwoNo": jwo_no,
                    "docType": doc_type,
                    "docNo": doc_no,
                    "challanNo": challan,
                    "debitQty": float(qty) if doc_type == "Outward" else 0.0,
                    "creditQty": float(qty) if doc_type == "Inward" else 0.0,
                    "lossQty": float(qty) if doc_type == "Loss" else 0.0,
                    "balanceQty": float(running),
                })

            if entries or processor_id or opening:
                results.append({
                    "processorId": str(p.id),
                    "processorName": p.name,
                    "processorCode": p.code,
                    "openingBalance": float(opening),
                    "totalSent": float(sent),
                    "totalReceived": float(received),
                    "totalLoss": float(lost),
                    "closingBalance": float(opening + sent - received - lost),
                    "entries": entries,
                })

        return Response({"results": results, "count": len(results)})

    @action(detail=True, methods=["post"], url_path="close")
    @transaction.atomic
    def close(self, request, pk=None):
        """Close the order (approval point, approve_jobwork). Rows freeze."""
        order = self.get_object()
        if order.status == "Closed":
            raise Conflict(f"Job work order {order.jwo_no} is already Closed.", code="JOBWORK_ORDER_CLOSED")

        # Reconciliation Gate (PDF §07): Sent = Received + Approved Loss
        sent_qty = sum((o.qty or ZERO for o in live(order.outwards)), ZERO)
        received_qty = sum((i.qty or ZERO for i in live(order.inwards)), ZERO)
        approved_loss = sum(
            (l.qty or ZERO for l in order.loss_entries.filter(deleted_at__isnull=True, status="Approved")),
            ZERO,
        )

        has_phase2_spec = (
            (order.process_master_id is not None)
            or (order.vendor_id is not None)
            or bool(isinstance(request.data, dict) and request.data.get("reconcile"))
        )
        if has_phase2_spec and sent_qty > ZERO:
            tol_pct = order.expected_loss_pct
            if (not tol_pct or tol_pct <= ZERO) and getattr(order, "process_master", None):
                tol_pct = order.process_master.tolerance_pct or order.process_master.expected_loss_pct
            tol_pct = D(tol_pct or 0)

            allowed_variance = round4(sent_qty * tol_pct / Decimal("100"))
            net_diff = abs(sent_qty - (received_qty + approved_loss))

            force = isinstance(request.data, dict) and (request.data.get("force") or request.data.get("override"))
            if net_diff > allowed_variance and not force:
                raise Conflict(
                    f"Cannot close job work order {order.jwo_no}: Reconciliation gate failed. "
                    f"Sent ({sent_qty}m) does not match Received ({received_qty}m) + Approved Loss ({approved_loss}m). "
                    f"Variance ({net_diff}m) exceeds allowed tolerance ({allowed_variance}m, {tol_pct}%). "
                    f"Record approved loss entry or pass force=true to override.",
                    code="JOBWORK_RECONCILIATION_FAILED",
                )

        before = snapshot(order)
        old = order.status
        order.status = "Closed"
        order.updated_by = request.user
        order.save(update_fields=["status", "updated_by", "updated_at"])
        remarks = request.data.get("remarks") if isinstance(request.data, dict) else None
        self.write_audit(
            "closed", order, before=before, after=snapshot(order),
            from_value=old, to_value="Closed", comments=remarks,
        )
        self._concurrency_instance = order
        return Response(self.get_serializer(self.get_queryset().get(pk=order.pk)).data)

    @action(detail=True, methods=["post"], url_path=r"losses/(?P<loss_id>[^/.]+)/approve")
    @transaction.atomic
    def approve_loss(self, request, pk=None, loss_id=None):
        order = self.get_object()
        loss = order.loss_entries.filter(id=_uuid_or_404(loss_id), deleted_at__isnull=True).first()
        if loss is None:
            raise NotFound("Loss entry not found.")
        loss.status = "Approved"
        loss.approved_by = request.user
        loss.approved_at = timezone.now()
        loss.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response(JWOLossEntrySerializer(loss).data)

    # -- child rows: /orders/{id}/<kind>/ and /orders/{id}/<kind>/{rowId}/ ---
    @action(
        detail=True, methods=["get", "post"],
        url_path=rf"(?P<kind>{ORDER_ROW_KIND_PATTERN})", url_name="rows",
    )
    @transaction.atomic
    def rows(self, request, pk=None, kind=None):
        order = self.get_object()
        _model, row_serializer, _series, _label = ORDER_ROW_KINDS[kind]
        context = self.get_serializer_context()
        if request.method == "GET":
            rows = live(getattr(order, kind))
            return Response({"results": row_serializer(rows, many=True, context=context).data})
        serializer = row_serializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        row = save_order_row(request, order, kind, serializer)
        return Response(row_serializer(row, context=context).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["get", "patch", "delete"],
        url_path=rf"(?P<kind>{ORDER_ROW_KIND_PATTERN})/(?P<row_id>[^/.]+)",
        url_name="row-detail",
    )
    @transaction.atomic
    def row_detail(self, request, pk=None, kind=None, row_id=None):
        order = self.get_object()
        _model, row_serializer, _series, _label = ORDER_ROW_KINDS[kind]
        context = self.get_serializer_context()
        row = getattr(order, kind).filter(
            pk=_uuid_or_404(row_id), deleted_at__isnull=True
        ).first()
        if row is None:
            raise NotFound()
        if request.method == "GET":
            return Response(row_serializer(row, context=context).data)
        if request.method == "DELETE":
            delete_order_row(request, order, kind, row)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = row_serializer(row, data=request.data, partial=True, context=context)
        serializer.is_valid(raise_exception=True)
        row = save_order_row(request, order, kind, serializer, instance=row)
        return Response(row_serializer(row, context=context).data)

    @action(detail=True, methods=["post"], url_path="validate-stock")
    def validate_stock(self, request, pk=None):
        """Pre-flight validation for Stage 12 outward dispatch."""
        order = self.get_object()
        from .services import validate_outward_stock
        takas_data = request.data.get("takas")
        qty = request.data.get("qty") or request.data.get("quantity")
        result = validate_outward_stock(order, takas_data=takas_data, qty=qty)
        return Response(result)

    @action(
        detail=True, methods=["post"],
        url_path=r"outwards/(?P<outward_id>[^/.]+)/confirm",
        url_name="confirm-outward",
    )
    @transaction.atomic
    def confirm_outward(self, request, pk=None, outward_id=None):
        """Confirm a draft outward challan and execute stock relocation to External WIP."""
        order = self.get_object()
        outward = order.outwards.filter(pk=_uuid_or_404(outward_id), deleted_at__isnull=True).first()
        if outward is None:
            raise NotFound()
        outward.status = "Sent"
        outward.save(update_fields=["status", "updated_at"])
        context = self.get_serializer_context()
        serializer = JWOOutwardSerializer(outward, data={}, partial=True, context=context)
        serializer.is_valid(raise_exception=True)
        save_order_row(request, order, "outwards", serializer, instance=outward)
        return Response(JWOOutwardSerializer(outward, context=context).data)

    @action(detail=True, methods=["post"], url_path="sign")
    @transaction.atomic
    def sign(self, request, pk=None):
        """Record or clear digital/drawn/uploaded e-signature on JWO or its documents."""
        order = self.get_object()
        doc_type = request.data.get("documentType", "jwo")
        doc_id = request.data.get("documentId")
        sig_type = request.data.get("signatoryType", "authorized")
        signature = request.data.get("signature")

        sigs = dict(order.signatures or {})
        if doc_type == "jwo":
            sigs.setdefault("jwo", {})
            if signature is None:
                sigs["jwo"].pop(sig_type, None)
            else:
                sigs["jwo"][sig_type] = signature
        elif doc_type in ("outward", "inward"):
            bucket = "outwards" if doc_type == "outward" else "inwards"
            sigs.setdefault(bucket, {})
            key = str(doc_id) if doc_id else "default"
            sigs[bucket].setdefault(key, {})
            if signature is None:
                sigs[bucket][key].pop(sig_type, None)
            else:
                sigs[bucket][key][sig_type] = signature

        order.signatures = sigs
        order.save(update_fields=["signatures", "updated_at"])

        desc = f"Signature updated for {doc_type} ({sig_type})" if signature else f"Signature removed for {doc_type} ({sig_type})"
        self.write_audit("sign", order, description=desc)

        return Response({
            "status": "success",
            "signatures": order.signatures,
            "order": self.get_serializer(order).data,
        })


def _uuid_or_404(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise NotFound()


from django.http import HttpResponse
from rest_framework.views import APIView
from .itc04_reports import get_itc04_report_data, generate_itc04_csv


class ITC04ReportView(APIView):
    """Statutory GST ITC-04 Job Work quarterly return data (PDF §20)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]

    def get(self, request):
        params = request.query_params
        fy = params.get("financialYear") or params.get("financial_year")
        quarter = params.get("quarter")
        start_date = params.get("startDate") or params.get("start_date")
        end_date = params.get("endDate") or params.get("end_date")
        vendor_id = params.get("vendorId") or params.get("vendor_id")

        data = get_itc04_report_data(
            client=request.user.client,
            financial_year=fy,
            quarter=quarter,
            start_date=start_date,
            end_date=end_date,
            vendor_id=vendor_id,
        )
        return Response(data)


class ITC04ExportView(APIView):
    """Download GST ITC-04 statement in standard CSV format."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]

    def get(self, request):
        params = request.query_params
        fy = params.get("financialYear") or params.get("financial_year")
        quarter = params.get("quarter")
        start_date = params.get("startDate") or params.get("start_date")
        end_date = params.get("endDate") or params.get("end_date")
        vendor_id = params.get("vendorId") or params.get("vendor_id")
        table = params.get("table", "both")

        data = get_itc04_report_data(
            client=request.user.client,
            financial_year=fy,
            quarter=quarter,
            start_date=start_date,
            end_date=end_date,
            vendor_id=vendor_id,
        )
        csv_content = generate_itc04_csv(data, table=table)
        period_str = f"{data['period'].get('quarter') or 'Q'}_{data['period'].get('financialYear') or 'FY'}"
        filename = f"ITC-04_{period_str}.csv"

        response = HttpResponse(csv_content, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

