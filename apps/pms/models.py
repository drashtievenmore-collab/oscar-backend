"""
PMS -- Project Management (db.md §10, api.md §10) plus grey fabric monitoring.

Project work uses Project, Stage, Task, Document, Approval, Delay. Grey fabric
work uses ProductionInstruction, DailyProductionEntry, IncentiveScheme,
IncentiveCalculation with prod_* tables. The two tracks share the app but not
the tables. HRMS Employee is read only here.

Replaces ``stores/pmsStore.js`` (~3k lines, localStorage-persisted) and
``stores/proofShareStore.js``. The frontend keeps stages, tasks, documents,
approvals and delays nested inside each project object; db.md §10 is explicit
that all five are flattened here, because a nested-JSON shortcut would hurt
most in exactly this module.
"""
from django.db import models

from apps.core.models import LegacyIdMixin, TenantModel

PRIORITIES = [("Low", "Low"), ("Medium", "Medium"), ("High", "High"), ("Urgent", "Urgent")]

PROJECT_STATUSES = [
    ("Draft", "Draft"),
    ("In Progress", "In Progress"),
    ("Delayed", "Delayed"),
    ("At Risk", "At Risk"),
    ("Completed", "Completed"),
    ("On Hold", "On Hold"),
]

#: api.md §10.3 -- the full stage status vocabulary.
STAGE_STATUSES = [
    ("Not Started", "Not Started"),
    ("Assigned", "Assigned"),
    ("In Progress", "In Progress"),
    ("At Risk", "At Risk"),
    ("Delayed", "Delayed"),
    ("Submitted", "Submitted"),
    ("Under Review", "Under Review"),
    ("Approved", "Approved"),
    ("Need Improvement", "Need Improvement"),
    ("Completed", "Completed"),
    ("Blocked", "Blocked"),
]
#: Stages that count as closed for the project-completion gate.
CLOSED_STAGE_STATUSES = ("Completed", "Approved")

TASK_STATUSES = [
    ("Not Started", "Not Started"),
    ("In Progress", "In Progress"),
    ("Blocked", "Blocked"),
    ("Completed", "Completed"),
]

APPROVAL_STATUSES = [
    ("Pending", "Pending"),
    ("Approved", "Approved"),
    ("Need Improvement", "Need Improvement"),
]

DURATION_UNITS = [("Hours", "Hours"), ("Days", "Days")]

#: api.md §10.9 -- the action codes the UI already renders.
ACTIVITY_ACTIONS = [
    "PROJECT_CREATED", "PROJECT_COMPLETED", "STAGES_CONFIGURED", "STAGE_ASSIGNED",
    "STAGE_STARTED", "STAGE_STATUS_CHANGED", "PROGRESS_UPDATED", "STAGE_HANDOFF",
    "DOCUMENT_UPLOADED", "APPROVAL_REQUESTED", "DOCUMENT_APPROVED",
    "REVISION_REQUESTED", "DELAY_LOGGED", "RECOVERY_PLAN_UPDATED", "DELAY_RESOLVED",
]


# ---------------------------------------------------------------------------
# Configuration (db.md §10.1)
# ---------------------------------------------------------------------------
class Department(TenantModel, LegacyIdMixin):
    """The colour is an identity colour used on Gantt and pipeline charts.

    Greens, ambers and reds are deliberately excluded from the palette because
    they are reserved for state (api.md §10.1).
    """

    name = models.TextField()
    color = models.TextField(default="#1f6bff")
    capacity = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "pms_departments"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_pms_department_name",
            )
        ]

    def __str__(self):
        return self.name


