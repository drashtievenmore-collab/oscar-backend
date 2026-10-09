"""
Sales (db.md §5, api.md §5).

The pipeline, walkable both ways through nullable upstream FKs:

    Quotation -> Sales Order -> [Proforma] -> Challan -> Invoice -> Payment In
                                                |                       |
                                          Warranty Card           Sales Return

Estimates were removed product-wide: no Estimate/EstimateLine tables, no
/sales/estimates/ endpoints, no EST number series.
                                                |                       |
                                          Warranty Card           Sales Return

Line-level links matter as much as header links: ``sales_invoice_lines
.sales_order_line`` and ``delivery_challan_lines.sales_order_line`` are what
make partial conversion and double-depletion avoidance tractable (db.md §5.1).
"""
from django.db import models

from apps.core.documents import (
    DocumentHeader,
    DocumentLine,
    number_unique_constraint,
    total_check_constraint,
)
from apps.core.models import LegacyIdMixin, TenantModel

# --- api.md Appendix A -------------------------------------------------------
QUOTATION_STATUSES = [
    "Draft", "Sent", "Viewed", "Accepted", "Rejected", "Expired",
    "Confirmed", "Converted", "Invoiced", "Cancelled",
]
ORDER_STAGES = [
    "Draft", "Confirmed", "Packing", "Dispatched", "Delivered", "Invoiced", "Cancelled",
]
#: Stages whose open lines no longer reserve stock (api.md §5.4, db.md §5.2).
NON_RESERVING_STAGES = ["Delivered", "Invoiced", "Cancelled"]
PAYMENT_STATUSES = ["Unpaid", "Partially Paid", "Paid"]
PROFORMA_STATUSES = ["Draft", "Sent", "Accepted", "Converted", "Expired", "Cancelled"]
CHALLAN_STATUSES = ["Draft", "Dispatched", "In Transit", "Delivered", "Cancelled"]
INVOICE_STATUSES = ["Draft", "Unpaid", "Partially Paid", "Paid", "Cancelled"]
# The strings the UI's <select> actually emits (api.md §0: where code and
# spec disagree, the frontend code is the fact). PaymentInPage, PaymentOutPage,
# PurchaseBillsPage and SalesInvoicesView between them offer all of these.
PAYMENT_MODES = [
    "Cash", "Bank", "UPI", "Cheque", "Card",
    "Bank Transfer", "Bank Wire", "ACH", "Corporate Card",
]


def choices(values):
    return [(value, value) for value in values]


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class Quotation(DocumentHeader):
    quotation_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(QUOTATION_STATUSES), default="Draft")
    valid_until = models.DateField(null=True, blank=True)
    crm_deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    crm_lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    subject = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "quotations"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("quotations", "quotation_number"),
            total_check_constraint("quotations"),
        ]

    def __str__(self):
        return self.quotation_number or f"Quotation {self.id}"


class QuotationLine(DocumentLine):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="line_items")

    class Meta(DocumentLine.Meta):
        db_table = "quotation_lines"
        constraints = [
            models.UniqueConstraint(fields=["quotation", "line_no"], name="uq_quotation_line_no")
        ]


class QuotationShare(TenantModel):
    """db.md §5.3 -- replaces ``services/localQuotationSharing.js``.

    Tokens are opaque random bytes, hashed at rest, single-quotation, expiring
    and revocable. The base64 encoder they replace was a security placeholder.
    """

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="shares")
    token_hash = models.TextField(unique=True)
    recipients = models.JSONField(default=list, blank=True)
    channel = models.TextField(null=True, blank=True)  # email | whatsapp | link
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "quotation_shares"
        ordering = ["-created_at"]


