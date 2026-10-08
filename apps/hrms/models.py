"""
HRMS (db.md §11, api.md §11).

Replaces eleven Zustand stores. The tables are numerous but mostly flat; the
interesting constraints are in attendance, leave and payroll, which interlock:

  - one attendance row per employee per day is the load-bearing constraint of
    the whole module (db.md §11.2)
  - leave approval posts attendance and decrements the balance in one
    transaction (api.md §11.3)
  - the payslip is the authoritative artefact, so its numbers are frozen and
    never recomputed from a later-edited attendance record (db.md §11.4)
"""
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q, F, Value

from apps.core.models import LegacyIdMixin, TenantModel

EMPLOYEE_STATUSES = [
    ("Active", "Active"),
    ("On Leave", "On Leave"),
    ("Probation", "Probation"),
    ("Resigned", "Resigned"),
    ("Terminated", "Terminated"),
]
EMPLOYMENT_TYPES = [
    ("Full-time", "Full-time"),
    ("Part-time", "Part-time"),
    ("Contract", "Contract"),
    ("Intern", "Intern"),
]
ATTENDANCE_STATUSES = [
    ("Present", "Present"),
    ("Absent", "Absent"),
    ("Late", "Late"),
    ("Half Day", "Half Day"),
    ("WFH", "WFH"),
    ("On Leave", "On Leave"),
    ("Holiday", "Holiday"),
    ("Week Off", "Week Off"),
]
LEAVE_STATUSES = [
    ("Pending Review", "Pending Review"),
    ("Delegate Confirmed", "Delegate Confirmed"),
    ("Approved", "Approved"),
    ("Rejected", "Rejected"),
    ("Cancelled", "Cancelled"),
]
#: api.md §11.4 -- not a Draft/Approved/Paid trio; the UI filters on these four.
PAYROLL_STATUSES = [
    ("In Progress", "In Progress"),
    ("Ready for Review", "Ready for Review"),
    ("Approved", "Approved"),
    ("Paid", "Paid"),
]
CANDIDATE_STAGES = [
    ("Applied", "Applied"),
    ("Screening", "Screening"),
    ("Interview", "Interview"),
    ("Offer", "Offer"),
    ("Hired", "Hired"),
    ("Rejected", "Rejected"),
]
OFFER_STATUSES = [
    ("Draft", "Draft"),
    ("Sent", "Sent"),
    ("Accepted", "Accepted"),
    ("Declined", "Declined"),
    ("Revoked", "Revoked"),
]
APPRAISAL_STAGES = [
    ("Self Review", "Self Review"),
    ("Manager Review", "Manager Review"),
    ("HR Review", "HR Review"),
    ("Finalization", "Finalization"),
]
APPRAISAL_STATUSES = [
    ("Draft", "Draft"),
    ("Submitted", "Submitted"),
    ("In Progress", "In Progress"),
    ("Returned", "Returned"),
    ("Approved", "Approved"),
    ("Completed", "Completed"),
]
TRAINING_STAGES = [
    ("Requested", "Requested"),
    ("Trainer Assigned", "Trainer Assigned"),
    ("Scheduled", "Scheduled"),
    ("Ongoing", "Ongoing"),
    ("Completed", "Completed"),
    ("Evaluated", "Evaluated"),
    ("Cancelled", "Cancelled"),
]
ASSET_STATUSES = [
    ("Available", "Available"),
    ("Assigned", "Assigned"),
    ("Under Maintenance", "Under Maintenance"),
    ("Lost/Damaged", "Lost/Damaged"),
    ("Under Repair", "Under Repair"),
    ("Retired", "Retired"),
]
ASSET_CONDITIONS = [
    ("Excellent", "Excellent"),
    ("Good", "Good"),
    ("Needs Repair", "Needs Repair"),
]
POLICY_STATUSES = [
    ("Draft", "Draft"),
    ("Pending Approval", "Pending Approval"),
    ("Approved", "Approved"),
    ("Active", "Active"),
    ("Review - Update", "Review - Update"),
    ("Archived", "Archived"),
]
CALENDAR_EVENT_TYPES = [
    ("Meeting", "Meeting"),
    ("Leave", "Leave"),
    ("Holiday", "Holiday"),
    ("Training", "Training"),
    ("Company", "Company"),
    ("Fun", "Fun"),
    ("HRMilestone", "HRMilestone"),
    ("Interview", "Interview"),
    ("Review", "Review"),
]