class StageConfig(TenantModel, LegacyIdMixin):
    """Stage template catalogue.

    ``sequence`` is unique and **deferrable** in db.md §10.1, because reordering
    swaps two rows inside one transaction. Django cannot declare a deferrable
    unique index portably, so the reorder service renumbers through a temporary
    offset instead -- same outcome, no mid-statement collision.
    """

    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    sequence = models.IntegerField(default=0)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="stage_configs"
    )
    default_duration = models.DecimalField(max_digits=9, decimal_places=2, default=1)
    duration_unit = models.TextField(choices=DURATION_UNITS, default="Days")
    assigned_role = models.TextField(null=True, blank=True)
    required_approval = models.BooleanField(default=False)
    required_document = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "pms_stage_configs"
        ordering = ["sequence", "name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Projects and stages (db.md §10.2)
# ---------------------------------------------------------------------------
class Project(TenantModel, LegacyIdMixin):
    """``code`` is ``PRJ-2026-001`` and the UI uses it in URLs.

    db.md §1.2: keep the uuid PK and expose ``code`` as the route key.
    """

    code = models.TextField()
    sales_order = models.ForeignKey(
        "sales.SalesOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="pms_projects"
    )
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="pms_projects"
    )
    customer_name = models.TextField(null=True, blank=True)  # frozen display copy

    product_name = models.TextField(null=True, blank=True)
    order_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    specifications = models.TextField(null=True, blank=True)

    project_manager = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_projects"
    )
    current_stage = models.ForeignKey(
        "ProjectStage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    current_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    priority = models.TextField(choices=PRIORITIES, default="Medium")
    #: DERIVED from the stage rollup on every task write (db.md §12).
    overall_completion_pct = models.SmallIntegerField(default=0)
    start_date = models.DateTimeField(null=True, blank=True)
    expected_completion_date = models.DateTimeField(null=True, blank=True)
    actual_completion_date = models.DateTimeField(null=True, blank=True)
    status = models.TextField(choices=PROJECT_STATUSES, default="Draft")

    class Meta:
        db_table = "pms_projects"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_pms_project_code",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    overall_completion_pct__gte=0, overall_completion_pct__lte=100
                ),
                name="ck_pms_project_pct",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "status"],
                name="ix_pms_projects_status",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(fields=["client", "project_manager"], name="ix_pms_projects_pm"),
        ]

    def __str__(self):
        return self.code


class ProjectStage(TenantModel, LegacyIdMixin):
    """Stage configs are **copied, not referenced**, for the fields that matter.

    Editing a template must not retroactively change a running project's gates
    -- the same freezing principle as document lines (db.md §10.2).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="stages")
    stage_config = models.ForeignKey(
        StageConfig, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    name = models.TextField()
    sequence = models.IntegerField(default=0)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="project_stages"
    )
    assigned_team = models.TextField(null=True, blank=True)
    assigned_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_stages"
    )
    planned_duration = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True
    )
    duration_unit = models.TextField(choices=DURATION_UNITS, null=True, blank=True)
    start_datetime = models.DateTimeField(null=True, blank=True)
    expected_completion_datetime = models.DateTimeField(null=True, blank=True)
    actual_start_datetime = models.DateTimeField(null=True, blank=True)
    actual_completion_datetime = models.DateTimeField(null=True, blank=True)
    #: DERIVED from tasks (db.md §12).
    completion_pct = models.SmallIntegerField(default=0)
    #: Stage weight percentage in project (0 to 100).
    weight_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    required_approval = models.BooleanField(default=False)
    required_document = models.BooleanField(default=False)
    status = models.TextField(choices=STAGE_STATUSES, default="Not Started")

    class Meta:
        db_table = "pms_project_stages"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "sequence"], name="uq_pms_project_stage_sequence"
            ),
            models.CheckConstraint(
                condition=models.Q(completion_pct__gte=0, completion_pct__lte=100),
                name="ck_pms_stage_pct",
            ),
            models.CheckConstraint(
                condition=models.Q(weight_pct__gte=0, weight_pct__lte=100),
                name="ck_pms_stage_weight_pct",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"


class Task(TenantModel, LegacyIdMixin):
    """``project`` is denormalised (reachable via ``stage``) because
    ``/pms/my-tasks/`` and ``/pms/tasks/?projectId=`` are hot paths that would
    otherwise join every time (db.md §10.2)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    stage = models.ForeignKey(ProjectStage, on_delete=models.CASCADE, related_name="tasks")
    task_name = models.TextField()
    description = models.TextField(null=True, blank=True)
    assigned_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="pms_tasks"
    )
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    completion_pct = models.SmallIntegerField(default=0)
    priority = models.TextField(choices=PRIORITIES, default="Medium")
    status = models.TextField(choices=TASK_STATUSES, default="Not Started")

    class Meta:
        db_table = "pms_tasks"
        ordering = ["due_date", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(completion_pct__gte=0, completion_pct__lte=100),
                name="ck_pms_task_pct",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "assigned_user", "status", "due_date"],
                name="ix_pms_tasks_assignee",
            ),
            models.Index(fields=["stage"], name="ix_pms_tasks_stage"),
        ]

    def __str__(self):
        return self.task_name


