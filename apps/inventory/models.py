"""
Inventory (db.md §7, api.md §7).

``stock_movements`` is the ledger and the only source of truth for stock.
It is **append-only**: no UPDATE, no DELETE. Corrections are new reversing
movements linked through ``original_movement`` / ``reversal_movement``.
"""
from django.db import models
from django.utils import timezone

from apps.core.models import LegacyIdMixin, TenantModel

#: api.md §7.2 -- the full movement vocabulary.
MOVEMENT_TYPES = [
    ("PURCHASE", "PURCHASE"),
    ("PURCHASE_RETURN", "PURCHASE_RETURN"),
    ("PURCHASE_REVERSAL", "PURCHASE_REVERSAL"),
    ("SALE", "SALE"),
    ("SALE_REVERSAL", "SALE_REVERSAL"),
    ("SALES_RETURN", "SALES_RETURN"),
    ("RETURN_CANCELLATION", "RETURN_CANCELLATION"),
    ("TRANSFER_IN", "TRANSFER_IN"),
    ("TRANSFER_OUT", "TRANSFER_OUT"),
    ("ADJUSTMENT", "ADJUSTMENT"),
    ("FAULTY", "FAULTY"),
    ("SERVICE_USAGE", "SERVICE_USAGE"),
    ("ZONE_ISSUE", "ZONE_ISSUE"),
]

#: Movements that reverse an earlier one, keyed to what they reverse.
REVERSAL_OF = {
    "SALE_REVERSAL": "SALE",
    "PURCHASE_REVERSAL": "PURCHASE",
    "RETURN_CANCELLATION": "SALES_RETURN",
}


class StockMovement(TenantModel, LegacyIdMixin):
    """The append-only stock ledger (db.md §7.1).

    ``weighed_qty`` takes precedence over ``quantity`` for weight-tracked items
    -- not a nicety. ``calculateItemStock`` already prefers it, and the movement
    must record the same number the UI showed or stock diverges on the first
    steel receipt.
    """

    item = models.ForeignKey(
        "masters.Item", on_delete=models.PROTECT, related_name="movements"
    )
    location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="movements"
    )
    type = models.TextField(choices=MOVEMENT_TYPES)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)  # signed: + in, - out
    #: The item's stock unit at posting time. ``quantity`` is always in this
    #: unit -- document lines in another unit are converted before posting.
    uom = models.TextField(null=True, blank=True)
    weighed_qty = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    reference_type = models.TextField(null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    reference_number = models.TextField(null=True, blank=True)
    source_document_type = models.TextField(null=True, blank=True)
    source_document_id = models.UUIDField(null=True, blank=True)

    original_movement = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reversal_movement = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    batch_number = models.TextField(null=True, blank=True)
    movement_date = models.DateField()
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "stock_movements"
        ordering = ["-movement_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(quantity=0), name="ck_movement_nonzero"
            ),
            # api.md §7.2 -- every movement carries a reference document; orphan
            # movements are allowed only for ADJUSTMENT, and only with a reason.
            models.CheckConstraint(
                condition=models.Q(reference_id__isnull=False) | models.Q(type="ADJUSTMENT"),
                name="ck_movement_reference",
            ),
            models.CheckConstraint(
                condition=~models.Q(type="ADJUSTMENT") | models.Q(notes__isnull=False),
                name="ck_movement_adjustment_reason",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "item", "location", "movement_date"],
                name="ix_movements_item",
            ),
            models.Index(
                fields=["client", "reference_type", "reference_id"], name="ix_movements_ref"
            ),
            models.Index(fields=["client", "-movement_date"], name="ix_movements_date"),
        ]

    def __str__(self):
        return f"{self.type} {self.quantity} of {self.item_id}"

    @property
    def effective_quantity(self):
        """``coalesce(weighed_qty, quantity)`` -- the weight-item rule (db.md §7.2)."""
        return self.weighed_qty if self.weighed_qty is not None else self.quantity