# ---------------------------------------------------------------------------
# db.md §11.1 -- Employees and organisation
# ---------------------------------------------------------------------------
class Department(TenantModel, LegacyIdMixin):
    """Separate from ``pms_departments`` on purpose (db.md §1.8): HR departments
    carry a head, a parent and headcount; PMS departments carry a colour and a
    capacity. Same word, different entity."""

    name = models.TextField()
    code = models.TextField(null=True, blank=True)
    head_employee = models.ForeignKey(
        "Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    status = models.TextField(default="Active")
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_departments"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_hrms_department_name",
            )
        ]

    def __str__(self):
        return self.name


class Designation(TenantModel, LegacyIdMixin):
    name = models.TextField()
    level = models.SmallIntegerField(null=True, blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="designations"
    )

    class Meta:
        db_table = "hrms_designations"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_hrms_designation",
            )
        ]

    def __str__(self):
        return self.name


class Location(TenantModel, LegacyIdMixin):
    name = models.TextField()
    address = models.JSONField(default=dict, blank=True)
    timezone = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_locations"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_hrms_location",
            )
        ]

    def __str__(self):
        return self.name


class Employee(TenantModel, LegacyIdMixin):
    """db.md §11.1.

    Aadhaar is stored as last-4 only. Full government identifiers do not belong
    in this schema without an encryption-at-rest design and a retention policy.
    """

    employee_code = models.TextField()
    name = models.TextField()
    email = models.EmailField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    avatar_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    avatar_url = models.TextField(null=True, blank=True)
    designation = models.ForeignKey(
        Designation, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports"
    )
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    joining_date = models.DateField()
    employment_type = models.TextField(choices=EMPLOYMENT_TYPES, null=True, blank=True)
    shift = models.TextField(default="General")  # General | Flexible | Night
    salary_structure = models.ForeignKey(
        "SalaryStructure", null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    standard_salary = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(choices=EMPLOYEE_STATUSES, default="Active")

    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.TextField(null=True, blank=True)
    blood_group = models.TextField(null=True, blank=True)
    personal_email = models.EmailField(null=True, blank=True)
    emergency_contact = models.JSONField(default=dict, blank=True)
    address = models.JSONField(default=dict, blank=True)

    bank_account_number = models.TextField(null=True, blank=True)
    ifsc_code = models.TextField(null=True, blank=True)
    pan = models.TextField(null=True, blank=True)
    uan = models.TextField(null=True, blank=True)
    aadhaar_last4 = models.CharField(max_length=4, null=True, blank=True)

    last_working_day = models.DateField(null=True, blank=True)
    termination_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_employees"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "employee_code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_hrms_employee_code",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "department"],
                name="ix_hrms_employees_dept",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(fields=["client", "manager"], name="ix_hrms_employees_mgr"),
        ]

    def __str__(self):
        return f"{self.employee_code} {self.name}"