# ---------------------------------------------------------------------------
# Documents, versions and approvals (db.md §10.3)
# ---------------------------------------------------------------------------
class Document(TenantModel, LegacyIdMixin):
    """Versioning (api.md §10.5): a ``Need Improvement`` decision never edits
    the document. The next upload on the same ``(stage, doc_key)`` inserts
    ``version + 1``, carries the revision reason forward and flips
    ``is_current``. Every version stays retrievable.

    The bytes live in object storage via the file service; the IndexedDB store
    the frontend uses today disappears, which is what lets a client open the
    drawing on their own device.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="documents")
    stage = models.ForeignKey(ProjectStage, on_delete=models.CASCADE, related_name="documents")
    #: Groups versions of the same logical document.
    doc_key = models.TextField()
    version = models.IntegerField(default=1)
    file = models.ForeignKey("core.File", on_delete=models.PROTECT, related_name="+")
    file_name = models.TextField()
    file_size = models.BigIntegerField(null=True, blank=True)
    preview_url = models.TextField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    comments = models.TextField(null=True, blank=True)
    revision_reason = models.TextField(null=True, blank=True)
    approval_status = models.TextField(choices=APPROVAL_STATUSES, default="Pending")
    is_current = models.BooleanField(default=True)
    #: Marks a design proof, which the POLICY_DESIGN_APPROVAL gate looks for.
    is_proof = models.BooleanField(default=False)

    class Meta:
        db_table = "pms_documents"
        ordering = ["-uploaded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["stage", "doc_key", "version"], name="uq_pms_document_version"
            ),
            models.UniqueConstraint(
                fields=["stage", "doc_key"],
                condition=models.Q(is_current=True),
                name="uq_pms_doc_current",
            ),
        ]

    def __str__(self):
        return f"{self.file_name} v{self.version}"


class Approval(TenantModel):
    APPROVER_TYPES = [("PM", "PM"), ("Client", "Client")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="approvals")
    stage = models.ForeignKey(ProjectStage, on_delete=models.CASCADE, related_name="approvals")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="approvals")
    approver_type = models.TextField(choices=APPROVER_TYPES)
    approver_name = models.TextField(null=True, blank=True)
    approver_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.TextField(choices=APPROVAL_STATUSES, default="Pending")
    decision_at = models.DateTimeField(null=True, blank=True)
    comments = models.TextField(null=True, blank=True)
    revision_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "pms_approvals"
        ordering = ["-requested_at"]
        indexes = [
            models.Index(
                fields=["client", "status"],
                name="ix_pms_approvals_pending",
                condition=models.Q(status="Pending"),
            )
        ]


class ProofShare(TenantModel):
    """Backs ``/pms/approve/:token`` outside the app shell (db.md §10.4).

    ``decided_by`` is text, not a user FK -- the decider is a customer without
    an account. A decision writes through to ``Approval`` and therefore to the
    handoff gate, in one transaction.
    """

    STATUSES = [("Active", "Active"), ("Revoked", "Revoked"), ("Expired", "Expired")]
    DECISIONS = [("Approved", "Approved"), ("Need Improvement", "Need Improvement")]

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="shares")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="proof_shares")
    token_hash = models.TextField(unique=True)
    recipient_name = models.TextField(null=True, blank=True)
    recipient_email = models.EmailField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Active")
    decision = models.TextField(choices=DECISIONS, null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.TextField(null=True, blank=True)
    decision_comments = models.TextField(null=True, blank=True)
    revision_reason = models.TextField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "pms_proof_shares"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Delays and activity (db.md §10.5)
# ---------------------------------------------------------------------------
class Delay(TenantModel):
    """A delay is a **singleton on the stage** in the API (``stage.delayDetails``)
    -- a stage has at most one *open* delay at a time (api.md §10.7).

    It is a table here so the history survives resolution, which the delay
    dashboard and the delay-reason Pareto report both need. The partial index
    on unresolved rows is the ``OPEN_DELAY`` handoff blocker's query.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="delays")
    stage = models.ForeignKey(ProjectStage, on_delete=models.CASCADE, related_name="delays")
    reason = models.TextField()
    #: Free text with suggested values, exposed from /pms/settings/ rather than
    #: hardcoded, since the Pareto report groups on it (api.md §10.7).
    category = models.TextField(null=True, blank=True)
    responsible_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expected_recovery_date = models.DateField(null=True, blank=True)
    recovery_plan = models.TextField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolution_notes = models.TextField(null=True, blank=True)
    #: DERIVED on resolution.
    delay_days = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "pms_delays"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["client", "stage"],
                name="ix_pms_delays_open",
                condition=models.Q(resolved_at__isnull=True),
            )
        ]

    @property
    def is_delayed(self):
        return self.resolved_at is None