class StockTransfer(TenantModel, LegacyIdMixin):
    """Posts a ``TRANSFER_OUT``/``TRANSFER_IN`` pair sharing a reference."""

    STATUSES = [
        ("Requested", "Requested"),
        ("In Transit", "In Transit"),
        ("Received", "Received"),
        ("Completed", "Completed"),
        ("Cancelled", "Cancelled"),
    ]

    transfer_number = models.TextField()
    from_location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="transfers_in"
    )
    transfer_date = models.DateField()
    status = models.TextField(choices=STATUSES, default="Requested")
    requested_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    shipped_by = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "stock_transfers"
        ordering = ["-transfer_date", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_location=models.F("to_location")),
                name="ck_transfer_locations",
            ),
            models.UniqueConstraint(
                fields=["client", "transfer_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_transfer_number",
            ),
        ]

    def __str__(self):
        return self.transfer_number


class StockTransferLine(TenantModel):
    stock_transfer = models.ForeignKey(
        StockTransfer, on_delete=models.CASCADE, related_name="items"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(max_digits=18, decimal_places=4)
    received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = "stock_transfer_lines"
        constraints = [
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_stl_qty")
        ]


class FaultyPart(TenantModel, LegacyIdMixin):
    """RMA register. ``timeline[]`` in the JSON is derived from status history."""

    STATUSES = [
        ("Reported", "Reported"),
        ("Pending Action", "Pending Action"),
        ("Sent for Replacement", "Sent for Replacement"),
        ("Replaced", "Replaced"),
        ("Credited", "Credited"),
        ("Closed", "Closed"),
    ]

    rma_number = models.TextField()
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    serial = models.ForeignKey(
        "masters.ItemSerial", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=1)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reported_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reported_date = models.DateField()
    fault_description = models.TextField(null=True, blank=True)
    vendor = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.TextField(choices=STATUSES, default="Reported")
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "faulty_parts"
        ordering = ["-reported_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "rma_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_rma_number",
            )
        ]

    def __str__(self):
        return self.rma_number


class ServiceUsage(TenantModel, LegacyIdMixin):
    """Parts consumed on service jobs. Posts a ``SERVICE_USAGE`` movement."""

    ticket_number = models.TextField()
    job_reference = models.TextField(null=True, blank=True)
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    serial = models.ForeignKey(
        "masters.ItemSerial", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    technician = models.TextField(null=True, blank=True)
    used_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    used_on = models.DateField()
    warranty_card = models.ForeignKey(
        "sales.WarrantyCard", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    chargeable = models.BooleanField(default=False)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "service_usage"
        ordering = ["-used_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "ticket_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_service_usage_ticket",
            )
        ]


class ZoneRequest(TenantModel, LegacyIdMixin):
    """Shop-floor material requests. Only ``Fulfilled`` issues stock."""

    STATUSES = [
        ("Requested", "Requested"),
        ("Approved", "Approved"),
        ("Fulfilled", "Fulfilled"),
        ("Rejected", "Rejected"),
    ]

    request_number = models.TextField()
    zone_location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="zone_requests"
    )
    target_sector = models.TextField(null=True, blank=True)
    requested_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_by_name = models.TextField(null=True, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    request_date = models.DateField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Requested")
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    issued_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reject_reason = models.TextField(null=True, blank=True)
    manager_signoff_needed = models.BooleanField(default=False)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "zone_requests"
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "request_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_zone_request_number",
            )
        ]

    def __str__(self):
        return self.request_number


class ZoneRequestLine(TenantModel):
    zone_request = models.ForeignKey(
        ZoneRequest, on_delete=models.CASCADE, related_name="lines"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    requested_qty = models.DecimalField(max_digits=18, decimal_places=4)
    issued_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = "zone_request_lines"


class StockAudit(TenantModel, LegacyIdMixin):
    """Month-end physical audits.

    Posting an audit turns every non-zero variance into an ``ADJUSTMENT``
    movement and stamps ``adjustment_movement`` -- that back-link is what makes
    a variance explainable six months later (db.md §7.4).
    """

    STATUSES = [
        ("Draft", "Draft"),
        ("Counting", "Counting"),
        ("Posted", "Posted"),
        ("Cancelled", "Cancelled"),
    ]

    audit_number = models.TextField()
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    period_month = models.DateField()  # first of month
    status = models.TextField(choices=STATUSES, default="Draft")
    conducted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "stock_audits"
        ordering = ["-period_month"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "audit_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_audit_number",
            )
        ]

    def __str__(self):
        return self.audit_number