# ---------------------------------------------------------------------------
# db.md §11.2 -- Attendance
# ---------------------------------------------------------------------------
class Attendance(TenantModel):
    SOURCES = [
        ("manual", "manual"),
        ("bulk", "bulk"),
        ("biometric", "biometric"),
        ("regularization", "regularization"),
        ("leave", "leave"),
        ("holiday", "holiday"),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="attendance")
    work_date = models.DateField()
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    #: DERIVED from check-in/out plus the attendance_flexibility policy.
    hours = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    status = models.TextField(choices=ATTENDANCE_STATUSES)
    leave_request = models.ForeignKey(
        "LeaveRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="attendance_rows"
    )
    source = models.TextField(choices=SOURCES, default="manual")
    remark = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_attendance"
        ordering = ["-work_date"]
        constraints = [
            # The load-bearing constraint of the whole module: payroll
            # proration, leave posting and the monthly summary all assume
            # exactly one row per employee per day (db.md §11.2).
            models.UniqueConstraint(
                fields=["client", "employee", "work_date"], name="uq_hrms_attendance"
            )
        ]
        indexes = [models.Index(fields=["client", "work_date"], name="ix_hrms_attendance_date")]


class AttendanceRegularization(TenantModel):
    STATUSES = [("Pending", "Pending"), ("Approved", "Approved"), ("Rejected", "Rejected")]

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="regularizations"
    )
    work_date = models.DateField()
    requested_check_in = models.DateTimeField(null=True, blank=True)
    requested_check_out = models.DateTimeField(null=True, blank=True)
    requested_status = models.TextField(null=True, blank=True)
    reason = models.TextField()
    status = models.TextField(choices=STATUSES, default="Pending")
    approver = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    remark = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_attendance_regularizations"
        ordering = ["-work_date"]


# ---------------------------------------------------------------------------
# db.md §11.3 -- Leave
# ---------------------------------------------------------------------------
class LeaveType(TenantModel, LegacyIdMixin):
    ACCRUALS = [
        ("Yearly", "Yearly"),
        ("Monthly", "Monthly"),
        ("Quarterly", "Quarterly"),
        ("None", "None"),
    ]

    name = models.TextField()
    code = models.TextField(null=True, blank=True)
    annual_entitlement = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    accrual = models.TextField(choices=ACCRUALS, default="Yearly")
    carry_forward_cap = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    is_encashable = models.BooleanField(default=False)
    is_paid = models.BooleanField(default=True)

    class Meta:
        db_table = "hrms_leave_types"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_leave_type",
            )
        ]

    def __str__(self):
        return self.name


class LeaveRequest(TenantModel, LegacyIdMixin):
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_requests"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="requests")
    from_date = models.DateField()
    to_date = models.DateField()
    days = models.DecimalField(max_digits=6, decimal_places=2, default=0)  # half-days allowed
    reason = models.TextField(null=True, blank=True)
    delegate_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="delegated_leaves"
    )
    delegate_confirmed_at = models.DateTimeField(null=True, blank=True)
    status = models.TextField(choices=LEAVE_STATUSES, default="Pending Review")
    approver = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    remark = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_leave_requests"
        ordering = ["-from_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(to_date__gte=models.F("from_date")), name="ck_leave_dates"
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "employee", "from_date"], name="ix_leave_requests_emp"
            )
        ]


class LeaveBalance(TenantModel):
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_balances"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name="balances")
    period_year = models.IntegerField()
    entitlement = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    carried_forward = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    #: DERIVED from approved requests.
    used = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    encashed = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    balance = models.GeneratedField(
        expression=F("entitlement") + F("carried_forward") - F("used") - F("encashed"),
        output_field=models.DecimalField(max_digits=6, decimal_places=2),
        db_persist=True,
    )

    class Meta:
        db_table = "hrms_leave_balances"
        constraints = [
            models.UniqueConstraint(
                fields=["client", "employee", "leave_type", "period_year"],
                name="uq_hrms_leave_balance",
            )
        ]


