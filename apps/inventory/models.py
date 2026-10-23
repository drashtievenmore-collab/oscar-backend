"""
Inventory (db.md §7, api.md §7).

``stock_movements`` is the ledger and the only source of truth for stock.
It is **append-only**: no UPDATE, no DELETE. Corrections are new reversing
movements linked through ``original_movement`` / ``reversal_movement``.
"""
from django.conf import settings as django_settings
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
    ("ZONE_RETURN", "ZONE_RETURN"),
    ("SAMPLE_ISSUE", "SAMPLE_ISSUE"),
    # A returned sample is routed by its condition (Phase 1.4): Good goes back
    # to stock, the rest to the QC Hold / Reprocess / Second Quality / Reject
    # system locations.
    ("SAMPLE_RETURN", "SAMPLE_RETURN"),
    ("SAMPLE_RETURN_HOLD", "SAMPLE_RETURN_HOLD"),
    ("SAMPLE_RETURN_REPROCESS", "SAMPLE_RETURN_REPROCESS"),
    ("SAMPLE_RETURN_SECOND_QUALITY", "SAMPLE_RETURN_SECOND_QUALITY"),
    ("SAMPLE_RETURN_REJECT", "SAMPLE_RETURN_REJECT"),
    ("JOBWORK_OUTWARD", "JOBWORK_OUTWARD"),
    ("JOBWORK_INWARD", "JOBWORK_INWARD"),
    ("QC_PASS", "QC_PASS"),
    ("QC_HOLD", "QC_HOLD"),
    ("QC_REPROCESS", "QC_REPROCESS"),
    ("QC_REJECT", "QC_REJECT"),
    ("QC_SECOND_QUALITY", "QC_SECOND_QUALITY"),
    ("SALES_RETURN_GOOD", "SALES_RETURN_GOOD"),
    ("SALES_RETURN_HOLD", "SALES_RETURN_HOLD"),
    ("SALES_RETURN_REPROCESS", "SALES_RETURN_REPROCESS"),
    ("SALES_RETURN_SECOND_QUALITY", "SALES_RETURN_SECOND_QUALITY"),
    ("SALES_RETURN_REJECT", "SALES_RETURN_REJECT"),
    # Several small lots combined into one processing batch (PDF §06 lot
    # merge): out of each source taka, into the merged taka, same godown.
    ("LOT_MERGE_OUT", "LOT_MERGE_OUT"),
    ("LOT_MERGE_IN", "LOT_MERGE_IN"),
]

#: Movements that reverse an earlier one, keyed to what they reverse.
REVERSAL_OF = {
    "SALE_REVERSAL": "SALE",
    "PURCHASE_REVERSAL": "PURCHASE",
    "RETURN_CANCELLATION": "SALES_RETURN",
}

#: Condition-routed sales returns are cancelled with RETURN_CANCELLATION too.
SALES_RETURN_TYPES = (
    "SALES_RETURN",
    "SALES_RETURN_GOOD",
    "SALES_RETURN_HOLD",
    "SALES_RETURN_REPROCESS",
    "SALES_RETURN_SECOND_QUALITY",
    "SALES_RETURN_REJECT",
)

#: Movements undone by posting the *same* type with the opposite sign (linked
#: through ``original_movement``) -- relocations between two locations have no
#: separate reversal vocabulary.
SELF_REVERSING_TYPES = (
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "QC_PASS",
    "QC_HOLD",
    "QC_REPROCESS",
    "QC_REJECT",
    "QC_SECOND_QUALITY",
    "SAMPLE_ISSUE",
    "SAMPLE_RETURN",
    "SAMPLE_RETURN_HOLD",
    "SAMPLE_RETURN_REPROCESS",
    "SAMPLE_RETURN_SECOND_QUALITY",
    "SAMPLE_RETURN_REJECT",
    "ZONE_RETURN",
)

