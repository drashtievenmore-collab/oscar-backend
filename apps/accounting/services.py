"""
Ledger services (api.md §8, db.md §8).

The auto-posting matrix of api.md §8.1, plus the ledger queries every screen
reads. The entry is written in the **same transaction** as the document's stock
movements -- one commit, or neither (db.md §8).

System entries are immutable. A cancellation posts a new reversing entry with
``reversal_of`` set; nothing here ever edits a posted entry.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation, Codes, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

from .models import Account, JournalEntry, JournalLine

#: db.md §8 -- the accounts the posting matrix depends on, seeded per tenant.
SYSTEM_ACCOUNTS = [
    # (system_key, code, name, type, subtype)
    ("debtors", "1100", "Sundry Debtors", "Asset", "Receivable"),
    ("stock", "1200", "Inventory", "Asset", "Stock"),
    ("bank", "1300", "Bank Accounts", "Asset", "Bank"),
    ("cash", "1310", "Cash in Hand", "Asset", "Bank"),
    ("gst_input", "1400", "GST Input Credit", "Asset", "Tax"),
    ("creditors", "2100", "Sundry Creditors", "Liability", "Payable"),
    ("gst_output", "2200", "GST Payable", "Liability", "Tax"),
    ("salary_payable", "2300", "Salaries Payable", "Liability", "Payable"),
    ("capital", "3000", "Owner Equity", "Equity", None),
    ("sales", "4000", "Sales", "Income", None),
    ("sales_return", "4100", "Sales Returns", "Income", None),
    ("purchases", "5000", "Purchases", "Expense", None),
    ("purchase_return", "5100", "Purchase Returns", "Expense", None),
    ("cogs", "5200", "Cost of Goods Sold", "Expense", None),
    ("freight", "5300", "Freight and Carriage", "Expense", None),
    ("stock_adjustment", "5400", "Stock Adjustment", "Expense", None),
    ("salary_expense", "5500", "Salaries and Wages", "Expense", None),
    ("general_expense", "5900", "General Expenses", "Expense", None),
]


def seed_chart_of_accounts(client):
    """Create the system accounts for a new tenant. Idempotent."""
    created = []
    for system_key, code, name, type_, subtype in SYSTEM_ACCOUNTS:
        account, was_created = Account.objects.get_or_create(
            client=client,
            system_key=system_key,
            defaults={
                "code": code,
                "name": name,
                "type": type_,
                "subtype": subtype,
                "is_system": True,
            },
        )
        if was_created:
            created.append(account)
    return created


def system_account(client_id, system_key):
    account = Account.objects.filter(
        client_id=client_id, system_key=system_key, deleted_at__isnull=True
    ).first()
    if account is None:
        raise BusinessRuleViolation(
            "The chart of accounts is not set up for this workspace.",
            code="MISSING_SYSTEM_ACCOUNT",
            detail=f"No account is mapped to '{system_key}'.",
        )
    return account


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
class Posting:
    """One side of a journal entry, built before anything is written."""

    __slots__ = ("account", "debit", "credit", "party", "description")

    def __init__(self, account, *, debit=ZERO, credit=ZERO, party=None, description=None):
        self.account = account
        self.debit = round2(debit)
        self.credit = round2(credit)
        self.party = party
        self.description = description


def debit(account, amount, *, party=None, description=None):
    return Posting(account, debit=amount, party=party, description=description)


def credit(account, amount, *, party=None, description=None):
    return Posting(account, credit=amount, party=party, description=description)


@transaction.atomic
def post_entry(
    *,
    client,
    postings,
    entry_date=None,
    narration=None,
    source_document_type=None,
    source_document_id=None,
    is_system=True,
    user=None,
    reversal_of=None,
):
    """Write a balanced journal entry.

    Zero-value postings are dropped before the balance check, so callers can
    pass ``credit(gst_output, total_tax)`` unconditionally without special-
    casing a zero-rated document.
    """
    client_id = getattr(client, "id", client)
    postings = [p for p in postings if p.debit > ZERO or p.credit > ZERO]
    if not postings:
        return None

    total_debit = round2(sum((p.debit for p in postings), ZERO))
    total_credit = round2(sum((p.credit for p in postings), ZERO))
    if total_debit != total_credit:
        raise BusinessRuleViolation(
            "This entry does not balance.",
            code=Codes.UNBALANCED_JOURNAL_ENTRY,
            detail=f"Debits {total_debit} vs credits {total_credit}.",
        )

    entry = JournalEntry.objects.create(
        client_id=client_id,
        entry_number=allocate_number(client, "JE", entry_date),
        entry_date=entry_date or timezone.localdate(),
        narration=narration,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        is_system=is_system,
        reversal_of=reversal_of,
        status="Posted",
        posted_at=timezone.now(),
        posted_by=user if getattr(user, "is_authenticated", False) else None,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    JournalLine.objects.bulk_create(
        [
            JournalLine(
                client_id=client_id,
                journal_entry=entry,
                account=posting.account,
                party=posting.party,
                debit=posting.debit,
                credit=posting.credit,
                line_no=index,
                description=posting.description,
            )
            for index, posting in enumerate(postings, start=1)
        ]
    )

    for posting in postings:
        if posting.party is not None:
            recalculate_party_balance(client_id, posting.party)

    return entry


@transaction.atomic
def reverse_entry(entry, *, user=None, narration=None):
    """Post the mirror image of an entry (api.md §6.9 step 3).

    Never edits the original -- that is what ``is_system`` immutability means.
    """
    if entry is None:
        return None
    if JournalEntry.objects.filter(reversal_of=entry, deleted_at__isnull=True).exists():
        return None  # already reversed

    postings = [
        Posting(
            line.account,
            debit=line.credit,
            credit=line.debit,
            party=line.party,
            description=line.description,
        )
        for line in entry.lines.all()
    ]
    reversal = post_entry(
        client=entry.client_id,
        postings=postings,
        entry_date=timezone.localdate(),
        narration=narration or f"Reversal of {entry.entry_number}",
        source_document_type=entry.source_document_type,
        source_document_id=entry.source_document_id,
        is_system=True,
        user=user,
        reversal_of=entry,
    )
    JournalEntry.objects.filter(pk=entry.pk).update(status="Reversed")
    return reversal


def reverse_document_entries(*, client_id, source_document_type, source_document_id, user=None):
    """Reverse every un-reversed entry a document produced."""
    entries = JournalEntry.objects.filter(
        client_id=client_id,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        deleted_at__isnull=True,
    ).exclude(status="Reversed")
    return [reverse_entry(entry, user=user) for entry in entries]


# ---------------------------------------------------------------------------
# api.md §8.1 -- the auto-posting matrix
# ---------------------------------------------------------------------------
def post_sales_invoice(invoice, *, user=None):
    """Dr Debtors | Cr Sales, Cr GST Output. Dr COGS | Cr Inventory."""
    client_id = invoice.client_id
    tax = round2((invoice.cgst or ZERO) + (invoice.sgst or ZERO) + (invoice.igst or ZERO) + (invoice.cess or ZERO))
    if tax == ZERO:
        tax = round2(invoice.total_tax)
    revenue = round2(invoice.total - tax)

    postings = [
        debit(system_account(client_id, "debtors"), invoice.total, party=invoice.party,
              description=f"Invoice {invoice.invoice_number}"),
        credit(system_account(client_id, "sales"), revenue),
        credit(system_account(client_id, "gst_output"), tax),
    ]

    cogs_total = ZERO
    for line in invoice.line_items.filter(deleted_at__isnull=True).select_related("item"):
        qty = D(line.quantity or 0)
        cost = D(getattr(line.item, "cost_price", 0) if line.item else 0)
        cogs_total += qty * cost
    cogs_total = round2(cogs_total)
    if cogs_total > ZERO:
        postings.append(debit(system_account(client_id, "cogs"), cogs_total, description="Cost of Goods Sold"))
        postings.append(credit(system_account(client_id, "stock"), cogs_total, description="Inventory Depletion"))

    return post_entry(
        client=invoice.client,
        postings=postings,
        entry_date=invoice.doc_date,
        narration=f"Sales invoice {invoice.invoice_number} - {invoice.party_name}",
        source_document_type="SalesInvoice",
        source_document_id=invoice.id,
        user=user,
    )


def post_payment_in(payment, *, user=None):
    """Dr Bank/Cash | Cr Debtors."""
    client_id = payment.client_id
    account = (
        payment.bank_account.account
        if payment.bank_account_id
        else system_account(client_id, "cash")
    )
    return post_entry(
        client=payment.client,
        postings=[
            debit(account, payment.amount, description=f"Payment {payment.payment_number}"),
            credit(system_account(client_id, "debtors"), payment.amount, party=payment.party),
        ],
        entry_date=payment.payment_date,
        narration=f"Payment received {payment.payment_number}",
        source_document_type="PaymentIn",
        source_document_id=payment.id,
        user=user,
    )


def post_sales_return(sales_return, *, user=None):
    """Dr Sales Return, Dr GST Output | Cr Debtors."""
    client_id = sales_return.client_id
    tax = round2(sales_return.total_tax)
    revenue = round2(sales_return.total - tax)
    return post_entry(
        client=sales_return.client,
        postings=[
            debit(system_account(client_id, "sales_return"), revenue),
            debit(system_account(client_id, "gst_output"), tax),
            credit(system_account(client_id, "debtors"), sales_return.total, party=sales_return.party),
        ],
        entry_date=sales_return.doc_date,
        narration=f"Credit note {sales_return.credit_note_number or sales_return.return_number}",
        source_document_type="SalesReturn",
        source_document_id=sales_return.id,
        user=user,
    )


def post_purchase_bill(bill, *, user=None):
    """Dr Purchases/Inventory, Dr GST Input | Cr Creditors."""
    client_id = bill.client_id
    tax = round2(bill.total_tax)
    goods = round2(bill.total - tax)

    lines = list(bill.line_items.filter(deleted_at__isnull=True).select_related("item"))
    stock_amt = ZERO
    non_stock_amt = ZERO
    if lines:
        for line in lines:
            amt = round2(D(getattr(line, "taxable_value", None) or getattr(line, "amount", 0)))
            if line.item and getattr(line.item, "type", "") == "Service":
                non_stock_amt += amt
            elif line.item and getattr(line.item, "item_type", "") in ("Service", "Expense"):
                non_stock_amt += amt
            else:
                stock_amt += amt
    else:
        stock_amt = goods

    postings = []
    goods_posted = stock_amt + non_stock_amt
    if goods_posted != goods:
        diff = goods - goods_posted
        if stock_amt >= non_stock_amt:
            stock_amt += diff
        else:
            non_stock_amt += diff

    if stock_amt > ZERO:
        postings.append(debit(system_account(client_id, "stock"), stock_amt))
    if non_stock_amt > ZERO:
        postings.append(debit(system_account(client_id, "purchases"), non_stock_amt))

    postings.extend([
        debit(system_account(client_id, "gst_input"), tax),
        credit(system_account(client_id, "creditors"), bill.total, party=bill.party,
               description=f"Bill {bill.bill_number}"),
    ])

    return post_entry(
        client=bill.client,
        postings=postings,
        entry_date=bill.doc_date,
        narration=f"Purchase bill {bill.bill_number} - {bill.party_name}",
        source_document_type="PurchaseBill",
        source_document_id=bill.id,
        user=user,
    )


def post_payment_out(payment, *, user=None):
    """Dr Creditors | Cr Bank/Cash."""
    client_id = payment.client_id
    account = (
        payment.bank_account.account
        if payment.bank_account_id
        else system_account(client_id, "cash")
    )
    return post_entry(
        client=payment.client,
        postings=[
            debit(system_account(client_id, "creditors"), payment.amount, party=payment.party),
            credit(account, payment.amount, description=f"Payment {payment.payment_number}"),
        ],
        entry_date=payment.payment_date,
        narration=f"Payment made {payment.payment_number}",
        source_document_type="PaymentOut",
        source_document_id=payment.id,
        user=user,
    )


def post_purchase_return(purchase_return, *, user=None):
    """Dr Creditors | Cr Purchase Return, Cr GST Input."""
    client_id = purchase_return.client_id
    tax = round2(purchase_return.total_tax)
    goods = round2(purchase_return.total - tax)
    return post_entry(
        client=purchase_return.client,
        postings=[
            debit(system_account(client_id, "creditors"), purchase_return.total,
                  party=purchase_return.party),
            credit(system_account(client_id, "purchase_return"), goods),
            credit(system_account(client_id, "gst_input"), tax),
        ],
        entry_date=purchase_return.doc_date,
        narration=f"Debit note {purchase_return.debit_note_number or purchase_return.return_number}",
        source_document_type="PurchaseReturn",
        source_document_id=purchase_return.id,
        user=user,
    )


def post_expense(expense, *, user=None):
    """Dr Expense head, Dr GST Input | Cr Bank/Cash/Creditors."""
    client_id = expense.client_id
    head = expense.account or (
        expense.category.account if expense.category_id and expense.category.account_id else None
    ) or system_account(client_id, "general_expense")

    if expense.payment_mode == "Credit":
        funding = system_account(client_id, "creditors")
        party = expense.party
    elif expense.bank_account_id:
        funding = expense.bank_account.account
        party = None
    else:
        funding = system_account(client_id, "cash")
        party = None

    total = round2(expense.total or (D(expense.amount) + D(expense.tax_amount)))
    return post_entry(
        client=expense.client,
        postings=[
            debit(head, expense.amount, description=expense.notes),
            debit(system_account(client_id, "gst_input"), expense.tax_amount),
            credit(funding, total, party=party),
        ],
        entry_date=expense.expense_date,
        narration=f"Expense {expense.expense_number}",
        source_document_type="Expense",
        source_document_id=expense.id,
        user=user,
    )


def post_bank_transfer(transfer, *, user=None):
    """Dr Destination account | Cr Source account."""
    return post_entry(
        client=transfer.client,
        postings=[
            debit(transfer.to_account.account, transfer.amount),
            credit(transfer.from_account.account, transfer.amount),
        ],
        entry_date=transfer.transfer_date,
        narration=transfer.reference or "Interbank transfer",
        source_document_type="BankTransfer",
        source_document_id=transfer.id,
        user=user,
    )


def post_stock_adjustment(movement, *, user=None, value=None):
    """Dr Inventory or Stock Adjustment | the opposite side."""
    client_id = movement.client_id
    amount = round2(value if value is not None else abs(movement.quantity) * D(movement.unit_cost))
    if amount == ZERO:
        return None
    stock = system_account(client_id, "stock")
    adjustment = system_account(client_id, "stock_adjustment")
    increased = movement.quantity > ZERO
    return post_entry(
        client=movement.client,
        postings=[
            debit(stock if increased else adjustment, amount),
            credit(adjustment if increased else stock, amount),
        ],
        entry_date=movement.movement_date,
        narration=movement.notes or "Stock adjustment",
        source_document_type="StockAdjustment",
        source_document_id=movement.id,
        user=user,
    )


def post_payroll(payslip, *, user=None):
    """Dr Salary expense | Cr Bank (api.md §11.4 -- payroll is the one HRMS
    table that touches accounts)."""
    client_id = payslip.client_id
    account = (
        payslip.bank_account.account
        if payslip.bank_account_id
        else system_account(client_id, "cash")
    )
    amount = round2(payslip.paid_amount or payslip.net_payable)
    return post_entry(
        client=payslip.client,
        postings=[
            debit(system_account(client_id, "salary_expense"), amount),
            credit(account, amount),
        ],
        entry_date=payslip.payment_date or timezone.localdate(),
        narration=f"Salary {payslip.period_month:%b %Y} - {payslip.employee.name}",
        source_document_type="Payslip",
        source_document_id=payslip.id,
        user=user,
    )


# ---------------------------------------------------------------------------
# Derived balances and ledgers
# ---------------------------------------------------------------------------
def _sum(queryset, field):
    return queryset.aggregate(
        value=Coalesce(
            Sum(field), Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        )
    )["value"]


def recalculate_party_balance(client_id, party):
    """db.md §12 -- ``parties.balance`` is derived from ``journal_lines``.

    Recomputed on every posting that touches the party and stored only for
    list-view performance; never accepted as a write.
    """
    from apps.masters.models import Party

    party_id = getattr(party, "id", party)
    rows = JournalLine.objects.filter(
        client_id=client_id, party_id=party_id, deleted_at__isnull=True
    ).exclude(journal_entry__status="Reversed").aggregate(
        debit=Coalesce(Sum("debit"), Value(Decimal("0.00")),
                       output_field=DecimalField(max_digits=18, decimal_places=2)),
        credit=Coalesce(Sum("credit"), Value(Decimal("0.00")),
                        output_field=DecimalField(max_digits=18, decimal_places=2)),
    )
    opening = (
        Party.objects.filter(pk=party_id).values_list("opening_balance", flat=True).first()
        or ZERO
    )
    balance = round2(opening + rows["debit"] - rows["credit"])
    Party.objects.filter(pk=party_id).update(balance=balance)
    return balance


def party_ledger(client_id, party_id, *, date_from=None, date_to=None):
    """``GET /parties/{id}/ledger/`` (api.md §8.4).

    A running ``sum(debit - credit)`` over ``journal_lines`` filtered by party.
    Deliberately a query, not a table: two sources of truth for money is where
    reconciliation bugs come from (db.md §8).
    """
    from apps.masters.models import Party

    party = Party.objects.filter(pk=party_id, client_id=client_id).first()
    opening = D(party.opening_balance) if party else ZERO

    queryset = (
        JournalLine.objects.filter(
            client_id=client_id, party_id=party_id, deleted_at__isnull=True
        )
        .exclude(journal_entry__status="Reversed")
        .select_related("journal_entry")
        .order_by("journal_entry__entry_date", "journal_entry__created_at", "line_no")
    )

    if date_from:
        prior = queryset.filter(journal_entry__entry_date__lt=date_from)
        opening = round2(opening + _sum(prior, "debit") - _sum(prior, "credit"))
        queryset = queryset.filter(journal_entry__entry_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(journal_entry__entry_date__lte=date_to)

    running = opening
    entries = []
    for line in queryset:
        running = round2(running + D(line.debit) - D(line.credit))
        entries.append(
            {
                "date": line.journal_entry.entry_date,
                "documentType": line.journal_entry.source_document_type or "Journal",
                "documentNumber": line.journal_entry.entry_number,
                "description": line.description or line.journal_entry.narration,
                "debit": line.debit,
                "credit": line.credit,
                "balance": running,
            }
        )

    return {
        "openingBalance": opening,
        "entries": entries,
        "closingBalance": running,
    }


def account_balances(client_id, *, as_of=None):
    """``GET /accounts/balances/`` -- replaces ``getAccountBalances``."""
    from .models import BankAccount

    queryset = JournalLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(journal_entry__status="Reversed")
    if as_of:
        queryset = queryset.filter(journal_entry__entry_date__lte=as_of)

    rows = queryset.values("account_id").annotate(
        debit=Coalesce(Sum("debit"), Value(Decimal("0.00")),
                       output_field=DecimalField(max_digits=18, decimal_places=2)),
        credit=Coalesce(Sum("credit"), Value(Decimal("0.00")),
                        output_field=DecimalField(max_digits=18, decimal_places=2)),
    )
    movement = {row["account_id"]: row for row in rows}

    results = []
    for bank in BankAccount.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).select_related("account"):
        row = movement.get(bank.account_id, {})
        balance = round2(
            D(bank.opening_balance)
            + D(row.get("debit", ZERO))
            - D(row.get("credit", ZERO))
        )
        results.append(
            {
                "id": str(bank.id),
                "name": bank.name,
                "type": bank.type,
                "accountNumber": bank.account_number,
                "bankName": bank.bank_name,
                "isDefault": bank.is_default,
                "balance": balance,
            }
        )
    return results


def trial_balance(client_id, *, as_of=None):
    queryset = JournalLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(journal_entry__status="Reversed")
    if as_of:
        queryset = queryset.filter(journal_entry__entry_date__lte=as_of)

    rows = (
        queryset.values(
            "account_id", "account__code", "account__name", "account__type"
        )
        .annotate(
            debit=Coalesce(Sum("debit"), Value(Decimal("0.00")),
                           output_field=DecimalField(max_digits=18, decimal_places=2)),
            credit=Coalesce(Sum("credit"), Value(Decimal("0.00")),
                            output_field=DecimalField(max_digits=18, decimal_places=2)),
        )
        .order_by("account__code")
    )

    results = []
    for row in rows:
        net = round2(row["debit"] - row["credit"])
        results.append(
            {
                "accountId": str(row["account_id"]),
                "code": row["account__code"],
                "name": row["account__name"],
                "type": row["account__type"],
                "debit": row["debit"],
                "credit": row["credit"],
                "balance": net,
            }
        )
    return results


def account_ledger(client_id, account_id, *, date_from=None, date_to=None):
    """``GET /accounts/ledger/{accountId}/`` with opening balance and running total."""
    account = Account.objects.filter(pk=account_id, client_id=client_id).first()
    opening = D(account.opening_balance) if account else ZERO

    queryset = (
        JournalLine.objects.filter(
            client_id=client_id, account_id=account_id, deleted_at__isnull=True
        )
        .exclude(journal_entry__status="Reversed")
        .select_related("journal_entry", "party")
        .order_by("journal_entry__entry_date", "journal_entry__created_at", "line_no")
    )

    if date_from:
        prior = queryset.filter(journal_entry__entry_date__lt=date_from)
        opening = round2(opening + _sum(prior, "debit") - _sum(prior, "credit"))
        queryset = queryset.filter(journal_entry__entry_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(journal_entry__entry_date__lte=date_to)

    running = opening
    entries = []
    for line in queryset:
        running = round2(running + D(line.debit) - D(line.credit))
        entries.append(
            {
                "date": line.journal_entry.entry_date,
                "entryNumber": line.journal_entry.entry_number,
                "narration": line.description or line.journal_entry.narration,
                "party": line.party.name if line.party_id else None,
                "debit": line.debit,
                "credit": line.credit,
                "balance": running,
            }
        )

    return {
        "accountId": str(account_id),
        "accountName": account.name if account else None,
        "openingBalance": opening,
        "entries": entries,
        "closingBalance": running,
    }