class CompOff(TenantModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="comp_offs")
    worked_date = models.DateField()
    work_type = models.TextField(null=True, blank=True)
    days = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    expiry_date = models.DateField(null=True, blank=True)
    used = models.BooleanField(default=False)
    used_leave_request = models.ForeignKey(
        LeaveRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "hrms_comp_offs"
        ordering = ["-worked_date"]


class LeaveEncashment(TenantModel):
    STATUSES = [
        ("Pending", "Pending"),
        ("Approved", "Approved"),
        ("Rejected", "Rejected"),
        ("Paid", "Paid"),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="encashments")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="+")
    days = models.DecimalField(max_digits=6, decimal_places=2)
    per_day_rate = models.DecimalField(max_digits=18, decimal_places=2)
    amount = models.GeneratedField(
        expression=F("days") * F("per_day_rate"),
        output_field=models.DecimalField(max_digits=18, decimal_places=2),
        db_persist=True,
    )
    status = models.TextField(choices=STATUSES, default="Pending")
    payslip = models.ForeignKey(
        "Payslip", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "hrms_leave_encashments"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# db.md §11.4 -- Payroll
# ---------------------------------------------------------------------------
class SalaryStructure(TenantModel, LegacyIdMixin):
    name = models.TextField()
    basic_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    hra_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    components = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "hrms_salary_structures"
        ordering = ["name"]

    def __str__(self):
        return self.name


class PayrollRun(TenantModel):
    period_month = models.DateField()  # first of month
    status = models.TextField(choices=PAYROLL_STATUSES, default="In Progress")
    processed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_payroll_runs"
        ordering = ["-period_month"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "period_month"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_payroll_run",
            )
        ]


class Payslip(TenantModel, LegacyIdMixin):
    """The authoritative artefact: the numbers are frozen here and never
    recomputed from a later-edited attendance record (db.md §11.4)."""

    payroll_run = models.ForeignKey(
        PayrollRun, on_delete=models.CASCADE, related_name="payslips"
    )
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payslips")
    period_month = models.DateField()
    standard_salary = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    attended_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    paid_leaves = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    #: api.md §11.4 -- totalDays defaults to 24.
    total_days = models.DecimalField(max_digits=6, decimal_places=2, default=24)
    #: DERIVED, with an explicit override kept as an escape hatch for manual
    #: adjustments before approval (api.md §11.4) -- the override is logged.
    earned_salary = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    earned_salary_overridden = models.BooleanField(default=False)
    basic = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    hra = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    allowances = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    additional_earnings = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    advance_recovery = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_payable = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(choices=PAYROLL_STATUSES, default="In Progress")
    paid_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    payment_date = models.DateField(null=True, blank=True)
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "hrms_payslips"
        ordering = ["-period_month"]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll_run", "employee"], name="uq_payslip"
            )
        ]


class PayslipComponent(TenantModel):
    """Exists so a payslip PDF can list per-component lines without parsing a
    blob; the aggregate columns on the payslip stay for list-view speed."""

    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="components")
    name = models.TextField()
    kind = models.TextField(choices=[("earning", "earning"), ("deduction", "deduction")])
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = "hrms_payslip_components"


class SalaryAdvance(TenantModel):
    STATUSES = [
        ("Active", "Active"),
        ("Recovered", "Recovered"),
        ("Written Off", "Written Off"),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="advances")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    issued_on = models.DateField()
    installments = models.IntegerField(default=1)
    #: DERIVED from payslips.
    recovered_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(choices=STATUSES, default="Active")
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_salary_advances"
        ordering = ["-issued_on"]

    @property
    def outstanding(self):
        return (self.amount or 0) - (self.recovered_amount or 0)


# ---------------------------------------------------------------------------
# db.md §11.5 -- Recruitment
# ---------------------------------------------------------------------------
class Job(TenantModel, LegacyIdMixin):
    STATUSES = [("Open", "Open"), ("On Hold", "On Hold"), ("Closed", "Closed")]

    title = models.TextField()
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    designation = models.ForeignKey(
        Designation, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )
    openings = models.IntegerField(default=1)
    experience_range = models.TextField(null=True, blank=True)
    salary_range = models.TextField(null=True, blank=True)
    employment_type = models.TextField(choices=EMPLOYMENT_TYPES, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Open")
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    #: The public career-portal URL key (api.md §11.5).
    slug = models.SlugField(max_length=160, null=True, blank=True)

    class Meta:
        db_table = "hrms_jobs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "slug"],
                condition=models.Q(slug__isnull=False, deleted_at__isnull=True),
                name="uq_hrms_job_slug",
            )
        ]

    def __str__(self):
        return self.title


