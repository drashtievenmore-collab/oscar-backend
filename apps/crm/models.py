"""
CRM (db.md §9, api.md §9).

Replaces the localStorage keys ``evenmore-crm-leads-v1``,
``evenmore_crm_deals_v2``, ``leadStageTasksV1`` and the rest, plus the two
automation services (``leadStageAutomation.js``, ``taskCompletionService.js``)
which move server-side entirely (api.md §9.3).

Lead stages are **data, not an enum** -- they are tenant-configurable through
``/crm/stages/``, and the ``icon``/``bg``/``fg`` fields are rendered directly,
so they are persisted.
"""
from django.db import models

from apps.core.models import LegacyIdMixin, TenantModel

PRIORITIES = [("Low", "Low"), ("Medium", "Medium"), ("High", "High"), ("Urgent", "Urgent")]
#: api.md §9.3 -- CRM task status vocabulary.
TASK_STATUSES = [
    ("Open", "Open"),
    ("In Progress", "In Progress"),
    ("Waiting", "Waiting"),
    ("Completed", "Completed"),
    ("Cancelled", "Cancelled"),
]
TASK_OUTCOMES = [
    ("Connected", "Connected"),
    ("No Answer", "No Answer"),
    ("Interested", "Interested"),
    ("Not Interested", "Not Interested"),
    ("Follow-up Required", "Follow-up Required"),
]
NEXT_ACTIONS = [
    ("call-again", "call-again"),
    ("schedule-demo", "schedule-demo"),
    ("send-quotation", "send-quotation"),
    ("move-next-stage", "move-next-stage"),
    ("finish", "finish"),
]
#: api.md §9.3 -- the marker the automation stamps on generated tasks.
AUTOMATION_SOURCE = "Created by Lead Stage Automation"
MANUAL_SOURCE = "Manual"


# ---------------------------------------------------------------------------
# Configuration (db.md §9.3, §9.5)
# ---------------------------------------------------------------------------
class Stage(TenantModel, LegacyIdMixin):
    """Lead stage catalogue. Shipped defaults: New Lead -> Details Collected ->
    Quotation Shared -> Demo Pending -> Demo Done -> Negotiation -> Won / Lost,
    plus an inactive ``Future``."""

    name = models.TextField()
    sequence = models.IntegerField(default=0)
    color = models.TextField(null=True, blank=True)
    icon = models.TextField(null=True, blank=True)
    bg = models.TextField(null=True, blank=True)
    fg = models.TextField(null=True, blank=True)
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_stages"
        ordering = ["sequence", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_crm_stage_name",
            )
        ]

    def __str__(self):
        return self.name


class DealStage(TenantModel):
    """A second, independent catalogue (api.md §9.3) configured on the same screen."""

    name = models.TextField()
    sequence = models.IntegerField(default=0)
    pipeline = models.TextField(null=True, blank=True)
    icon = models.TextField(null=True, blank=True)
    bg = models.TextField(null=True, blank=True)
    fg = models.TextField(null=True, blank=True)
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_deal_stages"
        ordering = ["sequence", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_crm_deal_stage_name",
            )
        ]


class Source(TenantModel):
    name = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_sources"
        ordering = ["name"]


class Industry(TenantModel):
    name = models.TextField()

    class Meta:
        db_table = "crm_industries"
        ordering = ["name"]


class LostReason(TenantModel):
    name = models.TextField()

    class Meta:
        db_table = "crm_lost_reasons"
        ordering = ["name"]


