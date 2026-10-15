"""
Accounts (db.md §8, api.md §8).

Ledgers, trial balance, P&L, balance sheet, GST summary and ageing are
**queries over journal_lines**, not stored tables. db.md §8 is explicit: do not
build a parallel ``party_ledger`` table -- two sources of truth for money is
where reconciliation bugs come from.
"""
from django.db import models
from django.utils import timezone

from apps.core.models import LegacyIdMixin, TenantModel


class Account(TenantModel, LegacyIdMixin):
    """Chart of accounts.

    ``is_system`` marks the accounts the auto-posting matrix (api.md §8.1)
    depends on -- Debtors, Creditors, GST Output/Input, Sales, Purchases,
    Stock. They cannot be deleted or renamed out from under a posting.
    """

    TYPES = [
        ("Asset", "Asset"),
        ("Liability", "Liability"),
        ("Equity", "Equity"),
        ("Income", "Income"),
        ("Expense", "Expense"),
    ]

    code = models.TextField()
    name = models.TextField()
    type = models.TextField(choices=TYPES)
    subtype = models.TextField(null=True, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    is_system = models.BooleanField(default=False)
    #: Stable handle for the system accounts the posting service looks up
    #: ('debtors', 'creditors', 'sales', 'gst_output', ...). Null for the rest.
    system_key = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = "chart_of_accounts"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_coa_code",
            ),
            models.UniqueConstraint(
                fields=["client", "system_key"],
                condition=models.Q(system_key__isnull=False, deleted_at__isnull=True),
                name="uq_coa_system_key",
            ),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class BankAccount(TenantModel, LegacyIdMixin):
    TYPES = [("Bank", "Bank"), ("Cash", "Cash"), ("UPI", "UPI"), ("Card", "Card")]

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="bank_accounts")
    name = models.TextField()
    type = models.TextField(choices=TYPES, default="Bank")
    account_number = models.TextField(null=True, blank=True)
    ifsc = models.TextField(null=True, blank=True)
    bank_name = models.TextField(null=True, blank=True)
    branch = models.TextField(null=True, blank=True)
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "bank_accounts"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name


class JournalEntry(TenantModel):
    """db.md §8.

    System entries are immutable: a cancellation posts a **new** reversing
    entry with ``reversal_of`` set (api.md §6.9 step 3), never an edit.
    """

    entry_number = models.TextField()
    entry_date = models.DateField()
    narration = models.TextField(null=True, blank=True)
    source_document_type = models.TextField(null=True, blank=True)
    source_document_id = models.UUIDField(null=True, blank=True)
    is_system = models.BooleanField(default=False)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reversals"
    )
    status = models.TextField(default="Posted")  # Posted | Draft | Reversed
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "journal_entries"
        ordering = ["-entry_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "entry_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_journal_number",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "source_document_type", "source_document_id"],
                name="ix_journal_source",
            ),
            models.Index(fields=["client", "-entry_date"], name="ix_journal_date"),
        ]

    def __str__(self):
        return self.entry_number


class JournalLine(TenantModel):
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines"
    )
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    #: Sub-ledger dimension -- ``GET /parties/{id}/ledger/`` is a window
    #: function over these rows with a running sum(debit - credit).
    party = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_lines"
    )
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    line_no = models.IntegerField(default=1)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "journal_lines"
        ordering = ["line_no"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0)
                ),
                name="ck_journal_line_one_side",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "account", "journal_entry"], name="ix_journal_lines_account"
            ),
            models.Index(
                fields=["client", "party"],
                name="ix_journal_lines_party",
                condition=models.Q(party__isnull=False),
            ),
        ]


class Budget(TenantModel):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="budgets")
    period_start = models.DateField()
    period_end = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "budgets"
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "account", "period_start", "period_end"],
                name="uq_budgets_period",
            )
        ]


class BankTransfer(TenantModel):
    """``POST /accounts/transfers/`` -- interbank transfer (api.md §8)."""

    from_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="transfers_in"
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    transfer_date = models.DateField()
    reference = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "bank_transfers"
        ordering = ["-transfer_date"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_account=models.F("to_account")),
                name="ck_bank_transfer_accounts",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="ck_bank_transfer_amount"
            ),
        ]


class ExpenseCategory(TenantModel):
    name = models.TextField()
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "expense_categories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_expense_categories_name",
            )
        ]

    def __str__(self):
        return self.name


class StandaloneNote(TenantModel, LegacyIdMixin):
    """Standalone Debit and Credit Notes with approval workflow (PDF §14)."""

    NOTE_TYPES = [
        ("DEBIT_NOTE", "Debit Note"),
        ("CREDIT_NOTE", "Credit Note"),
    ]
    STATUSES = [
        ("Draft", "Draft"),
        ("Pending Approval", "Pending Approval"),
        ("Approved", "Approved"),
        ("Posted", "Posted"),
        ("Cancelled", "Cancelled"),
    ]
    AGAINST_DOC_TYPES = [
        ("INVOICE", "Sales Invoice"),
        ("BILL", "Purchase Bill"),
        ("JWO", "Job Work Order"),
        ("SALES_RETURN", "Sales Return"),
        ("PURCHASE_RETURN", "Purchase Return"),
        ("MANUAL", "Manual / Direct"),
    ]

    note_type = models.TextField(choices=NOTE_TYPES, default="CREDIT_NOTE")
    note_number = models.TextField(db_index=True)
    note_date = models.DateField(default=timezone.localdate)
    party = models.ForeignKey(
        "masters.Party", on_delete=models.PROTECT, related_name="standalone_notes"
    )
    against_document_type = models.TextField(choices=AGAINST_DOC_TYPES, default="INVOICE")
    against_document_id = models.UUIDField(null=True, blank=True)
    against_document_number = models.TextField(null=True, blank=True)
    return_reason = models.ForeignKey(
        "masters.ReturnReasonMaster", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason_notes = models.TextField(null=True, blank=True)
    taxable_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(choices=STATUSES, default="Draft")
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "standalone_notes"
        ordering = ["-note_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "note_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_standalone_note_number",
            )
        ]

    def __str__(self):
        return f"{self.note_type} {self.note_number} ({self.status}): {self.total_amount}"

