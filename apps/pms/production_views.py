"""Production endpoints (HRMS_ERP_CHANGES.md).

Write gates reuse ``view_staff``/``edit_staff`` for employee selection and the
new ``view_production``/``enter_production``/``verify_production`` ids for
production work. ``mark_attendance`` is never accepted here.
"""
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
        "verify": ["verify_production"],
        "complete": ["enter_production"],
        "completion": ["view_production"],
    }

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            inProgress=Count("id", filter=Q(status="In Progress")),
            verified=Count("id", filter=Q(status="Verified")),
            completed=Count("id", filter=Q(status="Completed")),
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        instruction = services.create_instruction(
            client=self.request.user.client,
            employee=data["employee"],
            agency_name=data["agency_name"],
            order_reference=data["order_reference"],
            order_meter=data.get("order_meter") or 0,
            supervisor=data.get("supervisor"),
            notes=data.get("notes"),
            pi_date=data.get("pi_date"),
            fabric=data.get("fabric"),
            process_type=data.get("process_type"),
            agreed_job_rate=data.get("agreed_job_rate") or 0,
            user=self.request.user,
        )
        serializer.instance = instruction
        self._created_instance = instruction
        self._concurrency_instance = instruction
        return instruction

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
            entered_by_employee=data["entered_by_employee"],
            entered_by_user=self.request.user,
            remarks=data.get("remarks"),
        )
        serializer.instance = entry
        self._created_instance = entry
        self._concurrency_instance = entry
        self.write_audit("create", entry, after={"meters": str(entry.meters)})
        return entry


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