# ---------------------------------------------------------------------------
# Leads (db.md §9.1)
# ---------------------------------------------------------------------------
class Lead(TenantModel, LegacyIdMixin):
    """``status`` in the JSON maps to ``stage.name``, not a stored string --
    stages are tenant-configurable (db.md §9.1).

    The list row's eight counters (``productsCount``, ``openTasksCount`` ...)
    are computed server-side per request; db.md §9.1 warns against a counter
    table until it is measured slow, because premature counter tables drift.
    """

    lead_number = models.TextField()
    name = models.TextField()
    company = models.TextField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    job_title = models.TextField(null=True, blank=True)
    industry = models.TextField(null=True, blank=True)
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="leads")
    source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    owner = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_leads"
    )
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    city = models.TextField(null=True, blank=True)
    state = models.TextField(null=True, blank=True)
    country = models.TextField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    avatar_color = models.TextField(null=True, blank=True)
    photo_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_pinned = models.BooleanField(default=False)
    lost_reason = models.ForeignKey(
        LostReason, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    converted_deal = models.ForeignKey(
        "Deal", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_on = models.DateField(auto_now_add=True)
    #: Values captured by a dynamic form (db.md §9.1).
    custom_values = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "crm_leads"
        ordering = ["-is_pinned", "-created_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "lead_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_crm_lead_number",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "stage"],
                name="ix_crm_leads_stage",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(fields=["client", "owner"], name="ix_crm_leads_owner"),
            models.Index(
                fields=["client", "latitude", "longitude"],
                name="ix_crm_leads_geo",
                condition=models.Q(latitude__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.lead_number} {self.name}"


class LeadSubResource(TenantModel):
    """Common base for the lead workspace children (db.md §9.2)."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True


class LeadUser(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="lead_users")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    role = models.TextField(null=True, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_lead_users"
        constraints = [
            models.UniqueConstraint(fields=["lead", "user"], name="uq_crm_lead_users")
        ]


class LeadProduct(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="products")
    item = models.ForeignKey(
        "masters.Item", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    product_name = models.TextField(null=True, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=1)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_lead_products"


class LeadSource(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="source_entries")
    source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    campaign = models.TextField(null=True, blank=True)
    medium = models.TextField(null=True, blank=True)
    attributed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_lead_sources"


class LeadNote(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "crm_lead_notes"
        ordering = ["-created_at"]


class LeadThread(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="threads")
    subject = models.TextField()

    class Meta:
        db_table = "crm_lead_threads"
        ordering = ["-created_at"]


class LeadThreadMessage(TenantModel):
    thread = models.ForeignKey(LeadThread, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_lead_thread_messages"
        ordering = ["sent_at"]


class LeadCall(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="calls")
    direction = models.TextField(default="outbound")
    outcome = models.TextField(null=True, blank=True)
    duration_seconds = models.IntegerField(default=0)
    notes = models.TextField(null=True, blank=True)
    called_at = models.DateTimeField()
    called_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "crm_lead_calls"
        ordering = ["-called_at"]


class LeadEmail(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="emails")
    subject = models.TextField()
    body = models.TextField(null=True, blank=True)
    to_addresses = models.JSONField(default=list, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    provider_message_id = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_lead_emails"
        ordering = ["-created_at"]


class LeadFile(LeadSubResource):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="files")
    file = models.ForeignKey("core.File", on_delete=models.PROTECT, related_name="+")
    label = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_lead_files"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Task templates and tasks (db.md §9.3)
# ---------------------------------------------------------------------------
class MasterTask(TenantModel):
    """Reusable task library (api.md §9.3)."""

    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    role = models.TextField(null=True, blank=True)
    department = models.TextField(null=True, blank=True)
    duration_days = models.IntegerField(default=1)
    priority = models.TextField(choices=PRIORITIES, default="Medium")
    sort_order = models.IntegerField(default=0)
    #: A master task may apply to several stages (the frontend's `stages[]`).
    stages = models.ManyToManyField(Stage, blank=True, related_name="master_tasks")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_master_tasks"
        ordering = ["sort_order", "title"]


class StageTask(TenantModel):
    """Per-stage template. ``auto_create`` is what the stage automation reads."""

    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="stage_tasks")
    master_task = models.ForeignKey(
        MasterTask, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    assignee_role = models.TextField(null=True, blank=True)
    department = models.TextField(null=True, blank=True)
    offset_days = models.IntegerField(default=0)
    priority = models.TextField(choices=PRIORITIES, default="Medium")
    sort_order = models.IntegerField(default=0)
    required = models.BooleanField(default=False)
    auto_create = models.BooleanField(default=True)
    #: Max times the automation may fire this template for one lead
    #: (1 = create the task once). The UI edits it as "MAX REPEATS".
    repeats = models.IntegerField(default=1, null=True, blank=True)

    class Meta:
        db_table = "crm_stage_tasks"
        ordering = ["sort_order", "title"]


class Task(TenantModel, LegacyIdMixin):
    task_number = models.TextField(null=True, blank=True)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks"
    )
    deal = models.ForeignKey(
        "Deal", null=True, blank=True, on_delete=models.CASCADE, related_name="tasks"
    )
    stage_task = models.ForeignKey(
        StageTask, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    assignee = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="crm_tasks"
    )
    assignee_role = models.TextField(null=True, blank=True)
    department = models.TextField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    priority = models.TextField(choices=PRIORITIES, default="Medium")
    status = models.TextField(choices=TASK_STATUSES, default="Open")
    source = models.TextField(default=MANUAL_SOURCE)
    outcome = models.TextField(choices=TASK_OUTCOMES, null=True, blank=True)
    next_action = models.TextField(choices=NEXT_ACTIONS, null=True, blank=True)
    completion_note = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    follow_up_task = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="spawned_from"
    )

    class Meta:
        db_table = "crm_tasks"
        ordering = ["due_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(lead__isnull=False) | models.Q(deal__isnull=False),
                name="ck_crm_task_parent",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "assignee", "status", "due_date"],
                name="ix_crm_tasks_assignee",
            )
        ]

    def __str__(self):
        return self.title


class TaskAllocation(TenantModel, LegacyIdMixin):
    """A **separate entity** from lead tasks (api.md §9.3).

    This is internal work assignment, not lead follow-up. Every assignment and
    status change appends a human-readable ``audit`` line, generated server-side
    from the structured record.
    """

    STATUSES = [
        ("Pending", "Pending"),
        ("In Progress", "In Progress"),
        ("Completed", "Completed"),
    ]

    title = models.TextField()
    description = models.TextField(null=True, blank=True)
    department = models.TextField(null=True, blank=True)
    assignee = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations"
    )
    assigned_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    priority = models.TextField(
        choices=[("Low", "Low"), ("Medium", "Medium"), ("High", "High")], default="Medium"
    )
    deadline = models.DateTimeField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Pending")
    file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    file_name = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_task_allocations"
        ordering = ["-created_at"]


class TaskAllocationAudit(TenantModel):
    allocation = models.ForeignKey(
        TaskAllocation, on_delete=models.CASCADE, related_name="audit_entries"
    )
    action = models.TextField()
    text = models.TextField()
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "crm_task_allocation_audit"
        ordering = ["created_at"]


# ---------------------------------------------------------------------------
# Deals, contracts, projects (db.md §9.4)
# ---------------------------------------------------------------------------
class Deal(TenantModel, LegacyIdMixin):
    STAGES = [
        ("Draft", "Draft"),
        ("Sent", "Sent"),
        ("Open", "Open"),
        ("Revised", "Revised"),
        ("Declined", "Declined"),
        ("Won", "Won"),
        ("Lost", "Lost"),
    ]

    deal_number = models.TextField()
    title = models.TextField()
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals"
    )
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="deals"
    )
    owner = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_deals"
    )
    stage = models.TextField(choices=STAGES, default="Draft")
    value = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    probability = models.SmallIntegerField(null=True, blank=True)
    expected_close_date = models.DateField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.ForeignKey(
        LostReason, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    quotation = models.ForeignKey(
        "sales.Quotation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    crm_project = models.ForeignKey(
        "CrmProject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "crm_deals"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "deal_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_crm_deal_number",
            ),
            models.CheckConstraint(
                condition=models.Q(probability__isnull=True)
                | models.Q(probability__gte=0, probability__lte=100),
                name="ck_crm_deal_probability",
            ),
        ]
        indexes = [models.Index(fields=["client", "stage"], name="ix_crm_deals_stage")]

    def __str__(self):
        return f"{self.deal_number} {self.title}"


class DealActivity(models.Model):
    id = models.BigAutoField(primary_key=True)
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="activities")
    type = models.TextField()
    description = models.TextField(null=True, blank=True)
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_deal_activities"
        ordering = ["-created_at"]


class Contract(TenantModel, LegacyIdMixin):
    """``displayStatus`` (``Expiring Soon``, ``Expired``) is derived from
    ``end_date`` at read time, never stored (db.md §9.4)."""

    STATUSES = [
        ("Draft", "Draft"),
        ("Active", "Active"),
        ("Closed", "Closed"),
        ("Cancelled", "Cancelled"),
    ]

    contract_number = models.TextField()
    title = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="contracts")
    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts"
    )
    template_key = models.TextField(null=True, blank=True)
    contract_type = models.TextField(null=True, blank=True)
    value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.TextField(choices=STATUSES, default="Draft")
    body = models.TextField(null=True, blank=True)
    signed_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    signed_at = models.DateTimeField(null=True, blank=True)
    expiring_soon_days = models.IntegerField(default=30)

    class Meta:
        db_table = "crm_contracts"
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "contract_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_contract_number",
            ),
            # assertContractDates (api.md §9.5)
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="ck_contract_dates",
            ),
        ]

    def __str__(self):
        return self.contract_number


class CrmProject(TenantModel, LegacyIdMixin):
    """Deliberately separate from ``pms_projects`` (db.md §9.4).

    They are different entities with different lifecycles; merging them would
    force PMS stage machinery onto CRM's lightweight project card.
    """

    name = models.TextField()
    code = models.TextField(null=True, blank=True)
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="crm_projects"
    )
    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.SET_NULL, related_name="crm_projects"
    )
    owner = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.TextField(default="Active")
    value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    progress = models.SmallIntegerField(default=0)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_projects"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Allocation, tracking and forms (db.md §9.5)
# ---------------------------------------------------------------------------
class UserAllocation(TenantModel):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="crm_allocations")
    territory = models.TextField(null=True, blank=True)
    city = models.TextField(null=True, blank=True)
    state = models.TextField(null=True, blank=True)
    industry = models.ForeignKey(
        Industry, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    weight = models.SmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_user_allocations"


class UserLocation(models.Model):
    """High-volume, append-only field-user pings.

    db.md §9.5 asks for monthly partitions and 90-day retention; the retention
    sweep lives in the maintenance command.
    """

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey("accounts.Client", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="location_pings")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    recorded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_user_locations"
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(fields=["client", "user", "-recorded_at"], name="ix_crm_user_locations")
        ]


class Form(TenantModel, LegacyIdMixin):
    """The one place a builder-authored tree genuinely belongs in jsonb."""

    name = models.TextField()
    slug = models.SlugField(max_length=140, null=True, blank=True)
    schema = models.JSONField(default=dict)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    kind = models.TextField(default="lead")  # lead | task

    class Meta:
        db_table = "crm_forms"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "slug"],
                condition=models.Q(slug__isnull=False, deleted_at__isnull=True),
                name="uq_crm_form_slug",
            )
        ]

    def __str__(self):
        return self.name


class FormSubmission(TenantModel):
    """Keeps the raw ``payload`` **and** the created lead id, so a mapping bug
    can be replayed without losing the customer's input (db.md §9.5)."""

    form = models.ForeignKey(Form, on_delete=models.CASCADE, related_name="submissions")
    payload = models.JSONField(default=dict)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="submissions"
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_form_submissions"
        ordering = ["-submitted_at"]
