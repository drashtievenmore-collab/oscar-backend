"""Production endpoints (HRMS_ERP_CHANGES.md).

Write gates reuse ``view_staff``/``edit_staff`` for employee selection and the
new ``view_production``/``enter_production``/``verify_production`` ids for
production work. ``mark_attendance`` is never accepted here.
"""
from django.db import transaction
from django.db.models import Count, Q
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import NotFound
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import production_reports as reports, production_services as services
from .models import (
    DailyProductionEntry,
    IncentiveCalculation,
    IncentiveScheme,
    ProductionInstruction,
)
from .production_serializers import (
    CalculateIncentiveSerializer,
    DailyProductionEntrySerializer,
    IncentiveCalculationSerializer,
    IncentiveSchemeSerializer,
    ProductionInstructionSerializer,
    VerifyInstructionSerializer,
)


class ProductionInstructionViewSet(TenantModelViewSet):
    queryset = ProductionInstruction.objects.select_related("employee", "supervisor")
    serializer_class = ProductionInstructionSerializer
    audit_entity_type = "ProductionInstruction"
    audit_label_field = "instruction_number"
    status_field = "status"
    search_fields = ["instruction_number", "agency_name", "order_reference"]
    ordering = ["-created_at"]
    filter_map = {
        "agency": "agency_name",
        "employeeId": "employee_id",
        "supervisorId": "supervisor_id",
    }
    permission_map = {
        "read": ["view_production"],
        "write": ["enter_production"],
        "create": ["enter_production"],
        "approve": ["verify_production"],
        "start": ["enter_production"],
        "verify": ["verify_production"],
        "complete": ["enter_production"],
        "completion": ["view_production"],
        "generate_po": ["verify_production"],
        "three_way_match": ["view_production"],
    }

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            inProgress=Count("id", filter=Q(status__in=["Running", "In Progress", "Partially Completed"])),
            verified=Count("id", filter=Q(status__in=["Verified", "PO Pending", "PO Created"])),
            completed=Count("id", filter=Q(status="Completed")),
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        instruction = services.create_instruction(
            client=self.request.user.client,
            employee=data["employee"],
            agency_name=data.get("agency_name"),
            agency=data.get("agency"),
            order_reference=data["order_reference"],
            order_meter=data.get("order_meter") or 0,
            taka_count=data.get("taka_count") or 0,
            total_weight=data.get("total_weight") or 0,
            width=data.get("width"),
            gsm=data.get("gsm"),
            tentative_rate=data.get("tentative_rate") or 0,
            agreed_rate=data.get("agreed_rate") or data.get("agreed_job_rate") or 0,
            rate_uom=data.get("rate_uom") or "Meter",
            special_terms=data.get("special_terms"),
            expected_start_date=data.get("expected_start_date"),
            supervisor=data.get("supervisor"),
            notes=data.get("notes"),
            pi_date=data.get("pi_date"),
            fabric=data.get("fabric"),
            process_type=data.get("process_type"),
            agreed_job_rate=data.get("agreed_job_rate") or data.get("agreed_rate") or 0,
            grey_lot=data.get("grey_lot"),
            status="Draft",
            user=self.request.user,
        )
        serializer.instance = instruction
        self._created_instance = instruction
        self._concurrency_instance = instruction
        return instruction

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        instruction = services.approve_instruction(self.get_object(), user=request.user)
        return Response(self.get_serializer(instruction).data)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        instruction = services.start_instruction(self.get_object(), user=request.user)
        return Response(self.get_serializer(instruction).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        serializer = VerifyInstructionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        raw_rejected = (
            request.data.get("rejectedQty")
            if isinstance(request.data, dict) and "rejectedQty" in request.data
            else data.get("rejectedQty", data.get("rejected_qty", 0))
        )
        instruction = services.verify_instruction(
            self.get_object(),
            user=request.user,
            reason=data.get("reason") or request.data.get("reason"),
            rejected_qty=raw_rejected,
        )
        return Response(self.get_serializer(instruction).data)

    @action(detail=True, methods=["post"], url_path="generate-po")
    def generate_po(self, request, pk=None):
        instruction = services.generate_final_job_work_po(self.get_object(), user=request.user)
        return Response(self.get_serializer(instruction).data)

    @action(detail=True, methods=["post"], url_path="three-way-match")
    def three_way_match(self, request, pk=None):
        invoice_qty = request.data.get("invoiceQty") or request.data.get("qty")
        invoice_rate = request.data.get("invoiceRate") or request.data.get("rate")
        result = services.agency_three_way_match(self.get_object(), invoice_qty, invoice_rate)
        return Response(result)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        instruction = self.get_object()
        self.check_concurrency(instruction)
        instruction = services.complete_instruction(
            instruction, user=request.user
        )
        return Response(self.get_serializer(instruction).data)

    @action(detail=True, methods=["get"])
    def completion(self, request, pk=None):
        return Response(services.instruction_completion(self.get_object()))

    @action(detail=True, methods=["get"])
    def progress(self, request, pk=None):
        return Response(services.instruction_progress(self.get_object()))


class DailyProductionEntryViewSet(TenantModelViewSet):
    queryset = DailyProductionEntry.objects.select_related(
        "instruction", "entered_by_employee"
    )
    serializer_class = DailyProductionEntrySerializer
    audit_entity_type = "DailyProductionEntry"
    status_field = None
    default_date_field = "entry_date"
    ordering = ["entry_date"]
    filter_map = {
        "instructionId": "instruction_id",
        "employeeId": "entered_by_employee_id",
        "date": "entry_date",
    }
    permission_map = {
        "read": ["view_production"],
        "write": ["enter_production"],
        "create": ["enter_production"],
    }

    def perform_create(self, serializer):
        data = serializer.validated_data
        entry = services.record_daily_entry(
            client=self.request.user.client,
            instruction=data["instruction"],
            entry_date=data["entry_date"],
            meters=data["meters"],
            produced_takas=data.get("produced_takas") or 0,
            produced_weight=data.get("produced_weight") or 0,
            good_meter=data.get("good_meter") or data["meters"],
            reject_meter=data.get("reject_meter") or 0,
            reprocess_meter=data.get("reprocess_meter") or 0,
            photo_file=data.get("photo_file"),
            entered_by_employee=data["entered_by_employee"],
            entered_by_user=self.request.user,
            remarks=data.get("remarks"),
        )
        serializer.instance = entry
        self._created_instance = entry
        self._concurrency_instance = entry
        self.write_audit("create", entry, after={"meters": str(entry.meters)})
        return entry

    @transaction.atomic
    def perform_update(self, serializer):
        services.lock_open_instruction(serializer.instance.instruction)
        new_instruction = serializer.validated_data.get("instruction")
        if new_instruction and new_instruction.pk != serializer.instance.instruction_id:
            services.lock_open_instruction(new_instruction)
        return super().perform_update(serializer)

    @transaction.atomic
    def perform_destroy(self, instance):
        services.lock_open_instruction(instance.instruction)
        return super().perform_destroy(instance)


class IncentiveSchemeViewSet(TenantModelViewSet):
    queryset = IncentiveScheme.objects.all()
    serializer_class = IncentiveSchemeSerializer
    audit_entity_type = "IncentiveScheme"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {
        "read": ["view_production"],
        "write": ["verify_production"],
        "create": ["verify_production"],
        "calculate": ["enter_production"],
    }

    @action(detail=True, methods=["post"])
    def calculate(self, request, pk=None):
        scheme = self.get_object()
        serializer = CalculateIncentiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        calc = services.calculate_incentive(
            client=request.user.client,
            scheme=scheme,
            employee=data["employeeId"],
            period_label=data["periodLabel"],
            period_start=data["periodStart"],
            period_end=data["periodEnd"],
            user=request.user,
        )
        return Response(IncentiveCalculationSerializer(calc).data)


class IncentiveCalculationViewSet(ReadOnlyTenantViewSet):
    queryset = IncentiveCalculation.objects.select_related("scheme", "employee")
    serializer_class = IncentiveCalculationSerializer
    ordering = ["-period_start"]
    filter_map = {"employeeId": "employee_id", "period": "period_label"}
    permission_map = {"read": ["view_production"]}

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        calc = services.approve_incentive(self.get_object(), user=request.user)
        self.write_audit("approve", calc, description="Incentive calculation approved.")
        return Response(self.get_serializer(calc).data)

    @action(detail=True, methods=["post"], url_path="post-to-ledger")
    def post_to_ledger(self, request, pk=None):
        calc = services.post_incentive(self.get_object(), user=request.user)
        self.write_audit("post", calc, description="Incentive posted to ledger.")
        return Response(self.get_serializer(calc).data)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from apps.accounting.models import BankAccount
        bank_account = None
        bank_account_id = request.data.get("bankAccountId")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                id=bank_account_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()
        amount = request.data.get("amount")
        calc = services.pay_incentive(
            self.get_object(), bank_account=bank_account, amount=amount, user=request.user
        )
        self.write_audit("pay", calc, description="Incentive paid.")
        return Response(self.get_serializer(calc).data)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "verify_production")
        calc = services.reverse_incentive(
            self.get_object(), user=request.user, reason=request.data.get("reason")
        )
        self.write_audit("reverse", calc, description=request.data.get("reason"))
        return Response(self.get_serializer(calc).data)


class AgencyProductionView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_production"]

    def get(self, request):
        day = request.query_params.get("day")
        return Response(
            envelope(reports.agency_production_view(request.client_id, day=day))
        )


class EmployeeProductionView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_production"]

    def get(self, request):
        rows = reports.employee_production_view(
            request.client_id,
            employee_id=request.query_params.get("employeeId"),
            day=request.query_params.get("day"),
        )
        return Response(envelope(rows))


class SalespersonView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_production"]

    def get(self, request):
        from apps.core.exceptions import ValidationFailed

        employee_id = request.query_params.get("employeeId")
        period_start = request.query_params.get("periodStart")
        period_end = request.query_params.get("periodEnd")
        missing = {}
        if not employee_id:
            missing["employeeId"] = ["An employee is required."]
        if not period_start:
            missing["periodStart"] = ["A period start date is required."]
        if not period_end:
            missing["periodEnd"] = ["A period end date is required."]
        if missing:
            raise ValidationFailed("Some fields need attention.", field_errors=missing)
        row = reports.salesperson_view(
            request.client_id,
            employee_id=employee_id,
            period_start=period_start,
            period_end=period_end,
        )
        if row is None:
            raise NotFound("That employee no longer exists.")
        return Response(row)
