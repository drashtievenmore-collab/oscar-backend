"""
Base model classes and the platform tables of db.md §2.

Every business table inherits :class:`TenantModel`, which supplies the tenant
key (db.md §1.3) and the audit columns (db.md §1.4). Nothing financial is ever
hard-deleted: ``deleted_at`` exists for drafts and masters only, and posted
documents move to ``status = 'Cancelled'`` keeping their reversing rows.
"""
import uuid

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone

from .tenancy import get_current_client_id


# ---------------------------------------------------------------------------
# Managers
# ---------------------------------------------------------------------------
class BaseQuerySet(models.QuerySet):
    def live(self):
        """Rows that have not been soft-deleted (db.md §1.4)."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)

    def for_tenant(self, client_id):
        """Scope to one tenant. ``None`` yields nothing rather than everything.

        Returning an empty queryset for a missing tenant is deliberate: a bug
        that loses the tenant must show up as "no data", never as another
        tenant's data.
        """
        if client_id is None:
            return self.none()
        return self.filter(client_id=client_id)

    def current_tenant(self):
        return self.for_tenant(get_current_client_id())


class BaseManager(models.Manager.from_queryset(BaseQuerySet)):
    use_in_migrations = False


class LiveManager(BaseManager):
    """Default manager that hides soft-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


# ---------------------------------------------------------------------------
# Abstract bases
# ---------------------------------------------------------------------------
class UUIDModel(models.Model):
    """Surrogate uuid PK (db.md §1.2). Opaque to the UI; never encodes meaning."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class AuditedModel(UUIDModel):
    """The audit columns every table carries (db.md §1.4).

    ``updated_at`` backs ``If-Unmodified-Since`` / ``expectedVersion``
    (api.md §1.8); the update path compares it and returns 409 on mismatch.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = BaseManager()
    live_objects = LiveManager()

    class Meta:
        abstract = True

    def soft_delete(self, user=None):
        self.deleted_at = timezone.now()
        self.deleted_by = user
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by", "updated_at"])

    @property
    def is_deleted(self):
        return self.deleted_at is not None


class TenantModel(AuditedModel):
    """Everything a tenant owns. ``client_id`` is mandatory and never null."""

    client = models.ForeignKey(
        "accounts.Client", on_delete=models.PROTECT, related_name="+", db_index=True
    )

    class Meta:
        abstract = True


class LegacyIdMixin(models.Model):
    """Keeps the mock id the demo seeder imported from (db.md §1.2, §14.1).

    Cross-references inside the frontend mock files are by ids like ``cust-1``
    and ``PRJ-2026-001``; the seeder resolves them through a legacy_id -> uuid
    map, and keeping the column lets a mock-referencing fixture still resolve
    during cutover.
    """

    legacy_id = models.TextField(null=True, blank=True, db_index=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# db.md §2.4 -- Files
# ---------------------------------------------------------------------------
class File(AuditedModel):
    """Two-step upload (api.md §1.12): a `pending` row, then `commit`."""

    SCOPES = [
        ("pms_document", "PMS document"),
        ("crm_lead", "CRM lead"),
        ("hrms_document", "HRMS document"),
        ("expense_receipt", "Expense receipt"),
        ("avatar", "Avatar"),
        ("company_logo", "Company logo"),
        ("company_signature", "Company signature"),
        ("resume", "Candidate resume"),
        ("offer_letter", "Offer letter"),
        ("contract", "Contract"),
        ("policy", "Policy"),
        ("import", "Bulk import"),
        ("export", "Export"),
        ("other", "Other"),
    ]
    STATUSES = [("pending", "pending"), ("committed", "committed"), ("deleted", "deleted")]

    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="files"
    )
    scope = models.TextField(choices=SCOPES, default="other")
    storage_key = models.TextField(unique=True)
    file_name = models.TextField()
    content_type = models.TextField()
    file_size = models.BigIntegerField(default=0)
    checksum_sha256 = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="pending")
    uploaded_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "files"
        indexes = [
            # db.md §2.4 -- sweep pending rows older than 24h.
            models.Index(
                fields=["created_at"],
                name="ix_files_orphans",
                condition=models.Q(status="pending"),
            ),
            models.Index(fields=["client", "scope"], name="ix_files_scope"),
        ]

    def __str__(self):
        return f"{self.file_name} ({self.status})"