#: Valid values of ``LotTaka.qc_status`` (kept as-is for Phase 1).
LOT_QC_STATUSES = ("Passed", "Pending", "On Hold", "Reprocess", "Rejected")


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
    lot_taka = models.ForeignKey(
        "inventory.LotTaka", null=True, blank=True, on_delete=models.SET_NULL, related_name="movements"
    )
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
    #: The takas selected for this line: ``[{"lotTakaId": uuid, "qty": "12.5"}]``.
    #: A taka may be moved partly; dispatch splits it.
    takas = models.JSONField(default=list, blank=True)

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

    taka_id = models.TextField(null=True, blank=True, db_index=True)
    lot_number = models.TextField(db_index=True)
    taka_number = models.TextField(default="1")
    item = models.ForeignKey(
        "masters.Item", on_delete=models.PROTECT, related_name="lot_takas"
    )
    fabric_quality = models.ForeignKey(
        "masters.FabricQuality", null=True, blank=True, on_delete=models.SET_NULL, related_name="lot_takas"
    )
    design = models.ForeignKey(
        "masters.Design", null=True, blank=True, on_delete=models.SET_NULL, related_name="lot_takas"
    )
    shade = models.ForeignKey(
        "masters.Shade", null=True, blank=True, on_delete=models.SET_NULL, related_name="lot_takas"
    )
    parent_taka = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="child_takas"
    )
    parent_lots = models.ManyToManyField(
        "self", blank=True, symmetrical=False, related_name="derived_lots"
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
    STATUS_AVAILABLE = "Available"
    STATUS_RESERVED = "Reserved"
    STATUS_EXTERNAL_WIP = "External WIP"
    STATUS_QC_HOLD = "QC Hold"
    STATUS_REPROCESS = "Reprocess"
    STATUS_FINISHED = "Finished"
    STATUS_SECOND_QUALITY = "Second Quality"
    STATUS_REJECT = "Reject"
    STATUS_SAMPLE = "Sample"
    STATUS_DISPATCHED = "Dispatched"
    STATUS_CUSTOMER_RETURN = "Customer Return"

    STATUS_CHOICES = [
        ("Available", "Available"),
        ("Reserved", "Reserved"),
        ("External WIP", "External WIP"),
        ("QC Hold", "QC Hold"),
        ("Reprocess", "Reprocess"),
        ("Finished", "Finished"),
        ("Second Quality", "Second Quality"),
        ("Reject", "Reject"),
        ("Sample", "Sample"),
        ("Dispatched", "Dispatched"),
        ("Customer Return", "Customer Return"),
        ("Active", "Active"),  # backward compat
    ]

    qc_status = models.TextField(default="Passed")  # Passed, Pending, On Hold, Reprocess, Rejected
    qc_notes = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUS_CHOICES, default="Available")
    barcode = models.TextField(null=True, blank=True, db_index=True)
    tag_status = models.TextField(default="Pending")  # Pending, Printed, Reprinted
    print_count = models.IntegerField(default=0)
    last_printed_at = models.DateTimeField(null=True, blank=True)
    last_printed_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
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
    #: The document line this lot/taka was received on (e.g. a goods-receipt
    #: line), so a multi-line GRN can be dispositioned line by line.
    source_line_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "lot_takas"
        ordering = ["-received_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "lot_number", "taka_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_client_lot_taka",
            ),
            models.UniqueConstraint(
                fields=["client", "taka_id"],
                condition=models.Q(deleted_at__isnull=True, taka_id__isnull=False),
                name="uq_client_taka_id",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "taka_id"], name="ix_lot_takas_taka_id"),
            models.Index(fields=["client", "barcode"], name="ix_lot_takas_barcode"),
            models.Index(fields=["client", "tag_status"], name="ix_lot_takas_tag_status"),
        ]

    def __str__(self):
        return f"{self.taka_id or self.lot_number} - Taka {self.taka_number}"


class TakaTagPrintLog(TenantModel):
    """Audit history of label prints and reprints."""

    lot_taka = models.ForeignKey(
        LotTaka, on_delete=models.CASCADE, related_name="print_logs"
    )
    action_type = models.TextField(default="PRINT")  # PRINT, REPRINT, BULK_PRINT
    printed_at = models.DateTimeField(default=timezone.now)
    printed_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    printed_by_name = models.TextField(null=True, blank=True)
    taka_id = models.TextField()
    lot_number = models.TextField()
    taka_number = models.TextField()
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "taka_tag_print_logs"
        ordering = ["-printed_at"]

    def __str__(self):
        return f"{self.action_type} for {self.taka_id} at {self.printed_at}"