class Settings(TenantModel):
    """``GET/PUT /pms/settings/`` (api.md §10.1).

    Held as a row rather than in the generic ``settings`` blob table because
    every field here is read on the hot dashboard path and several are
    referenced by the handoff gate.
    """

    #: The share of a stage's planned window that may elapse before it is
    #: flagged At Risk -- and only when completion is also under 50%.
    at_risk_threshold_pct = models.SmallIntegerField(default=70)
    require_client_approval_on_design = models.BooleanField(default=True)
    require_qa_certificate = models.BooleanField(default=True)
    notifications = models.JSONField(
        default=dict, blank=True
    )  # enabled, onAssignment, onApproval, onDelay, onCompletion
    default_department_capacity = models.IntegerField(default=20)
    department_capacity = models.JSONField(default=dict, blank=True)
    status_colors = models.JSONField(default=dict, blank=True)  # overdue: '#d03b3b'
    delay_categories = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "pms_settings"

    DEFAULT_NOTIFICATIONS = {
        "enabled": True,
        "onAssignment": True,
        "onApproval": True,
        "onDelay": True,
        "onCompletion": True,
    }
    DEFAULT_DEPARTMENT_CAPACITY = {
        "Design": 18,
        "Production": 24,
        "Quality": 12,
        "Packaging": 10,
        "Installation": 8,
    }
    #: `overdue` is the one reserved state colour: it can be recoloured but
    #: never renamed or deleted (api.md §10.1).
    DEFAULT_STATUS_COLORS = {"overdue": "#d03b3b"}
    DEFAULT_DELAY_CATEGORIES = [
        "Material Issue",
        "Design Issue",
        "Quality Issue",
        "Client Approval Pending",
        "Client Revision",
        "Other",
    ]


