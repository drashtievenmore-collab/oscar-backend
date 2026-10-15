"""Jobwork endpoints — process plans and job work orders."""
from datetime import date

from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.db.models import DecimalField, Value
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.money import D, ZERO, round2
from apps.core.numbering import allocate_number
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import TenantModelViewSet

from .models import (
    JobWorkOrder,
    JWOCharge,
    JWOInward,
    JWOMaterial,
    JWOOutward,
    JWOReprocess,
    PlanCharge,
    ProcessPlan,
    VendorProcessInstruction,
    VPIProgressEntry,
)
from .serializers import (
    JobWorkOrderSerializer,
    JWOChargeSerializer,
    JWOInwardSerializer,
    JWOMaterialSerializer,
    JWOOutwardSerializer,
    JWOReprocessSerializer,
    PlanChargeSerializer,
    ProcessPlanSerializer,
    VendorProcessInstructionSerializer,
    VPIProgressEntrySerializer,
)


class ProcessPlanViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
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

    @transaction.atomic
    def perform_create(self, serializer):
        from .models import PlanCharge

        charges = serializer.validated_data.pop("charges", [])
        serializer.validated_data["plan_no"] = allocate_number(
            self.request.user.client, "PP",
            serializer.validated_data.get("date"),
        )
        plan = super().perform_create(serializer)
        PlanCharge.objects.bulk_create([
            PlanCharge(client_id=self.get_client_id(), process_plan=plan, **c)
            for c in charges
        ])
        return plan

    @transaction.atomic
    def perform_update(self, serializer):
        from .models import PlanCharge

        charges = serializer.validated_data.pop("charges", None)
        plan = super().perform_update(serializer)
        if charges is not None:
            keep_ids = {str(r.get("id")) for r in charges if r.get("id")}
            plan.charges.exclude(pk__in=[i for i in keep_ids if i]).delete()
            for row in charges:
                rid = row.pop("id", None)
                if rid:
                    plan.charges.filter(pk=rid).update(**row)
                else:
                    PlanCharge.objects.create(
                        client_id=self.get_client_id(), process_plan=plan, **row
                    )
        return plan


class JobWorkOrderViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
    }
    queryset = (
        JobWorkOrder.objects.select_related("process_plan")
        .prefetch_related("materials", "outwards", "inwards", "charges", "reprocesses")
    )
    serializer_class = JobWorkOrderSerializer
    audit_entity_type = "JobWorkOrder"
    audit_label_field = "jwo_no"
    status_field = "status"
    default_date_field = "order_date"
    search_fields = ["jwo_no", "process", "vendor_name"]
    ordering = ["-order_date", "-created_at"]
    idempotent_create = True

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
            total_outward = sum((o.qty or ZERO for o in order.outwards.all()), ZERO)
            total_inward = sum((i.qty or ZERO for i in order.inwards.all()), ZERO)
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
                o.date.isoformat() for o in order.outwards.all() if o.date
            )
            pending_since = outward_dates[0] if outward_dates else (
                order.order_date.isoformat() if order.order_date else None
            )
            days_pending = 0
            if register_status != "Completed" and pending_since:
                days_pending = max(
                    0, (date.today() - date.fromisoformat(pending_since[:10])).days
                )
            mat = next(iter(order.materials.all()), None)
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

            totalOutward = Σ outwards.qty
            totalInward  = Σ inwards.qty
            acceptedQty  = Σ inwards.accepted
            rejectedQty  = Σ inwards.rejected
            pendingQty   = totalOutward − totalInward
            difference   = pendingQty (the unreconciled balance)

        Status: pendingQty = 0 → Reconciled; else expected_completion
        passed → Mismatch; else Pending.
        """
        today = date.today().isoformat()
        queryset = (
            self.filter_queryset(self.get_queryset())
            .prefetch_related("materials", "outwards", "inwards")
            .order_by("jwo_no")
        )
        rows = []
        for order in queryset:
            total_outward = sum((o.qty or ZERO for o in order.outwards.all()), ZERO)
            total_inward = sum((i.qty or ZERO for i in order.inwards.all()), ZERO)
            if total_outward <= ZERO:
                continue
            accepted = sum((i.accepted or ZERO for i in order.inwards.all()), ZERO)
            rejected = sum((i.rejected or ZERO for i in order.inwards.all()), ZERO)
            pending = total_outward - total_inward
            if pending < ZERO:
                pending = ZERO
            expected = order.expected_completion.isoformat() if order.expected_completion else None
            if pending <= ZERO:
                recon_status = "Reconciled"
            elif expected and today > expected:
                recon_status = "Mismatch"
            else:
                recon_status = "Pending"
            mat = next(iter(order.materials.all()), None)
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
        from .models import JWOCharge, JWOInward, JWOMaterial, JWOOutward, JWOReprocess

        materials = serializer.validated_data.pop("materials", [])
        outwards = serializer.validated_data.pop("outwards", [])
        inwards = serializer.validated_data.pop("inwards", [])
        charges = serializer.validated_data.pop("charges", [])
        reprocesses = serializer.validated_data.pop("reprocesses", [])
        serializer.validated_data["jwo_no"] = allocate_number(
            self.request.user.client, "JWO",
            serializer.validated_data.get("order_date"),
        )
        # Recompute header totals from materials so the server owns the numbers.
        if materials:
            planned = sum((D(m.get("qty") or 0) for m in materials), ZERO)
            total = sum(
                (D(m.get("amount") or 0) or D(m.get("qty") or 0) * D(m.get("rate") or 0)
                 for m in materials), ZERO,
            )
            serializer.validated_data["planned_qty"] = planned
            serializer.validated_data["total_amount"] = round2(total)
        order = super().perform_create(serializer)
        client_id = self.get_client_id()
        JWOMaterial.objects.bulk_create([
            JWOMaterial(client_id=client_id, job_work_order=order, **m) for m in materials
        ])
        JWOOutward.objects.bulk_create([
            JWOOutward(client_id=client_id, job_work_order=order, **o) for o in outwards
        ])
        JWOInward.objects.bulk_create([
            JWOInward(client_id=client_id, job_work_order=order, **i) for i in inwards
        ])
        JWOCharge.objects.bulk_create([
            JWOCharge(client_id=client_id, job_work_order=order, **c) for c in charges
        ])
        JWOReprocess.objects.bulk_create([
            JWOReprocess(client_id=client_id, job_work_order=order, **r) for r in reprocesses
        ])
        return order

    @transaction.atomic
    def perform_update(self, serializer):
        from .models import JWOCharge, JWOInward, JWOMaterial, JWOOutward, JWOReprocess

        materials = serializer.validated_data.pop("materials", None)
        outwards = serializer.validated_data.pop("outwards", None)
        inwards = serializer.validated_data.pop("inwards", None)
        charges = serializer.validated_data.pop("charges", None)
        reprocesses = serializer.validated_data.pop("reprocesses", None)
        order = super().perform_update(serializer)
        client_id = self.get_client_id()

        def update_or_add(manager, model, rows):
            if rows is None:
                return
            for row in rows:
                rid = row.pop("id", None)
                if rid and manager.filter(pk=rid).exists():
                    manager.filter(pk=rid).update(**row)
                elif row.get("no") and manager.filter(no=row["no"]).exists():
                    manager.filter(no=row["no"]).update(**row)
                else:
                    model.objects.create(
                        client_id=client_id, job_work_order=order, **row
                    )

        update_or_add(order.materials, JWOMaterial, materials)
        update_or_add(order.outwards, JWOOutward, outwards)
        update_or_add(order.inwards, JWOInward, inwards)
        update_or_add(order.charges, JWOCharge, charges)
        update_or_add(order.reprocesses, JWOReprocess, reprocesses)
        if materials is not None:
            mats = list(order.materials.all())
            order.planned_qty = sum((m.qty or ZERO for m in mats), ZERO)
            order.total_amount = round2(
                sum((m.amount or ZERO for m in mats), ZERO)
            )
            order.save(update_fields=["planned_qty", "total_amount", "updated_at"])
        return order

    @action(detail=True, methods=["post"], url_path="materials")
    @transaction.atomic
    def add_material(self, request, pk=None):
        order = self.get_object()
        serializer = JWOMaterialSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from .models import JWOMaterial
        mat = JWOMaterial.objects.create(
            client_id=self.get_client_id(),
            job_work_order=order,
            **serializer.validated_data,
        )
        mats = list(order.materials.all())
        order.planned_qty = sum((m.qty or ZERO for m in mats), ZERO)
        order.total_amount = round2(sum((m.amount or ZERO for m in mats), ZERO))
        order.save(update_fields=["planned_qty", "total_amount", "updated_at"])
        return Response(JWOMaterialSerializer(mat).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="outwards")
    @transaction.atomic
    def add_outward(self, request, pk=None):
        order = self.get_object()
        serializer = JWOOutwardSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from .models import JWOOutward
        outward = JWOOutward.objects.create(
            client_id=self.get_client_id(),
            job_work_order=order,
            **serializer.validated_data,
        )
        return Response(JWOOutwardSerializer(outward).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="inwards")
    @transaction.atomic
    def add_inward(self, request, pk=None):
        order = self.get_object()
        serializer = JWOInwardSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from .models import JWOInward
        inward = JWOInward.objects.create(
            client_id=self.get_client_id(),
            job_work_order=order,
            **serializer.validated_data,
        )
        return Response(JWOInwardSerializer(inward).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="charges")
    @transaction.atomic
    def add_charge(self, request, pk=None):
        order = self.get_object()
        serializer = JWOChargeSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from .models import JWOCharge
        charge = JWOCharge.objects.create(
            client_id=self.get_client_id(),
            job_work_order=order,
            **serializer.validated_data,
        )
        return Response(JWOChargeSerializer(charge).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reprocesses")
    @transaction.atomic
    def add_reprocess(self, request, pk=None):
        order = self.get_object()
        serializer = JWOReprocessSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        from .models import JWOReprocess
        reprocess = JWOReprocess.objects.create(
            client_id=self.get_client_id(),
            job_work_order=order,
            **serializer.validated_data,
        )
        return Response(JWOReprocessSerializer(reprocess).data, status=status.HTTP_201_CREATED)


class VendorProcessInstructionViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
    }
    queryset = VendorProcessInstruction.objects.prefetch_related("entries")
    serializer_class = VendorProcessInstructionSerializer
    audit_entity_type = "VendorProcessInstruction"
    audit_label_field = "pi_number"
    status_field = "status"
    default_date_field = "date"
    search_fields = ["pi_number", "po_number", "vendor", "fabric", "process_type"]
    ordering = ["-date", "-created_at"]
    idempotent_create = True

    def perform_create(self, serializer):
        serializer.validated_data["pi_number"] = allocate_number(
            self.request.user.client, "VPI",
            serializer.validated_data.get("date"),
        )
        return super().perform_create(serializer)


class VPIProgressEntryViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]
    permission_map = {
        "write": ["manage_jobwork"],
    }
    queryset = VPIProgressEntry.objects.select_related("instruction")
    serializer_class = VPIProgressEntrySerializer
    audit_entity_type = "VPIProgressEntry"
    audit_label_field = "id"
    status_field = None
    default_date_field = "date"
    search_fields = ["entered_by", "remarks"]
    ordering = ["-date", "-created_at"]
    filter_map = {
        "instructionId": "instruction_id",
        "instruction_id": "instruction_id",
    }