class StockAuditLine(TenantModel):
    stock_audit = models.ForeignKey(StockAudit, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    system_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    counted_qty = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    #: db.md §7.4 declares this a stored generated column; Django expresses the
    #: same thing as a GeneratedField so the value is still computed by Postgres.
    variance = models.GeneratedField(
        expression=models.F("counted_qty") - models.F("system_qty"),
        output_field=models.DecimalField(max_digits=18, decimal_places=4),
        db_persist=True,
    )
    reason = models.TextField(null=True, blank=True)
    adjustment_movement = models.ForeignKey(
        StockMovement, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "stock_audit_lines"


class QualityStandard(TenantModel):
    """Per-category QC checklists (api.md §6.6)."""

    category = models.ForeignKey(
        "masters.ItemCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quality_standards",
    )
    name = models.TextField()
    checklist = models.JSONField(default=list, blank=True)
    tolerance_pct = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "quality_standards"
        ordering = ["name"]

    def __str__(self):
        return self.name


class StockBalance(TenantModel):
    """Incrementally-maintained balances (db.md §7.2, Open decision 2).

    db.md offers a matview refreshed every 1-5 minutes or a trigger-maintained
    table, and notes the table as the upgrade when stock tiles must be exact.
    This is the table: the posting service updates it in the same transaction
    as the movement, so a finalized invoice and the stock tile can never
    disagree. The matview definition survives as the nightly reconciliation
    reference (db.md §13, ``reconcile_stock``).
    """

    item = models.ForeignKey(
        "masters.Item", on_delete=models.CASCADE, related_name="balances"
    )
    location = models.ForeignKey(
        "masters.Location", on_delete=models.CASCADE, related_name="balances"
    )
    on_hand = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    damaged = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    inward_value = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    inward_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = "stock_balances"
        constraints = [
            models.UniqueConstraint(
                fields=["client", "item", "location"], name="uq_stock_balances"
            )
        ]
        indexes = [models.Index(fields=["client", "item"], name="ix_stock_balances_item")]

    @property
    def weighted_average_cost(self):
        """WAC from movement unit costs (db.md Appendix B, decision 5)."""
        if self.inward_qty and self.inward_qty > 0:
            return self.inward_value / self.inward_qty
        return None


class LotTaka(TenantModel, LegacyIdMixin):
    """Fabric Lot and Taka/Roll register with movement history."""

    lot_number = models.TextField(db_index=True)
    taka_number = models.TextField(default="1")
    item = models.ForeignKey(
        "masters.Item", on_delete=models.PROTECT, related_name="lot_takas"
    )
    supplier = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_document_type = models.TextField(default="GRN")
    source_document_number = models.TextField(null=True, blank=True)
    source_document_id = models.UUIDField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="+"
    )
    received_date = models.DateField(default=timezone.localdate)
    received_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    available_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    uom = models.TextField(null=True, blank=True)
    qc_status = models.TextField(default="Passed")  # Passed, Pending, On Hold, Reprocess, Rejected
    qc_notes = models.TextField(null=True, blank=True)
    status = models.TextField(default="Active")  # Active, Closed
    barcode = models.TextField(null=True, blank=True)
    roll_weight = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    width = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    pieces = models.IntegerField(default=1)
    gsm = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    remarks = models.TextField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    image_url = models.TextField(null=True, blank=True)
    stock_movement = models.ForeignKey(
        "inventory.StockMovement", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "lot_takas"
        ordering = ["-received_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "lot_number", "taka_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_client_lot_taka",
            )
        ]

    def __str__(self):
        return f"{self.lot_number} - Taka {self.taka_number}"