class QuotationActivity(models.Model):
    """Every public view writes a row -- ``GET .../activity/`` reads them."""

    EVENTS = [
        ("sent", "sent"),
        ("viewed", "viewed"),
        ("opened", "opened"),
        ("accepted", "accepted"),
        ("rejected", "rejected"),
        ("commented", "commented"),
        ("revoked", "revoked"),
    ]

    id = models.BigAutoField(primary_key=True)
    quotation = models.ForeignKey(
        Quotation, on_delete=models.CASCADE, related_name="activity"
    )
    share = models.ForeignKey(
        QuotationShare, null=True, blank=True, on_delete=models.SET_NULL, related_name="activity"
    )
    event = models.TextField(choices=EVENTS)
    actor_label = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotation_activity"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrder(DocumentHeader):
    order_number = models.TextField(null=True, blank=True)
    stage = models.TextField(choices=choices(ORDER_STAGES), default="Draft")
    #: DERIVED from payment_allocations vs invoice totals (db.md §12).
    payment_status = models.TextField(choices=choices(PAYMENT_STATUSES), default="Unpaid")
    delivery_date = models.DateField(null=True, blank=True)
    quotation = models.ForeignKey(
        Quotation, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders"
    )
    pms_project = models.ForeignKey(
        "pms.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "sales_orders"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_orders", "order_number"),
            total_check_constraint("sales_orders"),
        ]
        indexes = [models.Index(fields=["client", "stage"], name="ix_sales_orders_stage")]

    def __str__(self):
        return self.order_number or f"Sales order {self.id}"

    @property
    def reserves_stock(self):
        return self.stage not in NON_RESERVING_STAGES and self.deleted_at is None


class SalesOrderLine(DocumentLine):
    sales_order = models.ForeignKey(
        SalesOrder, on_delete=models.CASCADE, related_name="line_items"
    )
    #: DERIVED from challan / invoice lines in the conversion transaction.
    dispatched_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    invoiced_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta(DocumentLine.Meta):
        db_table = "sales_order_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_order", "line_no"], name="uq_sales_order_line_no"
            ),
            # The database half of api.md §5.4's "over-dispatch or over-invoice
            # returns 422". The handler returns the friendly error; the
            # constraint guarantees no concurrent pair of requests slips past.
            models.CheckConstraint(
                condition=models.Q(dispatched_qty__lte=models.F("qty")),
                name="ck_sol_dispatch",
            ),
            models.CheckConstraint(
                condition=models.Q(invoiced_qty__lte=models.F("qty")), name="ck_sol_invoice"
            ),
        ]
        indexes = [
            models.Index(fields=["item", "sales_order"], name="ix_sales_order_lines_item")
        ]

    @property
    def pending_qty(self):
        return (self.qty or 0) - (self.dispatched_qty or 0)


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoice(DocumentHeader):
    """Moves no stock and posts no ledger entry -- advance collection only."""

    proforma_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(PROFORMA_STATUSES), default="Draft")
    valid_until = models.DateField(null=True, blank=True)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="proformas"
    )

    class Meta:
        db_table = "proforma_invoices"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("proforma_invoices", "proforma_number"),
            total_check_constraint("proforma_invoices"),
        ]


class ProformaInvoiceLine(DocumentLine):
    proforma_invoice = models.ForeignKey(
        ProformaInvoice, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(DocumentLine.Meta):
        db_table = "proforma_invoice_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["proforma_invoice", "line_no"], name="uq_proforma_line_no"
            )
        ]


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallan(DocumentHeader):
    """Dispatching posts ``SALE`` movements and consumes the selected serials."""

    challan_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(CHALLAN_STATUSES), default="Draft")
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="challans"
    )
    quotation = models.ForeignKey(
        Quotation, null=True, blank=True, on_delete=models.SET_NULL, related_name="challans"
    )
    dispatch_date = models.DateField(null=True, blank=True)
    vehicle_number = models.TextField(null=True, blank=True)
    transporter = models.TextField(null=True, blank=True)
    lr_number = models.TextField(null=True, blank=True)
    delivery_location = models.TextField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "delivery_challans"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("delivery_challans", "challan_number"),
            total_check_constraint("delivery_challans"),
        ]

    def __str__(self):
        return self.challan_number or f"Challan {self.id}"