class QCInspection(TenantModel, LegacyIdMixin):
    """Piece-by-piece fabric inspection register header (PDF §08)."""

    STATUSES = [
        ("Draft", "Draft"),
        ("Pending", "Pending"),
        ("Approved", "Approved"),
        ("Partially Approved", "Partially Approved"),
        ("On Hold", "On Hold"),
        ("Rework", "Rework"),
        ("Rejected", "Rejected"),
    ]

    SOURCE_TYPES = [
        ("GRN", "GRN"),
        ("JWO_INWARD", "Job Work Inward"),
        ("SALES_RETURN", "Sales Return"),
        ("INTERNAL", "Internal / Manual"),
    ]

    inspection_number = models.TextField(db_index=True)
    source_document_type = models.TextField(choices=SOURCE_TYPES, default="GRN")
    source_document_id = models.UUIDField(null=True, blank=True)
    source_document_number = models.TextField(null=True, blank=True)
    inspection_date = models.DateField(default=timezone.localdate)
    inspector = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    inspector_employee = models.ForeignKey(
        "hrms.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    shift = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Pending")
    total_received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    total_inspected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    passed_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    second_quality_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    reprocess_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rejected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    hold_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    notes = models.TextField(null=True, blank=True)
    purchase_order = models.ForeignKey(
        "purchase.PurchaseOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="qc_inspections"
    )
    production_instruction = models.ForeignKey(
        "pms.ProductionInstruction", null=True, blank=True, on_delete=models.SET_NULL, related_name="qc_inspections"
    )
    fabric_item = models.ForeignKey(
        "masters.Item", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    inspection_type = models.TextField(default="Grey Fabric QC")
    inspection_method = models.TextField(default="Visual + Measurement")
    gsm = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    width = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    defects_found = models.TextField(null=True, blank=True)
    quality_grade = models.TextField(default="Grade A", null=True, blank=True)
    photos = models.JSONField(default=list, blank=True)
    previous_inspection = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reinspections"
    )

    class Meta:
        db_table = "qc_inspections"
        ordering = ["-inspection_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "inspection_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_qc_inspection_number",
            )
        ]

    def __str__(self):
        return f"{self.inspection_number} ({self.status})"


class QCInspectionLine(TenantModel):
    """Piece-by-piece taka inspection line with 4-point calculation (PDF §08)."""

    DISPOSITIONS = [
        ("Pass", "Pass"),
        ("Hold", "Hold"),
        ("Reprocess", "Reprocess"),
        ("Reject", "Reject"),
        ("Second Quality", "Second Quality"),
    ]

    inspection = models.ForeignKey(
        QCInspection, on_delete=models.CASCADE, related_name="lines"
    )
    lot_taka = models.ForeignKey(
        LotTaka, null=True, blank=True, on_delete=models.SET_NULL, related_name="inspection_lines"
    )
    taka_number = models.TextField(default="1")
    lot_number = models.TextField(null=True, blank=True)
    original_meter = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    actual_meter = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    roll_weight = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    width = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    gsm = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    grade = models.TextField(default="A")  # A, B, C, Second
    penalty_points = models.IntegerField(default=0)
    points_per_100_sq_meter = models.DecimalField(max_digits=9, decimal_places=2, default=0)
    disposition = models.TextField(choices=DISPOSITIONS, default="Pass")
    remarks = models.TextField(null=True, blank=True)
    photo_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "qc_inspection_lines"
        ordering = ["created_at"]


class QCInspectionLineDefect(TenantModel):
    """M2M Defect logging per taka (PDF §08)."""

    inspection_line = models.ForeignKey(
        QCInspectionLine, on_delete=models.CASCADE, related_name="defects"
    )
    defect = models.ForeignKey(
        "masters.DefectMaster", on_delete=models.PROTECT, related_name="+"
    )
    defect_count = models.IntegerField(default=1)
    points = models.IntegerField(default=1)
    distance_meter = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "qc_inspection_line_defects"
        ordering = ["created_at"]


