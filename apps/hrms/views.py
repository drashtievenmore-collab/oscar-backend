"""HRMS endpoints (api.md §11)."""
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import notify, record_audit
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission, has_permission, IsAuthenticatedInTenant
from apps.core.printing import PdfNotAvailable, print_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import services
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    ApprovalChain,
    Application,
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetRequest,
    Attendance,
    AttendanceRegularization,
    CalendarEvent,
    Candidate,
    Complaint,
    CompOff,
    Department,
    Designation,
    Employee,
    Goal,
    Holiday,
    HrDocument,
    Interview,
    Job,
    Kpi,
    LeaveBalance,
    LeaveEncashment,
    LeaveRequest,
    LeaveType,
    Location,
    Offer,
    OnboardingTask,
    PayrollRun,
    Payslip,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    SalaryAdvance,
    SalaryStructure,
    ScreeningQuestion,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
    WorkingDay,
    SalespersonIncentiveScheme,
    SalespersonIncentiveAccrual,
    BrokerCommissionRecord,
)
from .serializers import (
    AppraisalCycleSerializer,
    AppraisalSerializer,
    ApprovalChainSerializer,
    ApplicationSerializer,
    AssetCategorySerializer,
    AssetRequestSerializer,
    AssetSerializer,
    AttendanceSerializer,
    BulkAttendanceSerializer,
    CalendarEventSerializer,
    CandidateSerializer,
    ComplaintSerializer,
    CompOffSerializer,
    DepartmentSerializer,
    DesignationSerializer,
    EmployeeSerializer,
    GoalSerializer,
    HolidaySerializer,
    HrDocumentSerializer,
    InterviewSerializer,
    JobSerializer,
    KpiSerializer,
    LeaveBalanceSerializer,
    LeaveEncashmentSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    LocationSerializer,
    OfferSerializer,
    OnboardingTaskSerializer,
    PayrollRunSerializer,
    PayslipSerializer,
    PerformanceIndicatorSerializer,
    PolicyAcknowledgementSerializer,
    PolicyCategorySerializer,
    PolicySerializer,
    ProcessPayrollSerializer,
    RegularizationSerializer,
    ResignationSerializer,
    SalaryAdvanceSerializer,
    SalaryStructureSerializer,
    ScreeningQuestionSerializer,
    TeamSerializer,
    TerminationSerializer,
    TrainerSerializer,
    TrainingParticipantSerializer,
    TrainingSerializer,
    WorkingDaySerializer,
    SalespersonIncentiveSchemeSerializer,
    SalespersonIncentiveAccrualSerializer,
    BrokerCommissionRecordSerializer,
)


# ---------------------------------------------------------------------------

def reporting_chain_ids(client_id, manager_id):
    """Ids of every employee below manager_id (direct and indirect reports)."""
    found, frontier = set(), {manager_id}
    while frontier:
        below = set(
            Employee.objects.filter(
                client_id=client_id, manager_id__in=frontier, deleted_at__isnull=True
            ).values_list("id", flat=True)
        ) - found - {manager_id}
        found |= below
        frontier = below
    return found


class OwnEmployeeScopeMixin:
    """Record-level scope for self-service HR data.
    Holders of team_scope_permissions see all tenant rows;
    everyone else sees (and writes) only rows for their own linked employee.
    """
    team_scope_permissions = ()
    manager_read_scope = False
    delegate_read_field = None

    def has_team_scope(self):
        return has_permission(self.request.user, tuple(self.team_scope_permissions))

    def _resolve_employee(self, request, employee_id=None):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=client_id, deleted_at__isnull=True
            ).first()

        if getattr(request.user, "employee", None):
            return request.user.employee

        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp
        return None

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.has_team_scope():
            return queryset
        employee_id = getattr(self.request.user, "employee_id", None)
        if not employee_id:
            resolved = self._resolve_employee(self.request)
            if resolved:
                employee_id = resolved.id
        if not employee_id:
            return queryset.none()
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            return queryset.filter(employee_id=employee_id)
        visible = [employee_id]
        if self.manager_read_scope:
            visible += reporting_chain_ids(self.request.client_id, employee_id)
        scope = Q(employee_id__in=visible)
        if self.delegate_read_field:
            scope |= Q(**{self.delegate_read_field: employee_id})
        return queryset.filter(scope)

    def check_own_employee(self, employee):
        if self.has_team_scope():
            return
        resolved = self._resolve_employee(self.request)
        own = resolved.pk if resolved else getattr(self.request.user, "employee_id", None)
        if employee is None or own is None or employee.pk != own:
            raise PermissionDenied(
                "You can only do this for your own employee record.",
                code=self.team_scope_permissions[0] if self.team_scope_permissions else "PERMISSION_DENIED",
            )


# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class EmployeeViewSet(TenantModelViewSet):
    @action(detail=False, methods=["get"])
    def me(self, request):
        """GET /hrms/employees/me/ -- caller's own employee record."""
        employee_id = getattr(request.user, "employee_id", None)
        employee = (
            self.get_queryset().filter(pk=employee_id).first()
            if employee_id else None
        )
        if employee is None:
            try:
                from apps.hrms.user_link import linked_employee
                employee = linked_employee(request.user)
            except Exception:
                pass
        if employee is None and getattr(request.user, "email", None):
            employee = Employee.objects.filter(
                client_id=self.get_client_id(), email__iexact=request.user.email.strip(), deleted_at__isnull=True
            ).first()
            if employee and not request.user.employee_id:
                request.user.employee = employee
                request.user.save(update_fields=["employee"])
        if employee is None:
            raise NotFound(
                "Your login is not linked to an employee record. Ask HR to link it.",
                code="NO_EMPLOYEE",
            )
        return Response(self.get_serializer(employee).data)

    @action(detail=False, methods=["get"])
    def colleagues(self, request):
        """GET /hrms/employees/colleagues/ -- names only, for the delegate picker."""
        rows = (
            Employee.objects.filter(client_id=self.get_client_id(), deleted_at__isnull=True)
            .exclude(status__in=["Resigned", "Terminated"])
            .select_related("department", "designation")
            .order_by("name")
        )
        return Response(envelope([
            {
                "id": str(e.id),
                "name": e.name,
                "department": e.department.name if e.department_id else "",
                "designation": e.designation.name if e.designation_id else "",
            }
            for e in rows
        ]))

    queryset = Employee.objects.select_related(
        "designation", "department", "manager", "location", "salary_structure"
    )
    serializer_class = EmployeeSerializer
    audit_entity_type = "Employee"
    audit_label_field = "employee_code"
    status_field = "status"
    search_fields = ["name", "employee_code", "email", "phone"]
    ordering = ["name"]
    default_date_field = "joining_date"
    filter_map = {
        "department": "department__name",
        "departmentId": "department_id",
        "designation": "designation__name",
        "location": "location__name",
        "managerId": "manager_id",
        "employmentType": "employment_type",
    }
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"], "create": ["create_staff"], "me": [], "colleagues": []}
    def get_queryset(self):
        try:
            from apps.hrms.services import sync_all_users_and_employees
            sync_all_users_and_employees(self.get_client_id())
        except Exception:
            pass
        return super().get_queryset()

    def _ensure_employee_users_linked(self):
        try:
            from apps.hrms.services import sync_all_users_and_employees
            sync_all_users_and_employees(self.get_client_id())
        except Exception:
            pass

    @staticmethod
    def _link_user_to_employee(user):
        from apps.hrms.services import sync_employee_from_user
        return sync_employee_from_user(user)

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            probation=Count("id", filter=Q(status="Probation")),
            exited=Count("id", filter=Q(status__in=["Resigned", "Terminated"])),
        )

    @staticmethod
    def _normalize_employee_payload(raw, client_id, user):
        """Accept the drawer's flat labels, not just FK ids.

        The directory posts `department`/`designation`/`location` names and
        `joiningDate`; the serializer's writable keys are the `…Id` FKs plus
        `joining`. Without this the names are ignored (read-only) and the
        create 400s on a missing `joining`.
        """
        data = dict(raw)
        by_user = user if getattr(user, "is_authenticated", False) else None

        def resolve(model, name, extra=None):
            label = str(name or "").strip()
            if not label:
                return None
            row = model.objects.filter(
                client_id=client_id, name__iexact=label, deleted_at__isnull=True
            ).first()
            if row is None:
                row = model.objects.create(
                    client_id=client_id, name=label, created_by=by_user,
                    **(extra or {}),
                )
            return str(row.id)

        if not data.get("departmentId") and not data.get("department_id"):
            name = data.get("department") or data.get("dept")
            resolved = resolve(Department, name)
            if resolved:
                data["departmentId"] = resolved
        if not data.get("designationId") and not data.get("designation_id"):
            name = data.get("designation") or data.get("role")
            resolved = resolve(Designation, name)
            if resolved:
                data["designationId"] = resolved
        if not data.get("locationId") and not data.get("location_id"):
            resolved = resolve(Location, data.get("location"))
            if resolved:
                data["locationId"] = resolved
        if not data.get("joining") and data.get("joiningDate") is None and data.get("joining_date") is None:
            # A display string (`Mar 15, 2022`) or nothing — serializer
            # defaults to today; drop unparsable text instead of 400ing.
            text = data.get("doj") or data.get("date")
            if text:
                data["joining"] = text
        # Read-only display keys must not reach validation as unknowns.
        for key in ("dept", "role", "doj", "date", "img", "empId", "id"):
            data.pop(key, None)
        return data

    def create(self, request, *args, **kwargs):
        raw = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
        data = self._normalize_employee_payload(raw, self.get_client_id(), request.user)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        raw = request.data.dict() if hasattr(request.data, "dict") else dict(request.data)
        data = self._normalize_employee_payload(raw, self.get_client_id(), request.user)
        serializer = self.get_serializer(instance, data=data, partial=partial or request.method == "PATCH")
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_create(self, serializer):
        serializer.validated_data["employee_code"] = allocate_number(
            self.request.user.client, "EMP"
        )
        super().perform_create(serializer)
        employee = serializer.instance
        self._ensure_user_for_employee(
            employee, self.get_client_id(), actor=self.request.user
        )
        return employee

    @staticmethod
    def _ensure_user_for_employee(employee, client_id, actor=None):
        """Keep the Administration -> Users row in step with the employee row."""
        from apps.hrms.services import sync_user_from_employee
        return sync_user_from_employee(employee, client_id=client_id, actor=actor)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        employee = serializer.instance
        self._ensure_user_for_employee(
            employee, self.get_client_id(), actor=self.request.user
        )
        return employee

    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        try:
            from apps.accounts.models import User, UserSession
            user = User.objects.filter(
                client_id=instance.client_id, employee_id=instance.id, deleted_at__isnull=True
            ).first()
            if user:
                user.status = "Inactive"
                user.is_active = False
                user.save(update_fields=["status", "is_active", "updated_at"])
                UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                    revoked_at=timezone.now(), revoked_reason="employee_deleted"
                )
        except Exception:
            pass

    @action(detail=False, methods=["post"], url_path="backfill-users")
    @transaction.atomic
    def backfill_users(self, request):
        from apps.hrms.services import sync_all_users_and_employees
        sync_all_users_and_employees(request.client_id)
        from apps.accounts.models import User
        count = User.objects.filter(client_id=request.client_id, employee__isnull=False, deleted_at__isnull=True).count()
        return Response({"linked": count})

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def terminate(self, request, pk=None):
        employee = self.get_object()
        if employee.status == "Terminated":
            raise Conflict("This employee is already terminated.", code=Codes.ALREADY_DONE)

        reason = request.data.get("reason")
        last_working_day = request.data.get("lastWorkingDay") or timezone.localdate()

        employee.status = "Terminated"
        employee.termination_reason = reason
        employee.last_working_day = last_working_day
        employee.save(
            update_fields=["status", "termination_reason", "last_working_day", "updated_at"]
        )

        Termination.objects.create(
            client_id=request.client_id,
            employee=employee,
            reason=reason,
            last_working_day=last_working_day,
            status="Completed",
            created_by=request.user,
        )
        # A terminated employee must not keep an active login.
        from apps.accounts.models import User, UserSession

        users = User.objects.filter(employee=employee, deleted_at__isnull=True)
        users.update(status="Inactive", is_active=False)
        UserSession.objects.filter(user__in=users, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="employee_terminated"
        )

        self.write_audit("terminate", employee, description=reason)
        return Response(self.get_serializer(employee).data)

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        employee = self.get_object()
        rows = HrDocument.objects.filter(
            client_id=request.client_id, employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(HrDocumentSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def assets(self, request, pk=None):
        employee = self.get_object()
        rows = Asset.objects.filter(
            client_id=request.client_id, assigned_employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(AssetSerializer(rows, many=True).data))


class HrmsDepartmentViewSet(TenantModelViewSet):
    queryset = Department.objects.select_related("head_employee", "parent")
    serializer_class = DepartmentSerializer
    audit_entity_type = "HrmsDepartment"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code"]
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            employee_count=Count(
                "employees", filter=Q(employees__deleted_at__isnull=True), distinct=True
            ),
            team_count=Count("teams", filter=Q(teams__deleted_at__isnull=True), distinct=True),
        )


class DesignationViewSet(TenantModelViewSet):
    queryset = Designation.objects.select_related("department")
    serializer_class = DesignationSerializer
    audit_entity_type = "Designation"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class HrmsLocationViewSet(TenantModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    audit_entity_type = "HrmsLocation"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class OrgChartView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        return Response({"tree": services.org_chart(request.client_id)})


class HrmsDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["show_hrm_dashboard"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()
        employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)

        attendance_today = Attendance.objects.filter(
            client_id=client_id, work_date=today, deleted_at__isnull=True
        ).values("status").annotate(count=Count("id"))

        upcoming = CalendarEvent.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            starts_at__gte=timezone.now(),
            starts_at__lte=timezone.now() + timedelta(days=30),
        ).order_by("starts_at")[:10]

        exits = employees.filter(
            status__in=["Resigned", "Terminated"],
            last_working_day__gte=today - timedelta(days=365),
        ).count()
        headcount = employees.filter(status__in=["Active", "On Leave", "Probation"]).count()

        return Response(
            {
                "headcount": headcount,
                "newJoinersThisMonth": employees.filter(
                    joining_date__year=today.year, joining_date__month=today.month
                ).count(),
                "attritionRatePct": round(exits / headcount * 100, 1) if headcount else 0,
                "onLeaveToday": Attendance.objects.filter(
                    client_id=client_id, work_date=today, status="On Leave",
                    deleted_at__isnull=True,
                ).count(),
                "attendanceToday": {
                    row["status"]: row["count"] for row in attendance_today
                },
                "pendingLeaveRequests": LeaveRequest.objects.filter(
                    client_id=client_id,
                    status__in=["Pending Review", "Delegate Confirmed"],
                    deleted_at__isnull=True,
                ).count(),
                "openPositions": Job.objects.filter(
                    client_id=client_id, status="Open", deleted_at__isnull=True
                ).count(),
                "upcomingEvents": CalendarEventSerializer(upcoming, many=True).data,
            }
        )


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendanceViewSet(TenantModelViewSet):
    queryset = Attendance.objects.select_related("employee", "employee__department")
    serializer_class = AttendanceSerializer
    audit_entity_type = "Attendance"
    status_field = "status"
    default_date_field = "work_date"
    allowed_date_fields = ("work_date",)
    ordering = ["-work_date"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "date": "work_date",
    }
    permission_map = {
        "read": ["view_team_attendance"],
        "write": ["mark_attendance"],
        "today": [],
        "punch": [],
        "punch_timeline": [],
    }

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            wfh=Count("id", filter=Q(status="WFH")),
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data["employee"],
            work_date=data["work_date"],
            check_in=data.get("check_in"),
            check_out=data.get("check_out"),
            status=data.get("status"),
            remark=data.get("remark"),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._created_instance = row
        self._concurrency_instance = row
        return row

    def perform_update(self, serializer):
        data = serializer.validated_data
        instance = serializer.instance
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data.get("employee", instance.employee),
            work_date=data.get("work_date", instance.work_date),
            check_in=data.get("check_in", instance.check_in),
            check_out=data.get("check_out", instance.check_out),
            status=data.get("status", instance.status),
            remark=data.get("remark", instance.remark),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._concurrency_instance = row
        return row

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def bulk(self, request):
        """``{ date, records: [] }`` or ``{ ids: [], status }`` (api.md §11.2)."""
        serializer = BulkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("ids") and data.get("status"):
            updated = Attendance.objects.filter(
                client_id=request.client_id, pk__in=data["ids"], deleted_at__isnull=True
            ).update(status=data["status"], source="bulk")
            return Response({"updated": updated})

        work_date = data.get("date") or timezone.localdate()
        written = 0
        for record in data.get("records", []):
            employee_id = record.get("employeeId") or record.get("employee_id") or record.get("id")
            employee = None
            try:
                employee = Employee.objects.filter(
                    pk=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            except (ValueError, TypeError, Exception):
                employee = None
            if employee is None and employee_id:
                employee = Employee.objects.filter(
                    employee_code=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            if employee is None:
                continue
            services.mark_attendance(
                client=request.user.client,
                employee=employee,
                work_date=record.get("date") or work_date,
                check_in=record.get("checkIn"),
                check_out=record.get("checkOut"),
                status=record.get("status"),
                remark=record.get("remark"),
                source="bulk",
                user=request.user,
            )
            written += 1
        return Response({"written": written, "date": work_date})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        rows = services.attendance_summary(
            request.client_id,
            month=parsed,
            employee_id=request.query_params.get("employeeId"),
            department_id=request.query_params.get("departmentId"),
        )
        return Response(envelope(rows))

    @action(detail=False, methods=["get"], url_path=r"individual/(?P<employee_id>[^/.]+)")
    def individual(self, request, employee_id=None):
        rows = Attendance.objects.filter(
            client_id=request.client_id, employee_id=employee_id, deleted_at__isnull=True
        ).order_by("work_date")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            rows = rows.filter(work_date__gte=date_from)
        if date_to:
            rows = rows.filter(work_date__lte=date_to)
        return Response(envelope(AttendanceSerializer(rows, many=True).data))

    @action(detail=False, methods=["get"])
    def audit(self, request):
        """``GET /hrms/attendance/audit/`` -- the manual-edit trail (db.md §11.2)."""
        from apps.core.models import AuditLog
        from apps.core.serializers_platform import AuditLogSerializer

        rows = AuditLog.objects.filter(
            client_id=request.client_id,
            entity_type="Attendance",
            action__in=["attendance_edit", "attendance_mark"],
        )[:200]
        return Response(envelope(AuditLogSerializer(rows, many=True).data))

    def _resolve_employee(self, request, employee_id=None):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=client_id, deleted_at__isnull=True
            ).first()

        if getattr(request.user, "employee", None):
            return request.user.employee

        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        from apps.hrms.models import Designation
        desig = Designation.objects.filter(client_id=client_id, name__iexact="Employee").first()
        if not desig:
            desig = Designation.objects.create(client_id=client_id, name="Employee")
        code = f"EMP-{(Employee.objects.filter(client_id=client_id).count() + 1):03d}"
        emp = Employee.objects.create(
            client_id=client_id,
            employee_code=code,
            name=getattr(request.user, "name", "") or request.user.email,
            email=request.user.email,
            phone=getattr(request.user, "phone", "") or "",
            designation=desig,
            joining_date=timezone.localdate(),
            status="Active",
        )
        request.user.employee = emp
        request.user.save(update_fields=["employee"])
        return emp

    @action(detail=False, methods=["get"])
    def today(self, request):
        """GET /hrms/attendance/today/ -- punch state for current employee today."""
        employee_id = request.query_params.get("employeeId")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            return Response({
                "has_employee": False,
                "status": "Not Punched In",
                "is_punched_in": False,
                "punches": [],
                "formatted_working_time": "00h 00m",
                "working_seconds": 0,
                "working_hours": 0.0,
                "late_minutes": 0,
                "first_punch": None,
                "last_punch": None,
                "date": timezone.localdate().isoformat(),
            })
        status_data = services.get_today_punch_status(self.get_client_id(), employee)
        return Response(status_data)

    @action(detail=False, methods=["post"])
    def punch(self, request):
        """POST /hrms/attendance/punch/ -- record Punch In or Punch Out."""
        punch_type = (
            request.data.get("punch_type")
            or request.data.get("punchType")
            or request.data.get("type")
        )
        if not punch_type:
            raise ValidationFailed(
                "Punch type ('IN' or 'OUT') is required.",
                field_errors={"punch_type": ["Required."]},
            )

        employee = self._resolve_employee(
            request, request.data.get("employeeId") or request.data.get("employee_id")
        )
        if not employee:
            raise ValidationFailed(
                "No employee profile linked to current user account.",
                code="NO_EMPLOYEE",
            )

        remark = request.data.get("remark") or request.data.get("notes")
        source = request.data.get("source") or "web"
        early_reason = (
            request.data.get("earlyReason")
            or request.data.get("early_reason")
            or request.data.get("reason")
        )
        request_regularization = bool(
            request.data.get("requestRegularization")
            or request.data.get("request_regularization")
        )

        punch_obj, status_data = services.record_punch(
            client=self.get_client_id(),
            employee=employee,
            punch_type=punch_type,
            source=source,
            remark=remark,
            early_reason=early_reason,
            request_regularization=request_regularization,
            user=request.user,
        )
        return Response(
            status_data,
            status=status.HTTP_201_CREATED if punch_obj else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="punch-timeline")
    def punch_timeline(self, request):
        employee_id = request.query_params.get("employeeId")
        date_str = request.query_params.get("date")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            return Response([])
        work_date = timezone.localdate()
        if date_str:
            try:
                work_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        status_data = services.get_today_punch_status(self.get_client_id(), employee, work_date=work_date)
        return Response(status_data.get("punches", []))


class RegularizationViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    team_scope_permissions = ("view_team_attendance",)
    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        return super().perform_create(serializer)

    queryset = AttendanceRegularization.objects.select_related("employee")
    serializer_class = RegularizationSerializer
    audit_entity_type = "AttendanceRegularization"
    status_field = "status"
    default_date_field = "work_date"
    ordering = ["-work_date"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": [], "create": [], "write": ["regularize_attendance"]}

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark, status }`` -- an approval rewrites the attendance row."""
        approved = self.request.data.get("approved")
        status_val = self.request.data.get("status")
        if approved is True or (status_val and str(status_val).lower() == "approved"):
            is_approved = True
        elif approved is False or (status_val and str(status_val).lower() == "rejected"):
            is_approved = False
        else:
            is_approved = None

        row = super().perform_update(serializer)

        if is_approved is not None:
            row.status = "Approved" if is_approved else "Rejected"
            row.approver = self.request.user if getattr(self.request.user, "is_authenticated", False) else None
            row.decided_at = timezone.now()
            row.remark = self.request.data.get("remark", row.remark)
            row.save(update_fields=["status", "approver", "decided_at", "remark", "updated_at"])

            if is_approved:
                req_status = row.requested_status or "Present"
                services.mark_attendance(
                    client=getattr(self.request.user, "client", None) or row.client,
                    employee=row.employee,
                    work_date=row.work_date,
                    check_in=row.requested_check_in,
                    check_out=row.requested_check_out,
                    status=req_status,
                    remark=f"Regularized: {row.reason or ''}".strip(),
                    source="regularization",
                    user=self.request.user if getattr(self.request.user, "is_authenticated", False) else None,
                )
        return row

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def approve(self, request, pk=None):
        row = self.get_object()
        row.status = "Approved"
        row.approver = request.user if getattr(request.user, "is_authenticated", False) else None
        row.decided_at = timezone.now()
        row.remark = request.data.get("remark", row.remark)
        row.save(update_fields=["status", "approver", "decided_at", "remark", "updated_at"])

        req_status = row.requested_status or "Present"
        services.mark_attendance(
            client=row.client,
            employee=row.employee,
            work_date=row.work_date,
            check_in=row.requested_check_in,
            check_out=row.requested_check_out,
            status=req_status,
            remark=f"Regularized: {row.reason or ''}".strip(),
            source="regularization",
            user=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        self.write_audit("approve", row, description="Attendance regularization approved")
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        row = self.get_object()
        row.status = "Rejected"
        row.approver = request.user if getattr(request.user, "is_authenticated", False) else None
        row.decided_at = timezone.now()
        row.remark = request.data.get("remark", row.remark)
        row.save(update_fields=["status", "approver", "decided_at", "remark", "updated_at"])
        self.write_audit("reject", row, description="Attendance regularization rejected")
        return Response(self.get_serializer(row).data)


class FlexibilityPolicyView(APIView):
    """``GET/PUT /hrms/attendance/flexibility/``."""

    permission_classes = [HasModulePermission]
    permission_map = {
        "read": ["view_team_attendance"],
        "write": ["mark_attendance"],
        "today": [],
        "punch": [],
        "punch_timeline": [],
    }

    def get(self, request):
        return Response(services.flexibility_policy(request.client_id))

    def put(self, request):
        from apps.core.models import Setting

        policy = services.flexibility_policy(request.client_id)
        policy.update(request.data if isinstance(request.data, dict) else {})
        Setting.objects.update_or_create(
            client_id=request.client_id,
            key="attendance_flexibility",
            defaults={"value": policy, "updated_by": request.user},
        )
        return Response(policy)


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeViewSet(TenantModelViewSet):
    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer
    audit_entity_type = "LeaveType"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["apply_leave"], "write": ["approve_leave"]}


class LeaveRequestViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    team_scope_permissions = ("approve_leave",)
    manager_read_scope = True
    delegate_read_field = "delegate_employee_id" 
    queryset = LeaveRequest.objects.select_related(
        "employee", "leave_type", "delegate_employee"
    )
    serializer_class = LeaveRequestSerializer
    audit_entity_type = "LeaveRequest"
    status_field = "status"
    default_date_field = "from_date"
    ordering = ["-from_date"]
    search_fields = ["employee__name", "reason"]
    filter_map = {"employeeId": "employee_id", "type": "leave_type__name", "leaveTypeId": "leave_type_id"}
    permission_map = {"read": [], "create": [], "write": ["approve_leave"], "cancel": [], "confirm_delegate": []}

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status__in=["Pending Review", "Delegate Confirmed"])),
            approved=Count("id", filter=Q(status="Approved")),
            rejected=Count("id", filter=Q(status="Rejected")),
        )

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        request_row = super().perform_create(serializer)
        services.assert_no_overlap(request_row)
        return request_row

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark }`` or ``{ status }`` (api.md §11.3)."""
        approved = self.request.data.get("approved")
        target_status = self.request.data.get("status")
        instance = serializer.instance

        if approved is True or target_status == "Approved":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_leave")
            row = services.approve_leave(
                instance, user=self.request.user, remark=self.request.data.get("remark")
            )
            self._concurrency_instance = row
            self.write_audit("approve", row, description="Leave approved")
            return row

        if approved is False or target_status == "Rejected":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_leave")
            instance.status = "Rejected"
            instance.approver = self.request.user
            instance.decided_at = timezone.now()
            instance.remark = self.request.data.get("remark")
            instance.save()
            self._concurrency_instance = instance
            self.write_audit("reject", instance, description=instance.remark)
            return instance

        return super().perform_update(serializer)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        row = services.cancel_leave(
            self.get_object(), user=request.user, reason=request.data.get("reason")
        )
        self.write_audit("cancel", row, description=request.data.get("reason"))
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"], url_path="confirm-delegate")
    def confirm_delegate(self, request, pk=None):
        row = self.get_object()
        row.delegate_confirmed_at = timezone.now()
        row.status = "Delegate Confirmed"
        row.save(update_fields=["delegate_confirmed_at", "status", "updated_at"])
        return Response(self.get_serializer(row).data)


class LeaveBalanceViewSet(ReadOnlyTenantViewSet):
    queryset = LeaveBalance.objects.select_related("employee", "leave_type")
    serializer_class = LeaveBalanceSerializer
    status_field = None
    filter_map = {"employeeId": "employee_id", "leaveTypeId": "leave_type_id", "year": "period_year"}
    ordering = ["employee__name"]
    permission_map = {"read": ["apply_leave"]}


class CompOffViewSet(TenantModelViewSet):
    queryset = CompOff.objects.select_related("employee")
    serializer_class = CompOffSerializer
    audit_entity_type = "CompOff"
    status_field = None
    default_date_field = "worked_date"
    ordering = ["-worked_date"]
    filter_map = {"employeeId": "employee_id", "used": "used"}
    permission_map = {"read": ["apply_leave"], "write": ["approve_leave"]}


class LeaveEncashmentViewSet(TenantModelViewSet):
    queryset = LeaveEncashment.objects.select_related("employee", "leave_type")
    serializer_class = LeaveEncashmentSerializer
    audit_entity_type = "LeaveEncashment"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["apply_leave"], "write": ["approve_leave"]}


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class PayslipViewSet(TenantModelViewSet):
    queryset = Payslip.objects.select_related(
        "employee", "employee__department", "employee__designation", "bank_account"
    ).prefetch_related("components")
    serializer_class = PayslipSerializer
    audit_entity_type = "Payslip"
    status_field = "status"
    default_date_field = "period_month"
    ordering = ["-period_month", "employee__name"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "month": "period_month",
    }
    permission_map = {
        "read": ["view_own_payslip"],
        "write": ["generate_payroll"],
        "approve": ["approve_payroll"],
        "mark_paid": ["approve_payroll"],
    }
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        # Without the team-wide permission, a user sees only their own payslips.
        if not has_permission(self.request.user, "generate_payroll") and not has_permission(
            self.request.user, "approve_payroll"
        ):
            employee_id = getattr(self.request.user, "employee_id", None)
            queryset = queryset.filter(employee_id=employee_id) if employee_id else queryset.none()
        return queryset

    def get_aggregates(self, queryset):
        return services.payroll_summary(self.get_client_id())

    def perform_update(self, serializer):
        """Adjust earnings / deductions before approval (api.md §11.4)."""
        payslip = super().perform_update(serializer)
        override = self.request.data.get("earnedSalary")
        services.recompute_payslip(
            payslip,
            earned_salary_override=override if override is not None else None,
            user=self.request.user,
        )
        payslip.refresh_from_db()
        return payslip

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        payslip = self.get_object()
        if payslip.status == "Paid":
            raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
        payslip.status = "Approved"
        payslip.save(update_fields=["status", "updated_at"])
        self.write_audit("approve", payslip, description="Payslip approved")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        from apps.accounting.models import BankAccount

        bank_account = None
        bank_account_id = request.data.get("bankAccountId")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                pk=bank_account_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        payslip = services.mark_payslip_paid(
            self.get_object(),
            payment_date=request.data.get("paymentDate"),
            bank_account=bank_account,
            user=request.user,
        )
        self.write_audit("mark_paid", payslip, description="Payslip marked paid")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["get"])
    def payslip(self, request, pk=None):
        row = self.get_object()
        return Response(
            print_payload(row, self.get_serializer_class(), request=request, title="Payslip")
        )

    @action(detail=False, methods=["post"])
    def process(self, request):
        """``POST /hrms/payroll/process/`` -- generate the run."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "generate_payroll")

        serializer = ProcessPayrollSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.process_payroll(
            client=request.user.client,
            period_month=serializer.validated_data["month"],
            employee_ids=serializer.validated_data.get("employeeIds") or None,
            user=request.user,
        )
        return Response(
            {
                "run": PayrollRunSerializer(result["run"]).data,
                "payslips": PayslipSerializer(result["payslips"], many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        return Response(services.payroll_summary(request.client_id, parsed))


class SalaryStructureViewSet(TenantModelViewSet):
    queryset = SalaryStructure.objects.all()
    serializer_class = SalaryStructureSerializer
    audit_entity_type = "SalaryStructure"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_own_payslip"], "write": ["edit_salary_structure"]}


class SalaryAdvanceViewSet(TenantModelViewSet):
    queryset = SalaryAdvance.objects.select_related("employee")
    serializer_class = SalaryAdvanceSerializer
    audit_entity_type = "SalaryAdvance"
    status_field = "status"
    default_date_field = "issued_on"
    ordering = ["-issued_on"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["view_own_payslip"], "write": ["generate_payroll"]}


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobViewSet(TenantModelViewSet):
    queryset = Job.objects.select_related("department", "designation", "location")
    serializer_class = JobSerializer
    audit_entity_type = "Job"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "description"]
    ordering = ["-created_at"]
    filter_map = {"departmentId": "department_id", "isPublished": "is_published"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            applicant_count=Count("applications", distinct=True)
        )

    def perform_create(self, serializer):
        from django.utils.text import slugify

        job = super().perform_create(serializer)
        if not job.slug:
            base = slugify(job.title)[:120] or "role"
            slug, suffix = base, 1
            while Job.objects.filter(
                client_id=self.get_client_id(), slug=slug, deleted_at__isnull=True
            ).exclude(pk=job.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"
            job.slug = slug
            job.save(update_fields=["slug"])
        return job

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        job = self.get_object()
        job.is_published = True
        job.published_at = timezone.now()
        job.save(update_fields=["is_published", "published_at", "updated_at"])
        return Response(self.get_serializer(job).data)


class CandidateViewSet(TenantModelViewSet):
    queryset = Candidate.objects.select_related("resume_file", "employee")
    serializer_class = CandidateSerializer
    audit_entity_type = "Candidate"
    audit_label_field = "name"
    status_field = "stage"
    search_fields = ["name", "email", "phone"]
    ordering = ["-created_at"]
    filter_map = {"stage": "stage", "source": "source"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class ApplicationViewSet(TenantModelViewSet):
    queryset = Application.objects.select_related("candidate", "job")
    serializer_class = ApplicationSerializer
    audit_entity_type = "Application"
    status_field = "stage"
    ordering = ["-applied_at"]
    filter_map = {"jobId": "job_id", "candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class InterviewViewSet(TenantModelViewSet):
    queryset = Interview.objects.select_related("application", "application__candidate")
    serializer_class = InterviewSerializer
    audit_entity_type = "Interview"
    status_field = "status"
    ordering = ["scheduled_at"]
    filter_map = {"applicationId": "application_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class OfferViewSet(TenantModelViewSet):
    queryset = Offer.objects.select_related("application", "application__candidate")
    serializer_class = OfferSerializer
    audit_entity_type = "Offer"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"applicationId": "application_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    def perform_update(self, serializer):
        previous = serializer.instance.status
        offer = super().perform_update(serializer)
        if previous != offer.status:
            if offer.status == "Sent" and offer.sent_at is None:
                offer.sent_at = timezone.now()
            if offer.status in ("Accepted", "Declined") and offer.responded_at is None:
                offer.responded_at = timezone.now()
            offer.save(update_fields=["sent_at", "responded_at", "updated_at"])

            if offer.status == "Accepted":
                candidate = offer.application.candidate
                candidate.stage = "Offer"
                candidate.save(update_fields=["stage", "updated_at"])
        return offer

    @action(detail=True, methods=["get"])
    def letter(self, request, pk=None):
        offer = self.get_object()
        return Response(
            print_payload(
                offer, self.get_serializer_class(), request=request, title="Offer Letter"
            )
        )


class OnboardingViewSet(TenantModelViewSet):
    queryset = OnboardingTask.objects.select_related("candidate", "owner")
    serializer_class = OnboardingTaskSerializer
    audit_entity_type = "OnboardingTask"
    status_field = None
    ordering = ["due_date"]
    filter_map = {"candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(
        detail=False, methods=["post"], url_path=r"(?P<candidate_id>[^/.]+)/complete"
    )
    @transaction.atomic
    def complete(self, request, candidate_id=None):
        """Convert candidate -> employee, keeping the hiring trail (db.md §11.5)."""
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        if candidate.employee_id:
            raise Conflict(
                "This candidate has already been onboarded.",
                code=Codes.ALREADY_DONE,
                payload={"employeeId": str(candidate.employee_id)},
            )

        offer = (
            Offer.objects.filter(
                application__candidate=candidate, status="Accepted", deleted_at__isnull=True
            )
            .order_by("-created_at")
            .first()
        )
        application = (
            Application.objects.filter(candidate=candidate, deleted_at__isnull=True)
            .select_related("job")
            .order_by("-applied_at")
            .first()
        )

        employee = Employee.objects.create(
            client_id=request.client_id,
            employee_code=allocate_number(request.user.client, "EMP"),
            name=candidate.name,
            email=candidate.email,
            phone=candidate.phone,
            joining_date=(
                offer.joining_date if offer and offer.joining_date else timezone.localdate()
            ),
            department=application.job.department if application else None,
            designation=application.job.designation if application else None,
            location=application.job.location if application else None,
            employment_type=application.job.employment_type if application else None,
            standard_salary=(offer.offered_ctc / 12) if offer and offer.offered_ctc else ZERO,
            status="Probation",
            created_by=request.user,
        )
        candidate.employee = employee
        candidate.stage = "Hired"
        candidate.save(update_fields=["employee", "stage", "updated_at"])
        EmployeeViewSet._ensure_user_for_employee(
            employee, request.client_id, actor=request.user
        )
        record_audit(
            client=request.client_id,
            actor=request.user,
            action="onboard",
            entity_type="Employee",
            entity_id=employee.id,
            entity_label=employee.employee_code,
            description=f"Onboarded from candidate {candidate.name}",
        )
        return Response(
            EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<candidate_id>[^/.]+)/verify-documents",
    )
    def verify_documents(self, request, candidate_id=None):
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        OnboardingTask.objects.filter(
            client_id=request.client_id, candidate=candidate,
            title__icontains="document", completed_at__isnull=True,
        ).update(completed_at=timezone.now())
        return Response({"verified": True})


class ScreeningQuestionViewSet(TenantModelViewSet):
    queryset = ScreeningQuestion.objects.select_related("job")
    serializer_class = ScreeningQuestionSerializer
    audit_entity_type = "ScreeningQuestion"
    status_field = None
    ordering = ["sort_order"]
    filter_map = {"jobId": "job_id", "isActive": "is_active"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class RecruitmentFunnelView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request, section=None):
        client_id = request.client_id
        applications = Application.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        )
        rows = applications.values("stage").annotate(count=Count("id"))
        funnel = {row["stage"]: row["count"] for row in rows}

        if section == "funnel":
            return Response(
                envelope(
                    [
                        {"stage": stage, "count": funnel.get(stage, 0)}
                        for stage in [
                            "Applied", "Screening", "Interview", "Offer", "Hired", "Rejected",
                        ]
                    ]
                )
            )

        jobs = Job.objects.filter(client_id=client_id, deleted_at__isnull=True)
        return Response(
            {
                "openJobs": jobs.filter(status="Open").count(),
                "totalApplications": applications.count(),
                "interviewsScheduled": Interview.objects.filter(
                    client_id=client_id, status="Scheduled", deleted_at__isnull=True
                ).count(),
                "offersOut": Offer.objects.filter(
                    client_id=client_id, status="Sent", deleted_at__isnull=True
                ).count(),
                "hired": funnel.get("Hired", 0),
                "funnel": funnel,
            }
        )


# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleViewSet(TenantModelViewSet):
    queryset = AppraisalCycle.objects.all()
    serializer_class = AppraisalCycleSerializer
    audit_entity_type = "AppraisalCycle"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["-period_start"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceIndicatorViewSet(TenantModelViewSet):
    queryset = PerformanceIndicator.objects.select_related("department")
    serializer_class = PerformanceIndicatorSerializer
    audit_entity_type = "PerformanceIndicator"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class KpiViewSet(TenantModelViewSet):
    queryset = Kpi.objects.select_related("indicator")
    serializer_class = KpiSerializer
    audit_entity_type = "Kpi"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class AppraisalViewSet(TenantModelViewSet):
    queryset = Appraisal.objects.select_related(
        "cycle", "employee", "manager"
    ).prefetch_related("kpi_scores__kpi", "history__actor")
    serializer_class = AppraisalSerializer
    audit_entity_type = "Appraisal"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"cycleId": "cycle_id", "employeeId": "employee_id", "stage": "stage"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def _transition(self, appraisal, *, to_stage, to_status, action_name, comment=None,
                    require=None):
        """Every transition appends to ``history[]`` with actor, stage, timestamp
        and comment (api.md §11.6)."""
        if require and not has_permission(self.request.user, require):
            raise PermissionDenied(
                "You don't have permission to do that.", code=require
            )

        from_stage = appraisal.stage
        appraisal.stage = to_stage
        appraisal.status = to_status
        appraisal.save(update_fields=["stage", "status", "updated_at"])

        AppraisalHistory.objects.create(
            client_id=appraisal.client_id,
            appraisal=appraisal,
            from_stage=from_stage,
            to_stage=to_stage,
            actor=self.request.user,
            action=action_name,
            comment=comment,
        )
        return appraisal

    @action(detail=True, methods=["post"], url_path="self-review")
    def self_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.self_rating = request.data.get("rating", appraisal.self_rating)
        appraisal.self_comments = request.data.get("comments", appraisal.self_comments)
        appraisal.strengths = request.data.get("strengths", appraisal.strengths)
        appraisal.areas_for_improvement = request.data.get(
            "areasForImprovement", appraisal.areas_for_improvement
        )
        appraisal.save()
        self._transition(
            appraisal, to_stage="Manager Review", to_status="Submitted",
            action_name="self_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="manager-review")
    def manager_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.manager_rating = request.data.get("rating", appraisal.manager_rating)
        appraisal.development_feedback = request.data.get(
            "developmentFeedback", appraisal.development_feedback
        )
        appraisal.save()

        from .models import AppraisalKpiScore

        for score in request.data.get("kpiScores") or []:
            kpi_id = score.get("kpiId")
            if not kpi_id:
                continue
            AppraisalKpiScore.objects.update_or_create(
                appraisal=appraisal,
                kpi_id=kpi_id,
                defaults={
                    "client_id": appraisal.client_id,
                    "target": score.get("target"),
                    "achieved": score.get("achieved"),
                    "score": score.get("score"),
                    "weight": score.get("weight"),
                },
            )

        self._transition(
            appraisal, to_stage="HR Review", to_status="In Progress",
            action_name="manager_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="return")
    def return_to_stage(self, request, pk=None):
        appraisal = self.get_object()
        target = request.data.get("returnToStage") or "Self Review"
        self._transition(
            appraisal, to_stage=target, to_status="Returned",
            action_name="returned", comment=request.data.get("reason"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.hr_comments = request.data.get("hrComments", appraisal.hr_comments)
        appraisal.save(update_fields=["hr_comments", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Approved",
            action_name="hr_approved", comment=request.data.get("hrComments"),
            require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def finalize(self, request, pk=None):
        """Close and sync the rating to the profile (api.md §11.6)."""
        appraisal = self.get_object()
        appraisal.final_rating = request.data.get(
            "finalRating", appraisal.manager_rating or appraisal.self_rating
        )
        appraisal.save(update_fields=["final_rating", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Completed",
            action_name="finalized", require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        from .serializers import AppraisalHistorySerializer

        appraisal = self.get_object()
        return Response(
            envelope(
                AppraisalHistorySerializer(appraisal.history.all(), many=True).data
            )
        )


class GoalViewSet(TenantModelViewSet):
    queryset = Goal.objects.select_related("employee", "cycle")
    serializer_class = GoalSerializer
    audit_entity_type = "Goal"
    audit_label_field = "title"
    status_field = "status"
    ordering = ["target_date"]
    filter_map = {"employeeId": "employee_id", "cycleId": "cycle_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        appraisals = Appraisal.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        )
        by_stage = appraisals.values("stage").annotate(count=Count("id"))
        by_status = appraisals.values("status").annotate(count=Count("id"))
        return Response(
            {
                "total": appraisals.count(),
                "completed": appraisals.filter(status="Completed").count(),
                "byStage": {row["stage"]: row["count"] for row in by_stage},
                "byStatus": {row["status"]: row["count"] for row in by_status},
                "goalsOnTrack": Goal.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True,
                    progress_pct__gte=50,
                ).count(),
            }
        )


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerViewSet(TenantModelViewSet):
    queryset = Trainer.objects.select_related("employee")
    serializer_class = TrainerSerializer
    audit_entity_type = "Trainer"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class TrainingViewSet(TenantModelViewSet):
    queryset = Training.objects.select_related("trainer", "department").prefetch_related(
        "participants__employee"
    )
    serializer_class = TrainingSerializer
    audit_entity_type = "Training"
    audit_label_field = "title"
    status_field = "stage"
    default_date_field = "start_date"
    ordering = ["-start_date"]
    search_fields = ["title", "description", "venue"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @action(detail=True, methods=["post"], url_path="assign-trainer")
    def assign_trainer(self, request, pk=None):
        training = self.get_object()
        trainer_id = request.data.get("trainerId")
        trainer = Trainer.objects.filter(
            pk=trainer_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if trainer is None:
            raise ValidationFailed(
                "Unknown trainer.", field_errors={"trainerId": ["Not found."]}
            )
        training.trainer = trainer
        training.type = request.data.get("trainerType") or trainer.kind
        if training.stage == "Requested":
            training.stage = "Trainer Assigned"
        training.save(update_fields=["trainer", "type", "stage", "updated_at"])
        return Response(self.get_serializer(training).data)

    @action(detail=True, methods=["post"])
    def participants(self, request, pk=None):
        training = self.get_object()
        employee_ids = request.data.get("employeeIds") or []
        rows = TrainingParticipant.objects.bulk_create(
            [
                TrainingParticipant(
                    client_id=request.client_id, training=training, employee_id=employee_id
                )
                for employee_id in employee_ids
            ],
            ignore_conflicts=True,
        )
        return Response({"enrolled": len(rows)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def evaluate(self, request, pk=None):
        training = self.get_object()
        participant = training.participants.filter(
            pk=request.data.get("participantId"), deleted_at__isnull=True
        ).first()
        if participant is None:
            raise NotFound("That participant is not on this training.")
        participant.rating = request.data.get("rating")
        participant.feedback = request.data.get("feedback")
        participant.evaluated_at = timezone.now()
        participant.save()

        if not training.participants.filter(evaluated_at__isnull=True).exists():
            training.stage = "Evaluated"
            training.save(update_fields=["stage", "updated_at"])
        return Response(TrainingParticipantSerializer(participant).data)

    @action(detail=False, methods=["get"])
    def funnel(self, request):
        rows = self.get_queryset().values("stage").annotate(count=Count("id"))
        return Response(
            envelope([{"stage": row["stage"], "count": row["count"]} for row in rows])
        )

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        queryset = self.get_queryset()
        return Response(
            {
                "total": queryset.count(),
                "ongoing": queryset.filter(stage="Ongoing").count(),
                "completed": queryset.filter(stage__in=["Completed", "Evaluated"]).count(),
                "participants": TrainingParticipant.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True
                ).count(),
            }
        )


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetViewSet(TenantModelViewSet):
    queryset = Asset.objects.select_related(
        "category", "assigned_employee", "assigned_employee__department", "location"
    ).prefetch_related("history__employee")
    serializer_class = AssetSerializer
    audit_entity_type = "Asset"
    audit_label_field = "asset_code"
    status_field = "status"
    search_fields = ["name", "asset_code", "serial_number"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id", "employeeId": "assigned_employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("asset_code"):
            serializer.validated_data["asset_code"] = allocate_number(
                self.request.user.client, "ASSET"
            )
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def assign(self, request, pk=None):
        asset = self.get_object()
        employee = Employee.objects.filter(
            pk=request.data.get("employeeId"), client_id=request.client_id,
            deleted_at__isnull=True,
        ).first()
        if employee is None:
            raise ValidationFailed(
                "Unknown employee.", field_errors={"employeeId": ["Not found."]}
            )
        if asset.history.filter(returned_at__isnull=True).exists():
            raise Conflict(
                "This asset is already assigned. Record a return first.",
                code=Codes.IN_USE,
            )

        AssetAssignment.objects.create(
            client_id=request.client_id,
            asset=asset,
            employee=employee,
            assigned_by=request.user,
            notes=request.data.get("notes"),
        )
        asset.assigned_employee = employee
        asset.status = "Assigned"
        asset.save(update_fields=["assigned_employee", "status", "updated_at"])
        self.write_audit("assign", asset, description=f"Assigned to {employee.name}")
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=["post"], url_path="return")
    @transaction.atomic
    def return_asset(self, request, pk=None):
        asset = self.get_object()
        assignment = asset.history.filter(returned_at__isnull=True).first()
        if assignment is None:
            raise Conflict("This asset is not currently assigned.", code=Codes.BAD_TARGET)

        assignment.returned_at = timezone.now()
        assignment.returned_condition = request.data.get("condition")
        assignment.notes = request.data.get("notes", assignment.notes)
        assignment.save()

        asset.assigned_employee = None
        asset.condition = request.data.get("condition") or asset.condition
        asset.status = (
            "Under Maintenance"
            if request.data.get("condition") == "Needs Repair"
            else "Available"
        )
        asset.save(update_fields=["assigned_employee", "condition", "status", "updated_at"])
        self.write_audit("return", asset, description=request.data.get("notes"))
        return Response(self.get_serializer(asset).data)


class AssetCategoryViewSet(TenantModelViewSet):
    queryset = AssetCategory.objects.all()
    serializer_class = AssetCategorySerializer
    audit_entity_type = "AssetCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class AssetRequestViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    team_scope_permissions = ("edit_staff", "view_staff")
    queryset = AssetRequest.objects.select_related("employee", "category", "fulfilled_asset")
    serializer_class = AssetRequestSerializer
    audit_entity_type = "AssetRequest"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": [], "create": [], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        return super().perform_create(serializer)


class HrDocumentViewSet(TenantModelViewSet):
    queryset = HrDocument.objects.select_related("employee", "file", "created_by")
    serializer_class = HrDocumentSerializer
    audit_entity_type = "HrDocument"
    audit_label_field = "title"
    status_field = None
    search_fields = ["title", "description", "category"]
    ordering = ["-created_at"]
    filter_map = {"category": "category", "employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        # `status` is derived, so it is filtered after the query rather than in SQL.
        requested = self.request.query_params.getlist("status")
        if requested:
            today = timezone.localdate()
            if "Expired" in requested and len(requested) == 1:
                queryset = queryset.filter(valid_until__lt=today)
            elif "Valid" in requested and len(requested) == 1:
                queryset = queryset.filter(
                    Q(valid_until__isnull=True) | Q(valid_until__gte=today)
                )
        return queryset

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        from apps.core.files import public_url

        document = self.get_object()
        if document.file_id is None:
            raise NotFound("No file is attached to this document.")
        return Response({"downloadUrl": public_url(document.file, request)})


class PolicyViewSet(TenantModelViewSet):
    queryset = Policy.objects.select_related("category", "owner_department").prefetch_related(
        "version_history"
    )
    serializer_class = PolicySerializer
    audit_entity_type = "Policy"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "summary"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id"}
    permission_map = {"read": [], "write": ["edit_staff"], "acknowledge": []}

    @transaction.atomic
    def perform_update(self, serializer):
        """An edit bumps ``version`` and appends to ``versionHistory[]``."""
        instance = serializer.instance
        previous_body = instance.body
        policy = super().perform_update(serializer)

        if policy.body != previous_body:
            policy.version += 1
            policy.save(update_fields=["version", "updated_at"])
            PolicyVersion.objects.create(
                client_id=policy.client_id,
                policy=policy,
                version=policy.version,
                body=policy.body,
                file=policy.file,
                changed_by=self.request.user,
                change_note=self.request.data.get("changeNote"),
            )
        return policy

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Pending Approval"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Active"
        policy.approved_by = request.user
        policy.approved_at = timezone.now()
        policy.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        if policy.ack_required:
            employees = Employee.objects.filter(
                client_id=request.client_id, status="Active", deleted_at__isnull=True
            )
            PolicyAcknowledgement.objects.bulk_create(
                [
                    PolicyAcknowledgement(
                        client_id=request.client_id,
                        policy=policy,
                        policy_version=policy.version,
                        employee=employee,
                    )
                    for employee in employees
                ],
                ignore_conflicts=True,
            )
        self.write_audit("approve", policy, description="Policy approved")
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Archived"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["get"])
    def acknowledgements(self, request, pk=None):
        policy = self.get_object()
        rows = policy.acknowledgements.filter(deleted_at__isnull=True).select_related(
            "employee"
        )
        return Response(
            envelope(
                PolicyAcknowledgementSerializer(
                    rows, many=True, context={"policy": policy}
                ).data
            )
        )

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        policy = self.get_object()
        employee_id = getattr(request.user, "employee_id", None)
        if not employee_id:
            try:
                from apps.hrms.user_link import linked_employee
                emp = linked_employee(request.user)
                if emp:
                    employee_id = emp.id
            except Exception:
                pass
        if not employee_id and getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=request.client_id, email__iexact=request.user.email.strip(), deleted_at__isnull=True
            ).first()
            if emp:
                employee_id = emp.id
                if not getattr(request.user, "employee_id", None):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
        if employee_id is None:
            raise BusinessRuleViolation(
                "Your account is not linked to an employee record.",
                code="NO_EMPLOYEE_RECORD",
            )
        row, _ = PolicyAcknowledgement.objects.get_or_create(
            client_id=request.client_id,
            policy=policy,
            policy_version=policy.version,
            employee_id=employee_id,
        )
        row.acknowledged_at = timezone.now()
        row.save(update_fields=["acknowledged_at", "updated_at"])
        return Response(
            PolicyAcknowledgementSerializer(row, context={"policy": policy}).data
        )


class PolicyCategoryViewSet(TenantModelViewSet):
    queryset = PolicyCategory.objects.all()
    serializer_class = PolicyCategorySerializer
    audit_entity_type = "PolicyCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class CalendarEventViewSet(TenantModelViewSet):
    queryset = CalendarEvent.objects.select_related("department")
    serializer_class = CalendarEventSerializer
    audit_entity_type = "CalendarEvent"
    audit_label_field = "title"
    status_field = None
    default_date_field = "starts_at"
    ordering = ["starts_at"]
    search_fields = ["title", "description", "location"]
    filter_map = {"type": "type", "dept": "department__name", "departmentId": "department_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        params = self.request.query_params
        if params.get("from"):
            queryset = queryset.filter(starts_at__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(starts_at__lte=params["to"])
        return queryset

    def perform_destroy(self, instance):
        if instance.source_type == "LeaveRequest":
            raise Conflict(
                "Leave events are generated from the leave request.",
                code="GENERATED_EVENT",
                detail="Cancel the leave request instead.",
            )
        super().perform_destroy(instance)


class HolidayViewSet(TenantModelViewSet):
    queryset = Holiday.objects.select_related("location")
    serializer_class = HolidaySerializer
    audit_entity_type = "Holiday"
    audit_label_field = "name"
    status_field = None
    default_date_field = "date"
    ordering = ["date"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class WorkingDayView(APIView):
    """``GET/PUT /hrms/working-days/``."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get(self, request):
        rows = WorkingDay.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).order_by("weekday")
        return Response(envelope(WorkingDaySerializer(rows, many=True).data))

    @transaction.atomic
    def put(self, request):
        days = request.data.get("days") or request.data
        if not isinstance(days, list):
            raise ValidationFailed(
                "Provide a list of weekday configurations.",
                field_errors={"days": ["Expected a list."]},
            )
        for row in days:
            WorkingDay.objects.update_or_create(
                client_id=request.client_id,
                weekday=row.get("weekday"),
                location_id=row.get("locationId"),
                defaults={
                    "is_working": row.get("isWorking", True),
                    "shift_start": row.get("shiftStart"),
                    "shift_end": row.get("shiftEnd"),
                },
            )
        return self.get(request)