class DeliveryChallanLine(DocumentLine):
    delivery_challan = models.ForeignKey(
        DeliveryChallan, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="challan_lines"
    )

    class Meta(DocumentLine.Meta):
        db_table = "delivery_challan_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_challan", "line_no"], name="uq_challan_line_no"
            )
        ]


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoice(DocumentHeader):
    """``Overdue`` is derived from ``due_date`` and outstanding at read time,
    never stored (api.md §5.7, db.md §12)."""

    invoice_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(INVOICE_STATUSES), default="Draft")
    due_date = models.DateField(null=True, blank=True)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    delivery_challan = models.ForeignKey(
        DeliveryChallan, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    proforma_invoice = models.ForeignKey(
        ProformaInvoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    irn = models.TextField(null=True, blank=True)
    eway_bill_number = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "sales_invoices"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_invoices", "invoice_number"),
            total_check_constraint("sales_invoices"),
        ]
        indexes = [
            models.Index(fields=["client", "status"], name="ix_sales_invoices_status"),
            models.Index(fields=["client", "-doc_date", "id"], name="ix_sales_invoices_date"),
        ]

    def __str__(self):
        return self.invoice_number or f"Invoice {self.id}"


class SalesInvoiceLine(DocumentLine):
    sales_invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoice_lines"
    )
    delivery_challan_line = models.ForeignKey(
        DeliveryChallanLine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_lines",
    )
    #: DERIVED -- how much of this line has come back on a credit note.
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta(DocumentLine.Meta):
        db_table = "sales_invoice_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_invoice", "line_no"], name="uq_invoice_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(returned_qty__lte=models.F("qty")), name="ck_sil_returned"
            ),
        ]


# ---------------------------------------------------------------------------
# Payments in and allocation (db.md §5.4)
# ---------------------------------------------------------------------------
class PaymentIn(TenantModel, LegacyIdMixin):
    payment_number = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="payments_in")
    payment_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    mode = models.TextField(choices=choices(PAYMENT_MODES), default="Bank")
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    #: DERIVED from payment_allocations. The unallocated remainder *is* the
    #: customer advance -- there is no separate balance column (db.md §5.4).
    allocated_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(
        choices=[("Active", "Active"), ("Cancelled", "Cancelled")], default="Active"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "payments_in"
        ordering = ["-payment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "payment_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_payments_in_number",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_payments_in_amount"),
            models.CheckConstraint(
                condition=models.Q(allocated_amount__lte=models.F("amount")),
                name="ck_payments_in_alloc",
            ),
        ]

    def __str__(self):
        return self.payment_number

    @property
    def unallocated_amount(self):
        return (self.amount or 0) - (self.allocated_amount or 0)


class PaymentAllocation(TenantModel):
    """One allocation table for both sides (db.md §5.4).

    Keeps outstanding calculations single-sourced: ``sales_invoices
    .amount_paid``, customer advances and vendor advances are all queries over
    this table rather than parallel balance columns.
    """

    SIDES = [("in", "in"), ("out", "out")]
    DOCUMENT_TYPES = [
        ("SalesInvoice", "SalesInvoice"),
        ("PurchaseBill", "PurchaseBill"),
        ("SalesReturn", "SalesReturn"),
        ("PurchaseReturn", "PurchaseReturn"),
    ]

    payment_id = models.UUIDField()
    payment_side = models.TextField(choices=SIDES)
    document_type = models.TextField(choices=DOCUMENT_TYPES)
    document_id = models.UUIDField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocated_at = models.DateTimeField(auto_now_add=True)
    allocated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "payment_allocations"
        constraints = [
            models.UniqueConstraint(
                fields=["payment_side", "payment_id", "document_type", "document_id"],
                name="uq_payment_allocations",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="ck_payment_alloc_amount"
            ),
        ]
        indexes = [
            models.Index(fields=["document_type", "document_id"], name="ix_payment_alloc_doc"),
            models.Index(fields=["payment_side", "payment_id"], name="ix_payment_alloc_payment"),
        ]