class LotCostEntry(TenantModel, LegacyIdMixin):
    """Landed cost ledger per lot/taka (PDF §09)."""

    COST_TYPES = [
        ("Grey purchase", "Grey purchase"),
        ("Purchase freight/loading", "Purchase freight/loading"),
        ("Dyeing/Printing/Finishing", "Dyeing/Printing/Finishing"),
        ("Processing transport", "Processing transport"),
        ("Reprocess", "Reprocess"),
        ("Other direct", "Other direct"),
        ("Scrap recovery", "Scrap recovery"),
    ]

    lot_taka = models.ForeignKey(
        "inventory.LotTaka", null=True, blank=True, on_delete=models.SET_NULL, related_name="cost_entries"
    )
    lot_number = models.TextField(db_index=True)
    cost_type = models.TextField(choices=COST_TYPES)
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    cost_per_meter = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    source_document_type = models.TextField(default="MANUAL")
    source_document_id = models.UUIDField(null=True, blank=True)
    source_document_number = models.TextField(null=True, blank=True)
    date = models.DateField(default=timezone.localdate)
    posted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_approved = models.BooleanField(default=True)
    approval_status = models.TextField(default="Approved")  # Approved, Pending, Rejected
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "lot_cost_entries"
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["client", "lot_number"], name="ix_lot_cost_lot_num"),
            models.Index(fields=["client", "lot_taka"], name="ix_lot_cost_taka"),
        ]

    def __str__(self):
        return f"{self.lot_number} - {self.cost_type}: {self.amount}"


class Sample(TenantModel, LegacyIdMixin):
    """Sampling management with stock movements & expense booking (PDF §10)."""

    STATUSES = [
        ("Issued", "Issued"),
        ("Returned", "Returned"),
        ("Partially Returned", "Partially Returned"),
        ("Consumed", "Consumed"),
        ("Overdue", "Overdue"),
    ]
    SAMPLE_TYPES = [
        ("CUT_PIECE", "Cut Piece"),
        ("FULL_TAKA", "Full Taka"),
        ("SAMPLE_FOLDER", "Sample Folder / Hanger"),
    ]

    sample_number = models.TextField(db_index=True)
    sample_date = models.DateField(default=timezone.localdate)
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="samples"
    )
    salesperson = models.ForeignKey(
        "hrms.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="samples"
    )
    broker = models.ForeignKey(
        "masters.Broker", null=True, blank=True, on_delete=models.SET_NULL, related_name="samples"
    )
    source_lot_taka = models.ForeignKey(
        "inventory.LotTaka", null=True, blank=True, on_delete=models.SET_NULL, related_name="samples"
    )
    fabric_quality = models.ForeignKey(
        "masters.FabricQuality", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    design = models.ForeignKey(
        "masters.Design", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    shade = models.ForeignKey(
        "masters.Shade", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sample_type = models.TextField(choices=SAMPLE_TYPES, default="CUT_PIECE")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=1)
    uom = models.TextField(default="MTR")
    is_returnable = models.BooleanField(default=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Issued")
    returned_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    consumed_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    estimated_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    photo_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    issued_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "inventory_samples"
        ordering = ["-sample_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "sample_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_inventory_sample_number",
            )
        ]

    def __str__(self):
        return f"{self.sample_number} ({self.status})"


class Trip(TenantModel, LegacyIdMixin):
    """Logistics / Transport / Tempo trip management (PDF §13)."""

    TRIP_TYPES = [
        ("INWARD", "Inward / Purchase"),
        ("OUTWARD", "Outward / Sales Dispatch"),
        ("JOBWORK_OUT", "Job Work Outward"),
        ("JOBWORK_IN", "Job Work Inward"),
        ("INTERNAL", "Internal Transfer"),
    ]
    STATUSES = [
        ("Draft", "Draft"),
        ("Loaded", "Loaded"),
        ("In Transit", "In Transit"),
        ("Delivered", "Delivered"),
        ("Cancelled", "Cancelled"),
    ]
    PAID_BY_CHOICES = [
        ("Company", "Company"),
        ("Party", "Party"),
        ("Transporter", "Transporter"),
    ]
    COST_ALLOCATION_CHOICES = [
        ("LOT", "Allocate to Lots"),
        ("SALES_ORDER", "Allocate to Sales Order"),
        ("EXPENSE", "General Freight Expense"),
    ]

    trip_number = models.TextField(db_index=True)
    trip_date = models.DateField(default=timezone.localdate)
    from_location = models.TextField()
    to_location = models.TextField()
    transporter = models.ForeignKey(
        "masters.Transporter", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips"
    )
    transporter_party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="transporter_trips"
    )
    vehicle = models.ForeignKey(
        "masters.Vehicle", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips"
    )
    driver = models.ForeignKey(
        "masters.Driver", null=True, blank=True, on_delete=models.SET_NULL, related_name="trips"
    )
    vehicle_number = models.TextField()
    driver_name = models.TextField(null=True, blank=True)
    driver_mobile = models.TextField(null=True, blank=True)
    lr_number = models.TextField(null=True, blank=True)
    lr_date = models.DateField(null=True, blank=True)
    trip_type = models.TextField(choices=TRIP_TYPES, default="OUTWARD")
    status = models.TextField(choices=STATUSES, default="Draft")

    freight_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    loading_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    unloading_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_charges = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    paid_by = models.TextField(choices=PAID_BY_CHOICES, default="Company")
    cost_allocation_target = models.TextField(choices=COST_ALLOCATION_CHOICES, default="LOT")
    is_cost_allocated = models.BooleanField(default=False)

    dispatch_time = models.DateTimeField(null=True, blank=True)
    delivery_time = models.DateTimeField(null=True, blank=True)
    proof_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "transport_trips"
        ordering = ["-trip_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "trip_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_transport_trip_number",
            )
        ]

    def __str__(self):
        return f"{self.trip_number} ({self.vehicle_number})"


