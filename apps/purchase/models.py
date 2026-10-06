"""
Purchase (db.md §6, api.md §6).

    Purchase Order -> Goods Receipt (GRN) -> QC -> Purchase Bill -> Payment Out
                                              |
                                        Purchase Return

api.md §6.3 notes the frontend has no first-class GRN -- ``/purchase/receipts``
is a worklist of bills awaiting receipt, and receiving calls
``receivePurchaseBillGoods(billId, ...)``. Modelling a real GRN server-side is
"fine and recommended", so it exists here, but the bill-scoped endpoint is the
one the page calls and it stays.
"""
from django.db import models

from apps.core.documents import (
    DocumentHeader,
    DocumentLine,
    number_unique_constraint,
    total_check_constraint,
)
from apps.core.models import LegacyIdMixin, TenantModel

# Existing legacy values are retained; Oscar grey-fabric workflow adds the
# following so the UI can track the real lifecycle (Draft -> Pending Approval
# -> Approved -> In Production -> Partially/Fully Received -> Cancelled).
PO_STATUSES = [
    "Draft", "Issued", "Pending", "Received", "Cancelled",
    "Pending Approval", "Approved", "In Production",
    "Partially Received", "Fully Received",
]
BILL_STATUSES = ["Draft", "Unpaid", "Partially Paid", "Paid", "Cancelled"]
#: api.md §6.4 -- only `Approved` releases stock for sale or dispatch.
QC_STATUSES = ["Approved", "Pending Approval", "Rejected", "Rework"]
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
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrder(DocumentHeader):
    """The stored ``status`` is not what the UI shows.

    ``getPoBilledStatus`` derives ``Billed`` / ``Partially Billed`` from bill
    coverage and the UI renders that instead (api.md §6.2). The derivation
    lives in the service layer, not in a column, so it can never go stale.
    """

    po_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(PO_STATUSES), default="Draft")
    expected_date = models.DateField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    #: Set when the AutoPOModal reorder flow produced this PO.
    auto_generated = models.BooleanField(default=False)

    class Meta:
        db_table = "purchase_orders"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("purchase_orders", "po_number"),
            total_check_constraint("purchase_orders"),
        ]
        indexes = [models.Index(fields=["client", "status"], name="ix_purchase_orders_status")]

    def __str__(self):
        return self.po_number or f"PO {self.id}"


class PurchaseOrderLine(DocumentLine):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="line_items"
    )
    #: DERIVED in the receipt / billing transactions.
    received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    billed_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta(DocumentLine.Meta):
        db_table = "purchase_order_lines"
        constraints = [
            models.UniqueConstraint(fields=["purchase_order", "line_no"], name="uq_po_line_no")
        ]
        indexes = [models.Index(fields=["item", "purchase_order"], name="ix_po_lines_item")]

    @property
    def remaining_qty(self):
        return (self.qty or 0) - (self.billed_qty or 0)


# ---------------------------------------------------------------------------
# Purchase bills (api.md §6.4)
# ---------------------------------------------------------------------------
class PurchaseBill(DocumentHeader):
    bill_number = models.TextField(null=True, blank=True)
    #: The vendor's own bill reference, which is not ours to allocate.
    vendor_bill_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(BILL_STATUSES), default="Draft")
    due_date = models.DateField(null=True, blank=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="bills"
    )
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    goods_received = models.BooleanField(default=False)
    received_date = models.DateField(null=True, blank=True)
    qc_status = models.TextField(choices=choices(QC_STATUSES), null=True, blank=True)
    qc_note = models.TextField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "purchase_bills"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("purchase_bills", "bill_number"),
            total_check_constraint("purchase_bills"),
        ]
        indexes = [
            models.Index(fields=["client", "status"], name="ix_purchase_bills_status"),
            models.Index(
                fields=["client", "goods_received"], name="ix_purchase_bills_received"
            ),
        ]

    def __str__(self):
        return self.bill_number or f"Bill {self.id}"


class PurchaseBillLine(DocumentLine):
    """Landed cost is stored in three parts so the apportionment is auditable
    (db.md §6.2): ``landed_unit_cost = base_unit_cost + apportioned_cost / qty``,
    and the ``PURCHASE`` movement's ``unit_cost`` copies it."""

    purchase_bill = models.ForeignKey(
        PurchaseBill, on_delete=models.CASCADE, related_name="line_items"
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="bill_lines"
    )
    received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    received_weight = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    #: Weight-variance receiving (api.md §6.3).
    theoretical_weight = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    tolerance_pct = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    variation_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    is_weight_item = models.BooleanField(default=False)
    batch_number = models.TextField(null=True, blank=True)
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    base_unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    apportioned_cost = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    landed_unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta(DocumentLine.Meta):
        db_table = "purchase_bill_lines"
        constraints = [
            models.UniqueConstraint(fields=["purchase_bill", "line_no"], name="uq_bill_line_no")
        ]