# ---------------------------------------------------------------------------
# db.md §2.5 -- Audit log
# ---------------------------------------------------------------------------
class AuditLog(models.Model):
    """One append-only log behind three screens (api.md §1.10).

    PMS activity tab, CRM lead timeline and Administration -> Audit Logs all
    read this table. Written from a single service function inside the same
    transaction as the mutation -- never from a post-commit hook, or a
    rolled-back transaction leaves a phantom audit row.
    """

    id = models.BigAutoField(primary_key=True)
    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="audit_entries"
    )
    actor = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    actor_name = models.TextField(default="SYSTEM")
    action = models.TextField()
    entity_type = models.TextField()
    entity_id = models.UUIDField(null=True, blank=True)
    entity_label = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    # PMS activity vocabulary (api.md §10.9) renders from/to as a diff.
    from_value = models.TextField(null=True, blank=True)
    to_value = models.TextField(null=True, blank=True)
    comments = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["client", "entity_type", "entity_id", "-created_at"],
                name="ix_audit_entity",
            ),
            models.Index(fields=["client", "actor", "-created_at"], name="ix_audit_actor"),
        ]

    def __str__(self):
        return f"{self.action} {self.entity_type} {self.entity_label or self.entity_id}"


# ---------------------------------------------------------------------------
# db.md §2.6 -- Notifications
# ---------------------------------------------------------------------------
class Notification(models.Model):
    CATEGORIES = [
        ("crm", "crm"),
        ("hrms", "hrms"),
        ("pms", "pms"),
        ("erp", "erp"),
        ("system", "system"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="notifications"
    )
    recipient = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.TextField()
    category = models.TextField(choices=CATEGORIES)
    entity_type = models.TextField(null=True, blank=True)
    entity_id = models.UUIDField(null=True, blank=True)
    actor = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    title = models.TextField()
    body = models.TextField(null=True, blank=True)
    channels = models.JSONField(default=list)  # in_app | email | sms | whatsapp
    payload = models.JSONField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at"]
        indexes = [
            # The digest endpoint is a grouped count over this partial index --
            # it must not scan the table (db.md §2.6).
            models.Index(
                fields=["client", "recipient", "-created_at"],
                name="ix_notifications_unread",
                condition=models.Q(read_at__isnull=True),
            ),
        ]


class NotificationDelivery(models.Model):
    """One row per channel attempt (db.md §2.6)."""

    STATUSES = [
        ("queued", "queued"),
        ("sent", "sent"),
        ("failed", "failed"),
        ("skipped", "skipped"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries"
    )
    channel = models.TextField()
    status = models.TextField(choices=STATUSES, default="queued")
    provider_ref = models.TextField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification_deliveries"


# ---------------------------------------------------------------------------
# db.md §2.7 -- Document numbering
# ---------------------------------------------------------------------------
class NumberSeries(models.Model):
    """Gapless sequences per series, per financial year, per tenant.

    Deliberately *not* a Postgres sequence: sequences are non-transactional and
    leave gaps, which a GST audit will flag (db.md §2.7). Allocation takes a
    ``pg_advisory_xact_lock`` and bumps this row inside the creating
    transaction, so a rollback returns the number.
    """

    RESET_POLICIES = [
        ("fy", "Financial year"),
        ("yearly", "Calendar year"),
        ("monthly", "Monthly"),
        ("never", "Never"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="number_series"
    )
    series_key = models.TextField()
    prefix = models.TextField()
    fy_label = models.TextField(default="")
    reset_policy = models.TextField(choices=RESET_POLICIES, default="fy")
    pad_width = models.SmallIntegerField(default=4)
    separator = models.TextField(default="-")
    next_value = models.BigIntegerField(default=1)

    class Meta:
        db_table = "number_series"
        constraints = [
            models.UniqueConstraint(
                fields=["client", "series_key", "fy_label"], name="uq_number_series"
            )
        ]

    def __str__(self):
        return f"{self.series_key}/{self.fy_label} -> {self.next_value}"


# ---------------------------------------------------------------------------
# db.md §2.8 -- Settings
# ---------------------------------------------------------------------------
class Setting(models.Model):
    """One narrow key-value table (db.md §2.8).

    The settings endpoints (api.md §3.4) are GET/PUT blobs and adding a
    preference must not be a migration. Each blob is validated against a schema
    in the application layer, not the database.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="settings_blobs"
    )
    key = models.TextField()
    value = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "settings"
        constraints = [
            models.UniqueConstraint(fields=["client", "key"], name="uq_settings_client_key")
        ]

    def __str__(self):
        return f"{self.key}"


class CompanyProfile(models.Model):
    """db.md §2.8. ``state`` drives the CGST+SGST vs IGST split on every document."""

    client = models.OneToOneField(
        "accounts.Client",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="company_profile",
    )
    legal_name = models.TextField()
    trade_name = models.TextField(null=True, blank=True)
    gstin = models.TextField(null=True, blank=True)
    pan = models.TextField(null=True, blank=True)
    cin = models.TextField(null=True, blank=True)
    address = models.JSONField(default=dict)
    state = models.TextField(default="")
    state_code = models.TextField(null=True, blank=True)
    #: api.md §1.6 -- money is a plain number in *this* currency; the client
    #: formats it and must not convert it against anything else.
    currency = models.TextField(default="INR")
    phone = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    website = models.TextField(null=True, blank=True)
    logo_file = models.ForeignKey(
        File, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    signature_file = models.ForeignKey(
        File, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "company_profile"

    def __str__(self):
        return self.legal_name


# ---------------------------------------------------------------------------
# db.md §1.9 -- Idempotency
# ---------------------------------------------------------------------------
class IdempotencyKey(models.Model):
    """api.md §1.8 -- a replay returns the original 201 body.

    The row is inserted inside the creating transaction. A replay with a
    matching ``request_hash`` returns the stored response; a replay with a
    different hash is 409.
    """

    STATES = [
        ("in_progress", "in_progress"),
        ("completed", "completed"),
        ("failed", "failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "accounts.Client", on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    key = models.TextField()
    endpoint = models.TextField()
    request_hash = models.TextField()
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    entity_type = models.TextField(null=True, blank=True)
    entity_id = models.UUIDField(null=True, blank=True)
    state = models.TextField(choices=STATES, default="in_progress")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "idempotency_keys"
        constraints = [
            models.UniqueConstraint(
                fields=["client", "key", "endpoint"], name="uq_idempotency_keys"
            )
        ]
        indexes = [models.Index(fields=["expires_at"], name="ix_idempotency_expiry")]


# ---------------------------------------------------------------------------
# api.md §12.3 -- Support requests
# ---------------------------------------------------------------------------
class SupportTicket(TenantModel):
    """The floating support widget, which currently only toasts."""

    STATUSES = [
        ("Open", "Open"),
        ("In Progress", "In Progress"),
        ("Resolved", "Resolved"),
        ("Closed", "Closed"),
    ]

    subject = models.TextField()
    message = models.TextField()
    page_url = models.TextField(null=True, blank=True)
    app_version = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Open")
    raised_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_tickets",
    )
    resolution = models.TextField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "support_tickets"
        ordering = ["-created_at"]


class ExportJob(TenantModel):
    """Backs ``POST /reports/{key}/export/`` -> ``{ downloadUrl }`` (api.md §12)."""

    STATUSES = [
        ("queued", "queued"),
        ("running", "running"),
        ("done", "done"),
        ("failed", "failed"),
    ]

    report_key = models.TextField()
    format = models.TextField(default="csv")
    params = models.JSONField(default=dict)
    status = models.TextField(choices=STATUSES, default="queued")
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    error = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "export_jobs"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# db.md / PDF §16 -- Approval Engine
# ---------------------------------------------------------------------------
class ApprovalRule(TenantModel):
    """Generic approval rule per document type, threshold condition, and approver role."""

    DOCUMENT_TYPES = [
        ("purchase_order_rate", "Purchase order rate change / excess qty"),
        ("jobwork_order_rate", "Job-work rate change"),
        ("jobwork_wastage", "Job-work / Process loss above tolerance"),
        ("reprocess_order", "Reprocess order / write-off"),
        ("discount_limit", "Sales discount above authority"),
        ("below_margin_sale", "Sale below minimum margin / landed cost"),
        ("credit_limit_exceeded", "Credit limit or credit days exceeded"),
        ("stock_adjustment", "Manual stock adjustment"),
        ("brokerage_rate_change", "Brokerage % change"),
        ("incentive_adjustment", "Incentive manual adjustment"),
        ("standalone_note", "Debit / Credit note"),
        ("vendor_bill_mismatch", "Vendor bill three-way mismatch"),
        ("production_final_po", "Final PO generation after production verification"),
    ]

    CONDITION_TYPES = [
        ("always", "Always require approval"),
        ("threshold_amount", "Amount exceeds threshold"),
        ("threshold_percent", "Percentage exceeds threshold"),
        ("variance_percent", "Variance % exceeds tolerance"),
    ]

    document_type = models.CharField(max_length=64, choices=DOCUMENT_TYPES)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    condition_type = models.CharField(max_length=32, choices=CONDITION_TYPES, default="always")
    threshold_value = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    approver_role = models.ForeignKey(
        "accounts.Role", null=True, blank=True, on_delete=models.SET_NULL, related_name="approval_rules"
    )
    approver_role_code = models.CharField(max_length=64, blank=True, default="")
    min_level = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "approval_rules"
        ordering = ["document_type", "min_level"]

    def __str__(self):
        return f"{self.name} ({self.document_type})"


class ApprovalRequest(TenantModel):
    """Instance of a document or sensitive action waiting for authorization."""

    STATUSES = [
        ("Pending", "Pending"),
        ("Approved", "Approved"),
        ("Rejected", "Rejected"),
        ("Returned", "Returned for Correction"),
        ("Cancelled", "Cancelled"),
    ]

    URGENCIES = [
        ("Normal", "Normal"),
        ("High", "High"),
        ("Urgent", "Urgent"),
    ]

    rule = models.ForeignKey(
        ApprovalRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests"
    )
    document_type = models.CharField(max_length=64)
    document_id = models.CharField(max_length=64, db_index=True)
    document_number = models.CharField(max_length=128, blank=True, default="")
    requested_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_approval_requests",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()
    old_values = models.JSONField(default=dict, blank=True)
    new_values = models.JSONField(default=dict, blank=True)
    variance_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    variance_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=32, choices=STATUSES, default="Pending", db_index=True)
    urgency = models.CharField(max_length=16, choices=URGENCIES, default="Normal")
    decided_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decided_approval_requests",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    comments = models.TextField(blank=True, default="")

    class Meta:
        db_table = "approval_requests"
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["client", "status", "-requested_at"], name="ix_appr_req_status"),
            models.Index(fields=["client", "document_type", "document_id"], name="ix_appr_req_doc"),
        ]

    def __str__(self):
        return f"{self.document_type} {self.document_number} - {self.status}"


# ---------------------------------------------------------------------------
# db.md / PDF §19 -- Alerts & Exception Management
# ---------------------------------------------------------------------------
class AlertSetting(TenantModel):
    """Configurable thresholds for exception alarms."""

    alert_key = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    threshold_days = models.IntegerField(default=0)
    threshold_pct = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    threshold_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    is_enabled = models.BooleanField(default=True)
    channels = models.JSONField(default=list)  # e.g. ["in_app", "email", "whatsapp"]

    class Meta:
        db_table = "alert_settings"
        constraints = [
            models.UniqueConstraint(fields=["client", "alert_key"], name="uq_alert_setting_key")
        ]

    def __str__(self):
        return f"{self.name} ({'Enabled' if self.is_enabled else 'Disabled'})"


class AlertIncident(TenantModel):
    """An alert triggered by the scheduler or on-save checks."""

    SEVERITIES = [
        ("Info", "Info"),
        ("Warning", "Warning"),
        ("Critical", "Critical"),
    ]

    STATUSES = [
        ("Active", "Active"),
        ("Acknowledged", "Acknowledged"),
        ("Resolved", "Resolved"),
    ]

    alert_key = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=16, choices=SEVERITIES, default="Warning")
    title = models.CharField(max_length=255)
    message = models.TextField()
    entity_type = models.CharField(max_length=64, blank=True, default="")
    entity_id = models.CharField(max_length=64, blank=True, default="")
    entity_number = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=32, choices=STATUSES, default="Active", db_index=True)
    acknowledged_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="acknowledged_alerts",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "alert_incidents"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client", "status", "-created_at"], name="ix_alert_inc_status"),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.title} - {self.status}"