class TripMaterialLine(TenantModel):
    """Line item in a transport trip (PDF §13)."""

    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="materials")
    lot_taka = models.ForeignKey(
        LotTaka, null=True, blank=True, on_delete=models.SET_NULL, related_name="trip_materials"
    )
    lot_number = models.TextField(null=True, blank=True)
    taka_number = models.TextField(null=True, blank=True)
    item = models.ForeignKey(
        "masters.Item", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    roll_weight = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    document_reference_type = models.TextField(null=True, blank=True)
    document_reference_id = models.UUIDField(null=True, blank=True)
    document_reference_number = models.TextField(null=True, blank=True)
    allocated_freight = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = "transport_trip_materials"
        ordering = ["created_at"]





class GreyStockAllocation(TenantModel):
    """General grey stock earmarked for a project (a reservation, not a move).

    The fabric stays where it is and ``LotTaka.available_quantity`` -- which
    mirrors the movement ledger -- is untouched. Unallocated stock is
    ``available_quantity`` minus the Active allocations on the lot, and the
    allocation service locks the lot row so two allocations can never take
    the same metres.
    """

    STATUS_ACTIVE = "Active"
    STATUS_RELEASED = "Released"
    STATUSES = [(STATUS_ACTIVE, STATUS_ACTIVE), (STATUS_RELEASED, STATUS_RELEASED)]

    project = models.ForeignKey(
        "pms.Project", on_delete=models.PROTECT, related_name="grey_stock_allocations"
    )
    lot_taka = models.ForeignKey(
        LotTaka, on_delete=models.PROTECT, related_name="project_allocations"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default=STATUS_ACTIVE)
    remarks = models.TextField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        db_table = "grey_stock_allocations"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="ck_grey_alloc_qty_positive"
            )
        ]
        indexes = [
            models.Index(fields=["client", "project", "status"], name="ix_grey_alloc_project"),
            models.Index(fields=["client", "lot_taka", "status"], name="ix_grey_alloc_lot"),
        ]

    def __str__(self):
        return f"{self.lot_taka} -> {self.project} ({self.quantity})"