# ---------------------------------------------------------------------------
# Sales returns / credit notes (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturn(DocumentHeader):
    return_number = models.TextField(null=True, blank=True)
    credit_note_number = models.TextField(null=True, blank=True)
    status = models.TextField(
        choices=choices(["Draft", "Posted", "Cancelled"]), default="Posted"
    )
    sales_invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.PROTECT, related_name="returns"
    )
    reason = models.TextField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "sales_returns"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_returns", "return_number"),
            total_check_constraint("sales_returns"),
        ]


class SalesReturnLine(DocumentLine):
    sales_return = models.ForeignKey(
        SalesReturn, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_invoice_line = models.ForeignKey(
        SalesInvoiceLine, on_delete=models.PROTECT, related_name="return_lines"
    )
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta(DocumentLine.Meta):
        db_table = "sales_return_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_return", "line_no"], name="uq_sales_return_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(returned_qty__gt=0), name="ck_srl_returned_qty"
            ),
        ]


# ---------------------------------------------------------------------------
# Warranty cards (api.md §5.10)
# ---------------------------------------------------------------------------
class WarrantyCard(TenantModel, LegacyIdMixin):
    """Two independent status fields -- do not collapse them (api.md §5.10).

    ``document_status`` is set by user action. ``coverageStatus`` is **derived**
    on every read from the dates plus the document status, so it is not a
    column here: storing it would need a nightly job that is always a day wrong
    in some timezone (db.md §5.5).
    """

    DOCUMENT_STATUSES = ["Draft", "Generated", "Suspended", "Cancelled", "Void"]
    WARRANTY_UNITS = ["Years", "Months"]
    START_EVENTS = ["Delivery", "Invoice", "Installation"]

    card_number = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="warranties")
    contact_person = models.TextField(null=True, blank=True)
    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)
    gstin = models.TextField(null=True, blank=True)

    delivery_challan = models.ForeignKey(
        DeliveryChallan, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )
    sales_invoice = models.ForeignKey(
        SalesInvoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )

    delivery_date = models.DateField(null=True, blank=True)
    delivery_location = models.TextField(null=True, blank=True)
    warranty_period = models.IntegerField(default=1)
    warranty_unit = models.TextField(choices=choices(WARRANTY_UNITS), default="Years")
    warranty_start_event = models.TextField(choices=choices(START_EVENTS), default="Delivery")
    start_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    #: The "expiring soon" window used by the coverage derivation.
    expiring_soon_days = models.IntegerField(default=30)

    document_status = models.TextField(choices=choices(DOCUMENT_STATUSES), default="Draft")
    suspended_reason = models.TextField(null=True, blank=True)
    cancelled_reason = models.TextField(null=True, blank=True)
    void_reason = models.TextField(null=True, blank=True)
    terms = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "warranty_cards"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "card_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_warranty_number",
            )
        ]

    def __str__(self):
        return self.card_number


class WarrantyCardItem(TenantModel):
    warranty_card = models.ForeignKey(
        WarrantyCard, on_delete=models.CASCADE, related_name="items"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    sku = models.TextField(null=True, blank=True)
    item_name = models.TextField(null=True, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=1)

    class Meta:
        db_table = "warranty_card_items"


class WarrantyCardSerial(TenantModel):
    warranty_card = models.ForeignKey(
        WarrantyCard, on_delete=models.CASCADE, related_name="serial_links"
    )
    serial = models.ForeignKey(
        "masters.ItemSerial", on_delete=models.PROTECT, related_name="warranty_links"
    )

    class Meta:
        db_table = "warranty_card_serials"
        constraints = [
            models.UniqueConstraint(
                fields=["warranty_card", "serial"], name="pk_warranty_card_serials"
            )
        ]