class Candidate(TenantModel, LegacyIdMixin):
    name = models.TextField()
    email = models.EmailField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    resume_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source = models.TextField(null=True, blank=True)  # 'careers_portal' for public applies
    current_ctc = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    expected_ctc = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    notice_period_days = models.IntegerField(null=True, blank=True)
    stage = models.TextField(choices=CANDIDATE_STAGES, default="Applied")
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    #: The hiring trail must survive the conversion to employee (db.md §11.5).
    employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_candidates"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Application(TenantModel):
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="applications"
    )
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    applied_at = models.DateTimeField(auto_now_add=True)
    stage = models.TextField(choices=CANDIDATE_STAGES, default="Applied")
    rejection_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_applications"
        ordering = ["-applied_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "job"], name="uq_hrms_application"
            )
        ]


class Interview(TenantModel):
    STATUSES = [
        ("Scheduled", "Scheduled"),
        ("Completed", "Completed"),
        ("Cancelled", "Cancelled"),
        ("No Show", "No Show"),
    ]

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="interviews"
    )
    round = models.IntegerField(default=1)
    scheduled_at = models.DateTimeField()
    mode = models.TextField(null=True, blank=True)
    panel_user_ids = models.JSONField(default=list, blank=True)
    status = models.TextField(choices=STATUSES, default="Scheduled")
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    recommendation = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_interviews"
        ordering = ["scheduled_at"]


class Offer(TenantModel):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="offers")
    offered_ctc = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    joining_date = models.DateField(null=True, blank=True)
    status = models.TextField(choices=OFFER_STATUSES, default="Draft")
    letter_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    #: Offer-letter fields as typed in HR (position, work mode, reporting
    #: manager …) so the letter survives refresh and other devices.
    position = models.TextField(null=True, blank=True)
    department = models.TextField(null=True, blank=True)
    job_type = models.TextField(null=True, blank=True)
    location = models.TextField(null=True, blank=True)
    work_mode = models.TextField(null=True, blank=True)
    reporting_manager = models.TextField(null=True, blank=True)
    probation_period = models.TextField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "hrms_offers"
        ordering = ["-created_at"]


class OnboardingTask(TenantModel):
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="onboarding_tasks"
    )
    title = models.TextField()
    owner = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_onboarding_tasks"
        ordering = ["due_date"]


class ScreeningQuestion(TenantModel):
    job = models.ForeignKey(
        Job, null=True, blank=True, on_delete=models.CASCADE, related_name="questions"
    )  # null = global
    question = models.TextField()
    type = models.TextField(default="text")
    options = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "hrms_screening_questions"
        ordering = ["sort_order"]


class ScreeningAnswer(TenantModel):
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="screening_answers"
    )
    question = models.ForeignKey(
        ScreeningQuestion, on_delete=models.CASCADE, related_name="answers"
    )
    answer = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_screening_answers"


# ---------------------------------------------------------------------------
# db.md §11.6 -- Performance
# ---------------------------------------------------------------------------
class AppraisalCycle(TenantModel):
    name = models.TextField()
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.TextField(default="Draft")
    participants_scope = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "hrms_appraisal_cycles"
        ordering = ["-period_start"]


class PerformanceIndicator(TenantModel):
    name = models.TextField()
    category = models.TextField(null=True, blank=True)  # Technical | Organisational
    weight = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "hrms_performance_indicators"
        ordering = ["name"]


class Kpi(TenantModel):
    indicator = models.ForeignKey(
        PerformanceIndicator, null=True, blank=True, on_delete=models.SET_NULL, related_name="kpis"
    )
    name = models.TextField()
    target = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    unit = models.TextField(null=True, blank=True)
    applies_to = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_kpis"
        ordering = ["name"]