class TeamViewSet(TenantModelViewSet):
    queryset = Team.objects.select_related("department", "lead").prefetch_related("members")
    serializer_class = TeamSerializer
    audit_entity_type = "Team"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class ApprovalChainViewSet(TenantModelViewSet):
    queryset = ApprovalChain.objects.all()
    serializer_class = ApprovalChainSerializer
    audit_entity_type = "ApprovalChain"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["name"]
    filter_map = {"appliesTo": "applies_to"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class TerminationViewSet(TenantModelViewSet):
    queryset = Termination.objects.select_related("employee")
    serializer_class = TerminationSerializer
    audit_entity_type = "Termination"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["delete_staff"]}

    def perform_create(self, serializer):
        row = super().perform_create(serializer)
        services.process_employee_separation(
            row.employee, separation_type="Terminated", last_working_day=row.last_working_day, user=self.request.user
        )
        return row

    def perform_update(self, serializer):
        row = super().perform_update(serializer)
        services.process_employee_separation(
            row.employee, separation_type="Terminated", last_working_day=row.last_working_day, user=self.request.user
        )
        return row


class ResignationViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    team_scope_permissions = ("edit_staff", "view_staff")
    queryset = Resignation.objects.select_related("employee").prefetch_related("checklist")
    serializer_class = ResignationSerializer
    audit_entity_type = "Resignation"
    status_field = "status"
    default_date_field = "submitted_on"
    ordering = ["-submitted_on"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": [], "create": [], "cancel": [], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        instance = self.get_object()
        self.check_own_employee(instance.employee)
        instance.status = "Cancelled"
        instance.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(instance).data)

    def perform_update(self, serializer):
        row = super().perform_update(serializer)
        if row.status in ["Approved", "Completed", "Exited"]:
            services.process_employee_separation(
                row.employee, separation_type="Resigned", last_working_day=row.last_working_day, user=self.request.user
            )
        return row