# ---------------------------------------------------------------------------
# Grey fabric production monitoring (moved from apps.production per product
# decision). Tables keep prod_* names so existing rows stay valid. HRMS is
# read only here: instructions and entries point at hrms Employee.
# ---------------------------------------------------------------------------
class ProductionInstruction(TenantModel):
    STATUSES = [
        ("Draft", "Draft"),
        ("In Progress", "In Progress"),
        ("Completed", "Completed"),
        ("Verified", "Verified"),
        ("Closed", "Closed"),
    ]

    instruction_number = models.TextField(null=True, blank=True)
    agency_name = models.TextField()
    order_reference = models.TextField()
    order_meter = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    pi_date = models.DateField(null=True, blank=True)
    fabric = models.TextField(null=True, blank=True)
    process_type = models.TextField(null=True, blank=True)
    agreed_job_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rejected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    verification_remarks = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    employee = models.ForeignKey(
        "hrms.Employee",
        on_delete=models.PROTECT,
        related_name="production_instructions",
    )
    supervisor = models.ForeignKey(
        "hrms.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supervised_production_instructions",
    )
    status = models.TextField(choices=[(s, s) for s in
                                       ["Draft", "In Progress", "Completed", "Verified", "Closed"]],
                              default="Draft")
    verified_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "prod_instructions"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "instruction_number"],
                condition=models.Q(
                    deleted_at__isnull=True, instruction_number__isnull=False
                ),
                name="uq_prod_instructions_number",
            ),
            models.CheckConstraint(
                condition=models.Q(order_meter__gte=0), name="ck_prod_instructions_meter"
            ),
            models.CheckConstraint(
                condition=models.Q(agreed_job_rate__gte=0),
                name="ck_prod_instructions_rate",
            ),
            models.CheckConstraint(
                condition=models.Q(rejected_qty__gte=0),
                name="ck_prod_instructions_rejected",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "agency_name"], name="ix_prod_instructions_agency"),
            models.Index(fields=["client", "employee"], name="ix_prod_instructions_emp"),
        ]

    def __str__(self):
        return self.instruction_number or f"Instruction {self.id}"


class DailyProductionEntry(TenantModel):
    instruction = models.ForeignKey(
        ProductionInstruction, on_delete=models.CASCADE, related_name="daily_entries"
    )
    entry_date = models.DateField()
    meters = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    entered_by_employee = models.ForeignKey(
        "hrms.Employee", on_delete=models.PROTECT, related_name="production_entries"
    )
    entered_by_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "prod_daily_entries"
        ordering = ["entry_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(meters__gt=0), name="ck_prod_daily_entries_meters"
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "instruction", "entry_date"],
                name="ix_prod_daily_entries_instr",
            ),
            models.Index(
                fields=["client", "entered_by_employee", "entry_date"],
                name="ix_prod_daily_entries_emp",
            ),
        ]


class IncentiveScheme(TenantModel):
    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    rate_pct = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    applies_from = models.DateField(null=True, blank=True)
    applies_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "incentive_schemes"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_incentive_schemes_name",
            ),
        ]

    def __str__(self):
        return self.name


class IncentiveCalculation(TenantModel):
    STATUSES = [("Calculated", "Calculated"), ("Posted", "Posted"), ("Reversed", "Reversed")]

    calc_number = models.TextField(null=True, blank=True)
    scheme = models.ForeignKey(
        IncentiveScheme, on_delete=models.PROTECT, related_name="calculations"
    )
    employee = models.ForeignKey(
        "hrms.Employee", on_delete=models.PROTECT, related_name="incentive_calcs"
    )
    period_label = models.TextField()
    period_start = models.DateField()
    period_end = models.DateField()
    net_sales = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    returns_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    incentive_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(
        choices=[(s, s) for s in ["Calculated", "Posted", "Reversed"]],
        default="Calculated",
    )
    detail = models.JSONField(default=dict, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "incentive_calcs"
        ordering = ["-period_start", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "calc_number"],
                condition=models.Q(deleted_at__isnull=True, calc_number__isnull=False),
                name="uq_incentive_calcs_number",
            ),
            models.UniqueConstraint(
                fields=["client", "scheme", "employee", "period_label"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_incentive_calc_period",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "employee", "period_start"],
                name="ix_incentive_calcs_emp",
            ),
        ]

    def __str__(self):
        return self.calc_number or f"Incentive {self.id}"