class Appraisal(TenantModel):
    cycle = models.ForeignKey(
        AppraisalCycle, on_delete=models.CASCADE, related_name="appraisals"
    )
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="appraisals")
    manager = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_appraisals"
    )
    stage = models.TextField(choices=APPRAISAL_STAGES, default="Self Review")
    status = models.TextField(choices=APPRAISAL_STATUSES, default="Draft")
    self_rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    manager_rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    final_rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    strengths = models.TextField(null=True, blank=True)
    areas_for_improvement = models.TextField(null=True, blank=True)
    development_feedback = models.TextField(null=True, blank=True)
    hr_comments = models.TextField(null=True, blank=True)
    self_comments = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_appraisals"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["cycle", "employee"], name="uq_hrms_appraisal")
        ]


class AppraisalKpiScore(TenantModel):
    appraisal = models.ForeignKey(Appraisal, on_delete=models.CASCADE, related_name="kpi_scores")
    kpi = models.ForeignKey(Kpi, on_delete=models.CASCADE, related_name="+")
    target = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    achieved = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "hrms_appraisal_kpi_scores"


class AppraisalHistory(TenantModel):
    """Append-only. Every stage transition writes here (api.md §11.6); the
    table records who actually did it, the service role-gates who may."""

    appraisal = models.ForeignKey(Appraisal, on_delete=models.CASCADE, related_name="history")
    from_stage = models.TextField(null=True, blank=True)
    to_stage = models.TextField(null=True, blank=True)
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.TextField()
    comment = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_appraisal_history"
        ordering = ["created_at"]


class Goal(TenantModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="goals")
    cycle = models.ForeignKey(
        AppraisalCycle, null=True, blank=True, on_delete=models.SET_NULL, related_name="goals"
    )
    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    progress_pct = models.SmallIntegerField(default=0)
    status = models.TextField(default="In Progress")

    class Meta:
        db_table = "hrms_goals"
        ordering = ["target_date"]


# ---------------------------------------------------------------------------
# db.md §11.7 -- Training
# ---------------------------------------------------------------------------
class Trainer(TenantModel):
    KINDS = [("Internal", "Internal"), ("External", "External")]

    name = models.TextField()
    kind = models.TextField(choices=KINDS, default="Internal")
    employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    organisation = models.TextField(null=True, blank=True)
    expertise = models.TextField(null=True, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    contact = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_trainers"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Training(TenantModel, LegacyIdMixin):
    TYPES = [("Internal", "Internal"), ("External", "External")]

    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    type = models.TextField(choices=TYPES, default="Internal")
    trainer = models.ForeignKey(
        Trainer, null=True, blank=True, on_delete=models.SET_NULL, related_name="trainings"
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="trainings"
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    venue = models.TextField(null=True, blank=True)
    cost = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    stage = models.TextField(choices=TRAINING_STAGES, default="Requested")

    class Meta:
        db_table = "hrms_trainings"
        ordering = ["-start_date"]

    def __str__(self):
        return self.title


class TrainingParticipant(TenantModel):
    training = models.ForeignKey(
        Training, on_delete=models.CASCADE, related_name="participants"
    )
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="trainings")
    attendance_status = models.TextField(null=True, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    feedback = models.TextField(null=True, blank=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_training_participants"
        constraints = [
            models.UniqueConstraint(
                fields=["training", "employee"], name="uq_hrms_training_participant"
            )
        ]


# ---------------------------------------------------------------------------
# db.md §11.8 -- Assets, documents, policies, calendar, HR admin
# ---------------------------------------------------------------------------
class AssetCategory(TenantModel):
    name = models.TextField()
    depreciation_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    default_warranty_months = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "hrms_asset_categories"
        ordering = ["name"]


class Asset(TenantModel, LegacyIdMixin):
    asset_code = models.TextField()
    name = models.TextField()
    category = models.ForeignKey(
        AssetCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets"
    )
    serial_number = models.TextField(null=True, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets"
    )
    assigned_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets"
    )
    condition = models.TextField(choices=ASSET_CONDITIONS, null=True, blank=True)
    status = models.TextField(choices=ASSET_STATUSES, default="Available")
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_assets"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "asset_code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_hrms_asset_code",
            )
        ]

    def __str__(self):
        return f"{self.asset_code} {self.name}"