class ComplaintViewSet(TenantModelViewSet):
    """HR-only. db.md §11.8 asks for restricted access rather than relying on
    endpoint checks alone; this is the endpoint half."""

    queryset = Complaint.objects.select_related(
        "raised_by_employee", "against_employee", "assigned_to"
    )
    serializer_class = ComplaintSerializer
    audit_entity_type = "Complaint"
    status_field = "status"
    ordering = ["-created_at"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("raised_by_employee"):
            employee_id = getattr(self.request.user, "employee_id", None)
            if employee_id:
                serializer.validated_data["raised_by_employee_id"] = employee_id
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        complaint = super().perform_update(serializer)
        if complaint.status in ("Resolved", "Closed") and complaint.resolved_at is None:
            complaint.resolved_at = timezone.now()
            complaint.save(update_fields=["resolved_at", "updated_at"])
        return complaint


class HrmsSettingsView(APIView):
    """``GET/PUT /hrms/settings/`` -- the HRMS setup screen."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get(self, request):
        from apps.core.models import Setting

        row = Setting.objects.filter(client_id=request.client_id, key="hrms").first()
        return Response(row.value if row else {})

    def put(self, request):
        from apps.core.models import Setting

        row, _ = Setting.objects.update_or_create(
            client_id=request.client_id,
            key="hrms",
            defaults={"value": request.data, "updated_by": request.user},
        )
        return Response(row.value)


# =============================================================================
# Textile Operational HR & Commercial ViewSets (Oscar Letter Pad Scope)
# =============================================================================

class SalespersonIncentiveSchemeViewSet(TenantModelViewSet):
    queryset = SalespersonIncentiveScheme.objects.all()
    serializer_class = SalespersonIncentiveSchemeSerializer
    audit_entity_type = "SalespersonIncentiveScheme"
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class SalespersonIncentiveViewSet(TenantModelViewSet):
    queryset = SalespersonIncentiveAccrual.objects.select_related("employee", "scheme")
    serializer_class = SalespersonIncentiveAccrualSerializer
    audit_entity_type = "SalespersonIncentiveAccrual"
    permission_classes = [HasModulePermission]
    permission_map = {
        "read": ["view_staff"],
        "write": ["edit_staff"],
        "approve": ["approve_payroll"],
        "calculate_quarterly": ["edit_staff"],
        "hold_payout": ["approve_payroll"],
        "release_payout": ["approve_payroll"],
    }

    @action(detail=False, methods=["POST"], url_path="calculate-quarterly")
    def calculate_quarterly(self, request):
        from apps.sales.models import SalesInvoice, SalesReturn
        import calendar
        quarter = int(request.data.get("quarter", (timezone.localdate().month - 1) // 3 + 1))
        year = int(request.data.get("year", timezone.localdate().year))
        employee_id = request.data.get("employee_id")

        start_month = (quarter - 1) * 3 + 1
        end_month = quarter * 3
        start_date = datetime(year, start_month, 1).date()
        last_day = calendar.monthrange(year, end_month)[1]
        end_date = datetime(year, end_month, last_day).date()
        period_name = f"Q{quarter} {year}"

        schemes = SalespersonIncentiveScheme.objects.filter(
            client_id=request.client_id, is_active=True
        ).prefetch_related("applicable_salespersons")

        salespersons_qs = Employee.objects.filter(client_id=request.client_id, status="Active")
        if employee_id:
            salespersons_qs = salespersons_qs.filter(id=employee_id)

        results = []
        for sp in salespersons_qs:
            scheme = schemes.filter(applicable_salespersons=sp).first() or schemes.first()
            # Sum gross sales using correct DocumentHeader doc_date and total
            invoices = SalesInvoice.objects.filter(
                client_id=request.client_id,
                doc_date__range=[start_date, end_date],
            ).filter(
                Q(salesperson_fk=sp) | Q(salesperson__iexact=sp.name)
            ).exclude(status__in=["Cancelled", "Draft"])

            gross_sales = sum((inv.total for inv in invoices), ZERO)

            # Sum sales returns using doc_date and total
            returns = SalesReturn.objects.filter(
                client_id=request.client_id,
                doc_date__range=[start_date, end_date],
            ).filter(
                Q(salesperson_fk=sp) | Q(salesperson__iexact=sp.name)
            ).exclude(status__in=["Cancelled", "Draft"])
            returns_deduction = sum((ret.total for ret in returns), ZERO)

            net_sales = max(ZERO, gross_sales - returns_deduction)

            applicable_rate = D("0.0")
            calculated_incentive = ZERO

            if scheme:
                if scheme.slab_type == "FLAT":
                    applicable_rate = D(str(scheme.flat_rate_percent))
                    calculated_incentive = round2(net_sales * (applicable_rate / D("100.0")))
                else:
                    slabs = scheme.slabs or []
                    for slab in sorted(slabs, key=lambda s: float(s.get("min_amount", 0))):
                        min_amt = D(str(slab.get("min_amount", 0)))
                        if net_sales >= min_amt:
                            applicable_rate = D(str(slab.get("rate_percent", 0)))
                    calculated_incentive = round2(net_sales * (applicable_rate / D("100.0")))

            # Check for bad-debt overdue invoices (>90 days past due date with balance due)
            overdue_cutoff = timezone.localdate() - timedelta(days=90)
            overdue_invoices = SalesInvoice.objects.filter(
                client_id=request.client_id,
                due_date__lte=overdue_cutoff,
            ).filter(
                Q(salesperson_fk=sp) | Q(salesperson__iexact=sp.name)
            ).exclude(status__in=["Cancelled", "Draft"])

            has_bad_debt = any(inv.balance_due > ZERO for inv in overdue_invoices)
            initial_status = "HELD" if has_bad_debt else "ACCRUED"
            hold_reason = "Commercial policy hold: overdue unpaid customer invoices (>90 days)" if has_bad_debt else ""

            accrual, _ = SalespersonIncentiveAccrual.objects.update_or_create(
                client_id=request.client_id,
                employee=sp,
                period_name=period_name,
                defaults={
                    "scheme": scheme,
                    "period_type": "QUARTERLY",
                    "start_date": start_date,
                    "end_date": end_date,
                    "gross_sales": gross_sales,
                    "returns_deduction": returns_deduction,
                    "net_eligible_sales": net_sales,
                    "applicable_rate": applicable_rate,
                    "calculated_incentive": calculated_incentive,
                    "status": initial_status,
                    "hold_reason": hold_reason,
                },
            )
            results.append(SalespersonIncentiveAccrualSerializer(accrual).data)

        return Response({"period_name": period_name, "count": len(results), "accruals": results})

    @action(detail=True, methods=["POST"], url_path="hold")
    def hold_payout(self, request, pk=None):
        accrual = self.get_object()
        accrual.status = "HELD"
        accrual.hold_reason = request.data.get("reason", "Management hold")
        accrual.save(update_fields=["status", "hold_reason", "updated_at"])
        return Response(self.get_serializer(accrual).data)

    @action(detail=True, methods=["POST"], url_path="release")
    def release_payout(self, request, pk=None):
        accrual = self.get_object()
        accrual.status = "ACCRUED"
        accrual.hold_reason = ""
        accrual.save(update_fields=["status", "hold_reason", "updated_at"])
        return Response(self.get_serializer(accrual).data)

    @action(detail=True, methods=["POST"], url_path="approve")
    def approve_payout(self, request, pk=None):
        from apps.accounting.services import post_incentive_accrual
        accrual = self.get_object()
        accrual.status = "APPROVED"
        accrual.approved_by = request.user
        accrual.approved_at = timezone.now()
        accrual.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        try:
            post_incentive_accrual(accrual, user=request.user)
        except Exception:
            pass
        return Response(self.get_serializer(accrual).data)

    @action(detail=True, methods=["POST"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        """Pays the incentive outside payroll (letter pad §12) and clears the
        Incentive Payable that approval accrued."""
        from apps.accounting.services import post_incentive_payout
        accrual = self.get_object()
        if accrual.status == "PAID":
            return Response(self.get_serializer(accrual).data)
        if accrual.status != "APPROVED":
            raise ValidationFailed(
                "Approve the incentive before marking it paid.",
                field_errors={"status": ["Must be Approved."]},
            )
        post_incentive_payout(accrual, user=request.user)
        accrual.status = "PAID"
        accrual.paid_at = timezone.now()
        accrual.save(update_fields=["status", "paid_at", "updated_at"])
        return Response(self.get_serializer(accrual).data)


class BrokerCommissionViewSet(TenantModelViewSet):
    queryset = BrokerCommissionRecord.objects.select_related("broker", "invoice", "party", "salesperson")
    serializer_class = BrokerCommissionRecordSerializer
    audit_entity_type = "BrokerCommissionRecord"
    permission_classes = [HasModulePermission]
    permission_map = {
        "read": ["view_staff"],
        "write": ["edit_staff"],
        "approve": ["approve_payroll"],
        "mark_payable": ["edit_staff"],
        "mark_paid": ["approve_payroll"],
        "hold": ["edit_staff"],
    }

    @action(detail=True, methods=["POST"], url_path="approve")
    def approve(self, request, pk=None):
        item = self.get_object()
        item.status = "Approved"
        item.approved_by = request.user
        item.approved_at = timezone.now()
        item.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["POST"], url_path="mark-payable")
    def mark_payable(self, request, pk=None):
        item = self.get_object()
        item.status = "Payable"
        item.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["POST"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        item = self.get_object()
        item.status = "Paid"
        item.paid_at = timezone.now()
        item.paid_amount = item.commission_amount
        item.save(update_fields=["status", "paid_at", "paid_amount", "updated_at"])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["POST"], url_path="hold")
    def hold(self, request, pk=None):
        item = self.get_object()
        item.status = "Hold"
        item.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(item).data)


class StubLifecycleViewSet(viewsets.ViewSet):
    """Fallback stub for non-scope lifecycle events (transfers, promotions, warnings, awards)."""
    permission_classes = [IsAuthenticatedInTenant]

    def list(self, request):
        return Response(envelope([]))

    def create(self, request):
        return Response(request.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        return Response({})
