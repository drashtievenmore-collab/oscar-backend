"""HRMS serializers (api.md §11)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)

from . import services
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    AppraisalKpiScore,
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
    PayslipComponent,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    ResignationChecklistItem,
    SalaryAdvance,
    SalaryStructure,
    ScreeningAnswer,
    ScreeningQuestion,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
    WorkingDay,
)


# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class DepartmentSerializer(BaseModelSerializer):
    head = serializers.CharField(source="head_employee.name", read_only=True)
    headEmployeeId = TenantPrimaryKeyRelatedField(
        source="head_employee", model="hrms.Employee", required=False, allow_null=True
    )
    employees = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "head", "headEmployeeId", "parent", "status",
            "description", "employees", "teams", "created_at", "updated_at",
        ]

    def get_employees(self, department):
        return getattr(department, "employee_count", None)

    def get_teams(self, department):
        return getattr(department, "team_count", None)


class DesignationSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = Designation
        fields = ["id", "name", "level", "department", "departmentId", "created_at"]


class LocationSerializer(BaseModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "address", "timezone", "created_at"]


class EmployeeSerializer(BaseModelSerializer):
    employeeCode = serializers.CharField(source="employee_code", read_only=True)
    designation = serializers.CharField(source="designation.name", read_only=True)
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    manager = serializers.CharField(source="manager.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    location = serializers.CharField(source="location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    joining = serializers.DateField(source="joining_date", required=False)
    # Frontend posts `joiningDate` (hrmsSync.toApi); accept it as an alias so
    # a missing `joining` never 400s the create.
    joiningDate = serializers.DateField(write_only=True, required=False)
    employmentType = serializers.CharField(
        source="employment_type", required=False, allow_null=True
    )
    salaryStructureId = TenantPrimaryKeyRelatedField(
        source="salary_structure", model="hrms.SalaryStructure",
        required=False, allow_null=True,
    )
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)

    class Meta:
        model = Employee
        fields = [
            "id", "employeeCode", "name", "email", "phone", "avatar",
            "designation", "designationId", "department", "departmentId",
            "manager", "managerId", "location", "locationId", "joining", "joiningDate",
            "employmentType", "shift", "salaryStructureId", "standard_salary",
            "status", "date_of_birth", "gender", "blood_group", "personal_email",
            "emergency_contact", "address", "bank_account_number", "ifsc_code",
            "pan", "uan", "aadhaar_last4", "last_working_day",
            "termination_reason", "created_at", "updated_at",
        ]
        read_only_fields = ["employeeCode", "created_at", "updated_at"]

    def validate(self, attrs):
        from django.utils import timezone as dj_timezone
        manager = attrs.get("manager")
        if manager is not None and self.instance is not None:
            services.assert_no_manager_cycle(self.instance, manager.id)
        # `joiningDate` is the write alias for `joining_date`; default today.
        alias = attrs.pop("joiningDate", None)
        if attrs.get("joining_date") is None and alias is not None:
            attrs["joining_date"] = alias
        if attrs.get("joining_date") is None and self.instance is None:
            attrs["joining_date"] = dj_timezone.localdate()
        return attrs


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    date = serializers.DateField(source="work_date")
    checkIn = serializers.DateTimeField(source="check_in", required=False, allow_null=True)
    checkOut = serializers.DateTimeField(source="check_out", required=False, allow_null=True)

    class Meta:
        model = Attendance
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "department",
            "date", "checkIn", "checkOut", "hours", "status", "source",
            "remark", "leave_request", "created_at", "updated_at",
        ]
        # `hours` and the Late / Half Day verdict come from the flexibility
        # policy, applied server-side (api.md §11.2).
        read_only_fields = ["hours", "source", "leave_request", "created_at", "updated_at"]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        work_date = data.get("date") or data.get("work_date")
        if work_date:
            for time_field in ("checkIn", "check_in"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"
            for time_field in ("checkOut", "check_out"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"

        emp_id = data.get("employeeId") or data.get("employee_id")
        if emp_id and isinstance(emp_id, str):
            import uuid
            try:
                uuid.UUID(emp_id)
            except ValueError:
                from apps.hrms.models import Employee
                request = self.context.get("request")
                client_id = getattr(request, "client_id", None) or (request.user.client_id if request and hasattr(request, "user") else None)
                if client_id:
                    emp = Employee.objects.filter(
                        employee_code=emp_id, client_id=client_id, deleted_at__isnull=True
                    ).first()
                    if emp:
                        data["employeeId"] = str(emp.id)
        return super().to_internal_value(data)


class BulkAttendanceSerializer(BaseSerializer):
    date = serializers.DateField(required=False)
    records = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status = serializers.CharField(required=False, allow_null=True)


class RegularizationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    date = serializers.DateField(source="work_date")
    requestedCheckIn = serializers.DateTimeField(
        source="requested_check_in", required=False, allow_null=True
    )
    requestedCheckOut = serializers.DateTimeField(
        source="requested_check_out", required=False, allow_null=True
    )
    requestedStatus = serializers.CharField(
        source="requested_status", required=False, allow_null=True
    )

    class Meta:
        model = AttendanceRegularization
        fields = [
            "id", "employeeId", "employeeName", "date", "requestedCheckIn",
            "requestedCheckOut", "requestedStatus", "reason", "status",
            "approver", "decided_at", "remark", "created_at",
        ]
        read_only_fields = ["status", "approver", "decided_at", "created_at"]


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeSerializer(BaseModelSerializer):
    class Meta:
        model = LeaveType
        fields = [
            "id", "name", "code", "annual_entitlement", "accrual",
            "carry_forward_cap", "is_encashable", "is_paid", "created_at",
        ]


class LeaveRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    type = serializers.CharField(source="leave_type.name", read_only=True)
    leaveTypeId = TenantPrimaryKeyRelatedField(
        source="leave_type", model="hrms.LeaveType"
    )
    fromDate = serializers.DateField(source="from_date")
    toDate = serializers.DateField(source="to_date")
    delegateId = TenantPrimaryKeyRelatedField(
        source="delegate_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "type",
            "leaveTypeId", "fromDate", "toDate", "days", "reason", "delegateId",
            "delegate_confirmed_at", "status", "approver", "decided_at",
            "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "approver", "decided_at", "delegate_confirmed_at",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        start = attrs.get("from_date") or getattr(self.instance, "from_date", None)
        end = attrs.get("to_date") or getattr(self.instance, "to_date", None)
        if start and end and end < start:
            raise serializers.ValidationError({"toDate": ["Must be on or after the start date."]})
        return attrs


class LeaveBalanceSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    leaveType = serializers.CharField(source="leave_type.name", read_only=True)
    # GeneratedField (computed by Postgres) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    balance = serializers.DecimalField(
        max_digits=6, decimal_places=2, coerce_to_string=False, read_only=True
    )

    class Meta:
        model = LeaveBalance
        fields = [
            "id", "employee", "employeeName", "employeeCode", "leave_type",
            "leaveType", "period_year", "entitlement", "carried_forward",
            "used", "encashed", "balance",
        ]
        read_only_fields = ["balance", "used"]


class CompOffSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    workedDate = serializers.DateField(source="worked_date")
    workType = serializers.CharField(source="work_type", required=False, allow_null=True)
    expiryDate = serializers.DateField(source="expiry_date", required=False, allow_null=True)

    class Meta:
        model = CompOff
        fields = [
            "id", "employeeId", "workedDate", "workType", "days", "expiryDate",
            "used", "created_at",
        ]


class LeaveEncashmentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    leaveTypeId = TenantPrimaryKeyRelatedField(source="leave_type", model="hrms.LeaveType")
    perDayRate = MoneyField(source="per_day_rate")
    # GeneratedField (days * per_day_rate) -- see LeaveBalanceSerializer.balance.
    amount = MoneyField(read_only=True)

    class Meta:
        model = LeaveEncashment
        fields = [
            "id", "employeeId", "leaveTypeId", "days", "perDayRate", "amount",
            "status", "created_at",
        ]
        read_only_fields = ["amount"]


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class SalaryStructureSerializer(BaseModelSerializer):
    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "basic_pct", "hra_pct", "components", "is_active",
            "created_at", "updated_at",
        ]


class PayslipComponentSerializer(BaseModelSerializer):
    class Meta:
        model = PayslipComponent
        fields = ["id", "name", "kind", "amount"]


class PayslipSerializer(BaseModelSerializer):
    """The payroll row api.md §11.4 documents."""

    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    employeeId = serializers.CharField(source="employee_id", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    month = serializers.DateField(source="period_month", read_only=True)
    standardSalary = MoneyField(source="standard_salary", read_only=True)
    earnedSalary = MoneyField(source="earned_salary", read_only=True)
    additionalEarnings = MoneyField(source="additional_earnings", required=False)
    advance = MoneyField(source="advance_recovery", required=False)
    paidLeaves = serializers.DecimalField(
        source="paid_leaves", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    attendedDays = serializers.DecimalField(
        source="attended_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    totalDays = serializers.DecimalField(
        source="total_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    paidAmount = MoneyField(source="paid_amount", read_only=True)
    paymentDate = serializers.DateField(source="payment_date", read_only=True)
    bank = serializers.CharField(source="bank_account.name", read_only=True)
    components = PayslipComponentSerializer(many=True, read_only=True)
    netPayable = MoneyField(source="net_payable", read_only=True)

    class Meta:
        model = Payslip
        fields = [
            "id", "empId", "employeeId", "name", "role", "department", "month",
            "standardSalary", "earnedSalary", "additionalEarnings", "deductions",
            "advance", "paidLeaves", "basic", "hra", "allowances", "status",
            "paidAmount", "paymentDate", "bank", "attendedDays", "totalDays",
            "netPayable", "earned_salary_overridden", "components",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "earned_salary_overridden", "created_at", "updated_at"]


class PayrollRunSerializer(BaseModelSerializer):
    payslipCount = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = [
            "id", "period_month", "status", "processed_by", "processed_at",
            "approved_by", "approved_at", "payslipCount", "created_at",
        ]

    def get_payslipCount(self, run):
        return run.payslips.filter(deleted_at__isnull=True).count()


class ProcessPayrollSerializer(BaseSerializer):
    month = serializers.DateField()
    employeeIds = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class SalaryAdvanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    issuedOn = serializers.DateField(source="issued_on")
    recoveredAmount = MoneyField(source="recovered_amount", read_only=True)
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = SalaryAdvance
        fields = [
            "id", "employeeId", "employeeName", "amount", "issuedOn",
            "installments", "recoveredAmount", "outstanding", "status",
            "notes", "created_at",
        ]
        read_only_fields = ["status", "created_at"]

    def get_outstanding(self, advance):
        return advance.outstanding


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    applicantCount = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id", "title", "department", "departmentId", "designationId",
            "locationId", "openings", "experience_range", "salary_range",
            "employment_type", "description", "status", "is_published",
            "published_at", "slug", "applicantCount", "created_at", "updated_at",
        ]
        read_only_fields = ["published_at", "slug", "created_at", "updated_at"]

    def get_applicantCount(self, job):
        return getattr(job, "applicant_count", None)


class CandidateSerializer(BaseModelSerializer):
    resumeFileId = TenantPrimaryKeyRelatedField(
        source="resume_file", model="core.File", required=False, allow_null=True
    )

    class Meta:
        model = Candidate
        fields = [
            "id", "name", "email", "phone", "resumeFileId", "source",
            "current_ctc", "expected_ctc", "notice_period_days", "stage",
            "rating", "employee", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["employee", "created_at", "updated_at"]


class ApplicationSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")
    candidateName = serializers.CharField(source="candidate.name", read_only=True)
    jobId = TenantPrimaryKeyRelatedField(source="job", model="hrms.Job")
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = Application
        fields = [
            "id", "candidateId", "candidateName", "jobId", "jobTitle",
            "applied_at", "stage", "rejection_reason",
        ]


class InterviewSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    scheduledAt = serializers.DateTimeField(source="scheduled_at")
    panelUserIds = serializers.JSONField(source="panel_user_ids", required=False)

    class Meta:
        model = Interview
        fields = [
            "id", "applicationId", "candidateName", "round", "scheduledAt",
            "mode", "panelUserIds", "status", "rating", "notes",
            "recommendation", "created_at",
        ]


class OfferSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    offeredCtc = MoneyField(source="offered_ctc", required=False, allow_null=True)
    joiningDate = serializers.DateField(
        source="joining_date", required=False, allow_null=True
    )

    class Meta:
        model = Offer
        fields = [
            "id", "applicationId", "candidateName", "offeredCtc", "joiningDate",
            "status", "letter_file", "sent_at", "responded_at", "notes",
            "position", "department", "job_type", "location", "work_mode",
            "reporting_manager", "probation_period", "expiry_date", "created_at",
        ]
        read_only_fields = ["sent_at", "responded_at", "created_at"]


class OnboardingTaskSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")

    class Meta:
        model = OnboardingTask
        fields = ["id", "candidateId", "title", "owner", "due_date", "completed_at"]


class ScreeningQuestionSerializer(BaseModelSerializer):
    jobId = TenantPrimaryKeyRelatedField(
        source="job", model="hrms.Job", required=False, allow_null=True
    )

    class Meta:
        model = ScreeningQuestion
        fields = ["id", "jobId", "question", "type", "options", "is_active", "sort_order"]


# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleSerializer(BaseModelSerializer):
    class Meta:
        model = AppraisalCycle
        fields = [
            "id", "name", "period_start", "period_end", "status",
            "participants_scope", "created_at",
        ]


class PerformanceIndicatorSerializer(BaseModelSerializer):
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = PerformanceIndicator
        fields = ["id", "name", "category", "weight", "departmentId", "created_at"]


class KpiSerializer(BaseModelSerializer):
    indicatorId = TenantPrimaryKeyRelatedField(
        source="indicator", model="hrms.PerformanceIndicator",
        required=False, allow_null=True,
    )

    class Meta:
        model = Kpi
        fields = ["id", "indicatorId", "name", "target", "unit", "applies_to", "created_at"]


class AppraisalKpiScoreSerializer(BaseModelSerializer):
    kpiId = TenantPrimaryKeyRelatedField(source="kpi", model="hrms.Kpi")
    kpiName = serializers.CharField(source="kpi.name", read_only=True)

    class Meta:
        model = AppraisalKpiScore
        fields = ["id", "kpiId", "kpiName", "target", "achieved", "score", "weight"]


class AppraisalHistorySerializer(BaseModelSerializer):
    actorName = serializers.CharField(source="actor.name", read_only=True)

    class Meta:
        model = AppraisalHistory
        fields = [
            "id", "from_stage", "to_stage", "actor", "actorName", "action",
            "comment", "created_at",
        ]


class AppraisalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    cycleId = TenantPrimaryKeyRelatedField(source="cycle", model="hrms.AppraisalCycle")
    kpiScores = AppraisalKpiScoreSerializer(source="kpi_scores", many=True, read_only=True)
    history = AppraisalHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Appraisal
        fields = [
            "id", "cycleId", "employeeId", "employeeName", "managerId", "stage",
            "status", "self_rating", "manager_rating", "final_rating",
            "strengths", "areas_for_improvement", "development_feedback",
            "hr_comments", "self_comments", "kpiScores", "history",
            "created_at", "updated_at",
        ]
        read_only_fields = ["stage", "status", "created_at", "updated_at"]


class GoalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")

    class Meta:
        model = Goal
        fields = [
            "id", "employeeId", "cycle", "title", "description", "target_date",
            "progress_pct", "status", "created_at",
        ]


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerSerializer(BaseModelSerializer):
    class Meta:
        model = Trainer
        fields = [
            "id", "name", "kind", "employee", "organisation", "expertise",
            "rate", "contact", "created_at",
        ]


class TrainingParticipantSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = TrainingParticipant
        fields = [
            "id", "employeeId", "employeeName", "attendance_status", "rating",
            "feedback", "evaluated_at",
        ]


class TrainingSerializer(BaseModelSerializer):
    trainerId = TenantPrimaryKeyRelatedField(
        source="trainer", model="hrms.Trainer", required=False, allow_null=True
    )
    trainerName = serializers.CharField(source="trainer.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    participants = TrainingParticipantSerializer(many=True, read_only=True)

    class Meta:
        model = Training
        fields = [
            "id", "title", "description", "type", "trainerId", "trainerName",
            "departmentId", "start_date", "end_date", "venue", "cost", "stage",
            "participants", "created_at", "updated_at",
        ]


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetCategorySerializer(BaseModelSerializer):
    class Meta:
        model = AssetCategory
        fields = ["id", "name", "depreciation_pct", "default_warranty_months"]


class AssetAssignmentSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = AssetAssignment
        fields = [
            "id", "employee", "employeeName", "assigned_at", "assigned_by",
            "returned_at", "returned_condition", "notes",
        ]


class AssetSerializer(BaseModelSerializer):
    assetCode = serializers.CharField(source="asset_code", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    assignedTo = serializers.CharField(source="assigned_employee.name", read_only=True)
    employeeId = TenantPrimaryKeyRelatedField(
        source="assigned_employee", model="hrms.Employee", required=False, allow_null=True
    )
    dept = serializers.CharField(
        source="assigned_employee.department.name", read_only=True
    )
    serialNumber = serializers.CharField(
        source="serial_number", required=False, allow_null=True, allow_blank=True
    )
    purchaseDate = serializers.DateField(
        source="purchase_date", required=False, allow_null=True
    )
    purchaseCost = MoneyField(source="purchase_cost", required=False, allow_null=True)
    warrantyExpiry = serializers.DateField(
        source="warranty_expiry", required=False, allow_null=True
    )
    history = AssetAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id", "assetCode", "name", "category", "categoryId", "serialNumber",
            "assignedTo", "employeeId", "dept", "status", "condition",
            "purchaseDate", "purchaseCost", "warrantyExpiry", "location",
            "notes", "history", "created_at", "updated_at",
        ]
        read_only_fields = ["assetCode", "created_at", "updated_at"]


class AssetRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )

    class Meta:
        model = AssetRequest
        fields = [
            "id", "employeeId", "employeeName", "categoryId", "justification",
            "status", "approver", "fulfilled_asset", "created_at",
        ]


class HrDocumentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employee = serializers.CharField(source="employee.name", read_only=True)
    fileId = TenantPrimaryKeyRelatedField(
        source="file", model="core.File", required=False, allow_null=True
    )
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    fileType = serializers.CharField(source="file.content_type", read_only=True)
    expiry = serializers.DateField(source="valid_until", required=False, allow_null=True)
    #: DERIVED from ``valid_until`` at read time (api.md §11.8).
    status = serializers.SerializerMethodField()
    uploadedBy = serializers.CharField(source="created_by.name", read_only=True)
    updatedOn = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = HrDocument
        fields = [
            "id", "title", "category", "employee", "employeeId", "version",
            "expiry", "status", "fileId", "fileSize", "fileType", "uploadedBy",
            "updatedOn", "tags", "description", "valid_from", "is_confidential",
            "created_at",
        ]

    def get_status(self, document):
        return services.document_status(document)


class PolicyVersionSerializer(BaseModelSerializer):
    changedByName = serializers.CharField(source="changed_by.name", read_only=True)

    class Meta:
        model = PolicyVersion
        fields = ["id", "version", "change_note", "changedByName", "created_at"]


class PolicySerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.PolicyCategory", required=False, allow_null=True
    )
    ownerDept = serializers.CharField(source="owner_department.name", read_only=True)
    ownerDepartmentId = TenantPrimaryKeyRelatedField(
        source="owner_department", model="hrms.Department",
        required=False, allow_null=True,
    )
    applicableTo = serializers.CharField(
        source="applicable_to", required=False, allow_null=True, allow_blank=True
    )
    effectiveDate = serializers.DateField(
        source="effective_date", required=False, allow_null=True
    )
    reviewDate = serializers.DateField(
        source="review_date", required=False, allow_null=True
    )
    approvalRequired = serializers.BooleanField(source="approval_required", required=False)
    ackRequired = serializers.BooleanField(source="ack_required", required=False)
    versionHistory = PolicyVersionSerializer(
        source="version_history", many=True, read_only=True
    )

    class Meta:
        model = Policy
        fields = [
            "id", "name", "category", "categoryId", "ownerDept",
            "ownerDepartmentId", "applicableTo", "version", "effectiveDate",
            "reviewDate", "approvalRequired", "ackRequired", "ack_window_days",
            "status", "summary", "body", "file", "versionHistory",
            "approved_by", "approved_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "version", "status", "approved_by", "approved_at", "created_at", "updated_at",
        ]


class PolicyAcknowledgementSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = PolicyAcknowledgement
        fields = [
            "id", "employee", "employeeName", "employeeCode", "policy_version",
            "acknowledged_at", "status",
        ]

    def get_status(self, acknowledgement):
        policy = self.context.get("policy") or acknowledgement.policy
        return services.acknowledgement_status(acknowledgement, policy)


class PolicyCategorySerializer(BaseModelSerializer):
    class Meta:
        model = PolicyCategory
        fields = ["id", "name", "description"]


class CalendarEventSerializer(BaseModelSerializer):
    startDate = serializers.DateTimeField(source="starts_at")
    endDate = serializers.DateTimeField(source="ends_at", required=False, allow_null=True)
    date = serializers.SerializerMethodField()
    dept = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    virtualLink = serializers.CharField(
        source="virtual_link", required=False, allow_null=True, allow_blank=True
    )
    #: ``time`` is a display string the server derives (api.md §11.8).
    time = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "title", "date", "startDate", "endDate", "type", "time",
            "location", "dept", "departmentId", "organizer", "description",
            "virtualLink", "all_day", "source_type", "source_id", "created_at",
        ]
        read_only_fields = ["source_type", "source_id", "created_at"]

    def get_date(self, event):
        return event.starts_at.date() if event.starts_at else None

    def get_time(self, event):
        return services.calendar_time_label(event)


class HolidaySerializer(BaseModelSerializer):
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )

    class Meta:
        model = Holiday
        fields = [
            "id", "date", "name", "locationId", "is_optional", "applies_to",
            "holiday_type", "status", "created_at",
        ]


class WorkingDaySerializer(BaseModelSerializer):
    class Meta:
        model = WorkingDay
        fields = ["id", "weekday", "is_working", "shift_start", "shift_end", "location"]


class TeamSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    leadName = serializers.CharField(source="lead.name", read_only=True)
    memberCount = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            "id", "name", "department", "departmentId", "lead", "leadName",
            "members", "memberCount", "description", "status", "created_at",
        ]

    def get_memberCount(self, team):
        return team.members.count()


class ApprovalChainSerializer(BaseModelSerializer):
    class Meta:
        model = ApprovalChain
        fields = ["id", "name", "applies_to", "steps", "status", "created_at"]


class TerminationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )

    class Meta:
        model = Termination
        fields = [
            "id", "employeeId", "employeeName", "reason", "lastWorkingDay",
            "letter_file", "settlement_amount", "status", "created_at",
        ]


class ResignationChecklistItemSerializer(BaseModelSerializer):
    class Meta:
        model = ResignationChecklistItem
        fields = ["id", "title", "owner", "completed_at"]


class ResignationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    submittedOn = serializers.DateField(source="submitted_on")
    noticePeriodDays = serializers.IntegerField(source="notice_period_days", required=False)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    checklist = ResignationChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Resignation
        fields = [
            "id", "employeeId", "employeeName", "submittedOn", "noticePeriodDays",
            "lastWorkingDay", "exit_interview_at", "reason", "handover_to", "status",
            "checklist", "created_at",
        ]


class ComplaintSerializer(BaseModelSerializer):
    """``is_anonymous`` is honoured by nulling the raiser in every serialiser --
    including exports (db.md §11.8)."""

    raisedBy = serializers.SerializerMethodField()
    raisedByEmployeeId = TenantPrimaryKeyRelatedField(
        source="raised_by_employee", model="hrms.Employee",
        required=False, allow_null=True, write_only=True,
    )
    againstEmployeeId = TenantPrimaryKeyRelatedField(
        source="against_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    againstName = serializers.SerializerMethodField()
    isAnonymous = serializers.BooleanField(source="is_anonymous", required=False)

    class Meta:
        model = Complaint
        fields = [
            "id", "raisedBy", "raisedByEmployeeId", "againstEmployeeId",
            "againstName", "category", "description", "isAnonymous", "status",
            "assigned_to", "resolution", "resolved_at", "created_at",
        ]
        read_only_fields = ["resolved_at", "created_at"]

    def get_raisedBy(self, complaint):
        if complaint.is_anonymous:
            return None
        return complaint.raised_by_employee.name if complaint.raised_by_employee_id else None

    def get_againstName(self, complaint):
        return complaint.against_employee.name if complaint.against_employee_id else None