class AssetAssignment(TenantModel):
    """The ``history[]`` the UI appends to -- append-only.

    The partial unique index is the physical statement of "an asset is assigned
    to at most one person at a time", which a nested array cannot enforce.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="history")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_condition = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_asset_assignments"
        ordering = ["-assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset"],
                condition=models.Q(returned_at__isnull=True),
                name="uq_asset_active_assignment",
            )
        ]


class AssetRequest(TenantModel):
    STATUSES = [("Pending", "Pending"), ("Approved", "Approved"), ("Fulfilled", "Fulfilled"),
                ("Rejected", "Rejected")]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="asset_requests")
    category = models.ForeignKey(
        AssetCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    justification = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Pending")
    approver = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    fulfilled_asset = models.ForeignKey(
        Asset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "hrms_asset_requests"
        ordering = ["-created_at"]


class HrDocument(TenantModel, LegacyIdMixin):
    """``status`` (``Valid | Expiring Soon | Expired``) is **derived** from
    ``valid_until`` at read time (api.md §11.8); ``employee = null`` marks a
    company-wide document (the frontend's ``employeeId: "ALL"``)."""

    employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.CASCADE, related_name="documents"
    )
    category = models.TextField(null=True, blank=True)
    title = models.TextField()
    file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    version = models.IntegerField(default=1)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    is_confidential = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)
    description = models.TextField(null=True, blank=True)
    expiring_soon_days = models.IntegerField(default=30)

    class Meta:
        db_table = "hrms_documents"
        ordering = ["-created_at"]


class PolicyCategory(TenantModel):
    name = models.TextField()
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_policy_categories"
        ordering = ["name"]


class Policy(TenantModel, LegacyIdMixin):
    name = models.TextField()
    category = models.ForeignKey(
        PolicyCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="policies"
    )
    owner_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    applicable_to = models.TextField(null=True, blank=True)
    version = models.IntegerField(default=1)
    body = models.TextField(null=True, blank=True)
    summary = models.TextField(null=True, blank=True)
    file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.TextField(choices=POLICY_STATUSES, default="Draft")
    effective_date = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)
    approval_required = models.BooleanField(default=True)
    ack_required = models.BooleanField(default=False)
    ack_window_days = models.IntegerField(default=14)
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_policies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class PolicyVersion(TenantModel):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="version_history")
    version = models.IntegerField()
    body = models.TextField(null=True, blank=True)
    file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    change_note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "hrms_policy_versions"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["policy", "version"], name="uq_hrms_policy_version")
        ]


class PolicyAcknowledgement(TenantModel):
    policy = models.ForeignKey(
        Policy, on_delete=models.CASCADE, related_name="acknowledgements"
    )
    policy_version = models.IntegerField()
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="policy_acks")
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_policy_acknowledgements"
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "policy_version", "employee"],
                name="uq_hrms_policy_ack",
            )
        ]


class CalendarEvent(TenantModel, LegacyIdMixin):
    """Leave-derived events are **generated**, not hand-entered (api.md §11.8):
    an approved leave writes rows with ``source_type = 'LeaveRequest'`` so
    cancelling the leave can remove exactly those rows."""

    title = models.TextField()
    type = models.TextField(choices=CALENDAR_EVENT_TYPES, default="Meeting")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    location = models.TextField(null=True, blank=True)
    organizer = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    virtual_link = models.TextField(null=True, blank=True)
    source_type = models.TextField(null=True, blank=True)
    source_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "hrms_calendar_events"
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["client", "starts_at"], name="ix_hrms_calendar_start"),
            models.Index(
                fields=["source_type", "source_id"], name="ix_hrms_calendar_source"
            ),
        ]