# ---------------------------------------------------------------------------
# Goods receipts and QC (db.md §6.1)
# ---------------------------------------------------------------------------
class GoodsReceipt(TenantModel, LegacyIdMixin):
    GRN_QC_STATUSES = ["Pending", "Approved", "Rejected", "On Hold"]

    grn_number = models.TextField()
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="receipts"
    )
    purchase_bill = models.ForeignKey(
        PurchaseBill, null=True, blank=True, on_delete=models.SET_NULL, related_name="receipts"
    )
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="receipts")
    receipt_date = models.DateField()
    location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="receipts"
    )
    qc_status = models.TextField(choices=choices(GRN_QC_STATUSES), default="Pending")
    qc_note = models.TextField(null=True, blank=True)
    qc_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    qc_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "goods_receipts"
        ordering = ["-receipt_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "grn_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_grn_number",
            )
        ]

    def __str__(self):
        return self.grn_number


class GoodsReceiptLine(TenantModel):
    goods_receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.CASCADE, related_name="lines"
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    purchase_bill_line = models.ForeignKey(
        PurchaseBillLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    ordered_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    #: Wins over received_qty for weight items -- calculateItemStock already
    #: prefers it, and the movement must record the same number the UI showed.
    weighed_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    rejected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    batch_number = models.TextField(null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    variation_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "goods_receipt_lines"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rejected_qty__lte=models.F("received_qty")),
                name="ck_grl_rejected",
            )
        ]


# ---------------------------------------------------------------------------
# Payments out (api.md §6.5)
# ---------------------------------------------------------------------------
class PaymentOut(TenantModel, LegacyIdMixin):
    payment_number = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="payments_out")
    payment_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    mode = models.TextField(choices=choices(PAYMENT_MODES), default="Bank")
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
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
        db_table = "payments_out"
        ordering = ["-payment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "payment_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_payments_out_number",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_payments_out_amount"),
            models.CheckConstraint(
                condition=models.Q(allocated_amount__lte=models.F("amount")),
                name="ck_payments_out_alloc",
            ),
        ]

    def __str__(self):
        return self.payment_number

    @property
    def unallocated_amount(self):
        return (self.amount or 0) - (self.allocated_amount or 0)


# ---------------------------------------------------------------------------
# Purchase returns / debit notes (api.md §6.5)
# ---------------------------------------------------------------------------
class PurchaseReturn(DocumentHeader):
    return_number = models.TextField(null=True, blank=True)
    debit_note_number = models.TextField(null=True, blank=True)
    status = models.TextField(
        choices=choices(["Draft", "Posted", "Cancelled"]), default="Posted"
    )
    purchase_bill = models.ForeignKey(
        PurchaseBill, on_delete=models.PROTECT, related_name="returns"
    )
    reason = models.TextField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "purchase_returns"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("purchase_returns", "return_number"),
            total_check_constraint("purchase_returns"),
        ]


class PurchaseReturnLine(DocumentLine):
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.CASCADE, related_name="line_items"
    )
    purchase_bill_line = models.ForeignKey(
        PurchaseBillLine, on_delete=models.PROTECT, related_name="return_lines"
    )
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta(DocumentLine.Meta):
        db_table = "purchase_return_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_return", "line_no"], name="uq_purchase_return_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(returned_qty__gt=0), name="ck_prl_returned_qty"
            ),
        ]


# ---------------------------------------------------------------------------
# Expenses (db.md §6.3)
# ---------------------------------------------------------------------------
class Expense(TenantModel, LegacyIdMixin):
    PAYMENT_MODES_WITH_CREDIT = PAYMENT_MODES + ["Credit"]

    expense_number = models.TextField()
    category = models.ForeignKey(
        "accounting.ExpenseCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses"
    )
    expense_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    payment_mode = models.TextField(choices=choices(PAYMENT_MODES_WITH_CREDIT), null=True, blank=True)
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    receipt_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    status = models.TextField(
        choices=choices(["Draft", "Posted", "Cancelled"]), default="Posted"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "expenses"
        ordering = ["-expense_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "expense_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_expense_number",
            )
        ]

    def __str__(self):
        return self.expense_number