class Holiday(TenantModel, LegacyIdMixin):
    date = models.DateField()
    name = models.TextField()
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.CASCADE, related_name="holidays"
    )
    is_optional = models.BooleanField(default=False)
    #: Free-text scope shown on the HRMS calendar (e.g. "All Locations").
    applies_to = models.TextField(null=True, blank=True)
    #: National Gazetted / Festival / Optional …
    holiday_type = models.TextField(null=True, blank=True)
    status = models.TextField(default="Upcoming")

    class Meta:
        db_table = "hrms_holidays"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "date", "location"],
                condition=models.Q(location__isnull=False),
                name="uq_hrms_holiday_located",
            ),
            models.UniqueConstraint(
                fields=["client", "date"],
                condition=models.Q(location__isnull=True),
                name="uq_hrms_holiday_global",
            ),
        ]


class WorkingDay(TenantModel):
    weekday = models.SmallIntegerField()  # 0 = Monday
    is_working = models.BooleanField(default=True)
    shift_start = models.TimeField(null=True, blank=True)
    shift_end = models.TimeField(null=True, blank=True)
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.CASCADE, related_name="working_days"
    )

    class Meta:
        db_table = "hrms_working_days"
        ordering = ["weekday"]


class Team(TenantModel):
    """HR Admin -> Teams tab (api.md §11.8)."""

    name = models.TextField()
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams"
    )
    lead = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="led_teams"
    )
    members = models.ManyToManyField(Employee, blank=True, related_name="teams")
    description = models.TextField(null=True, blank=True)
    status = models.TextField(default="Active")

    class Meta:
        db_table = "hrms_teams"
        ordering = ["name"]


class ApprovalChain(TenantModel):
    """Multi-step approval chains for leave, expense and offers."""

    name = models.TextField()
    applies_to = models.TextField()  # leave | expense | offer | ...
    steps = models.JSONField(default=list, blank=True)
    status = models.TextField(default="Active")

    class Meta:
        db_table = "hrms_approval_chains"
        ordering = ["name"]


class Termination(TenantModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="terminations")
    reason = models.TextField(null=True, blank=True)
    last_working_day = models.DateField(null=True, blank=True)
    letter_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    settlement_amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    status = models.TextField(default="Initiated")

    class Meta:
        db_table = "hrms_terminations"
        ordering = ["-created_at"]


class Resignation(TenantModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="resignations")
    submitted_on = models.DateField()
    notice_period_days = models.IntegerField(default=30)
    last_working_day = models.DateField(null=True, blank=True)
    exit_interview_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    handover_to = models.TextField(null=True, blank=True)
    status = models.TextField(default="Submitted")

    class Meta:
        db_table = "hrms_resignations"
        ordering = ["-submitted_on"]


class ResignationChecklistItem(TenantModel):
    resignation = models.ForeignKey(
        Resignation, on_delete=models.CASCADE, related_name="checklist"
    )
    title = models.TextField()
    owner = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_resignation_checklist_items"


class Complaint(TenantModel):
    """Grievances that may name a colleague.

    db.md §11.8 asks for HR-only access and for ``is_anonymous`` to be honoured
    by nulling the raiser in every serialiser -- including exports.
    """

    STATUSES = [
        ("Open", "Open"),
        ("Under Review", "Under Review"),
        ("Resolved", "Resolved"),
        ("Closed", "Closed"),
        ("Dismissed", "Dismissed"),
    ]

    raised_by_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="complaints_raised"
    )
    against_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="complaints_against"
    )
    category = models.TextField(null=True, blank=True)
    description = models.TextField()
    is_anonymous = models.BooleanField(default=False)
    status = models.TextField(choices=STATUSES, default="Open")
    assigned_to = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolution = models.TextField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hrms_complaints"
        ordering = ["-created_at"]
