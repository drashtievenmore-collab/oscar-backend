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
from apps.core.money import ZERO, D, round2, round4
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
    ("pf_payable", "2310", "Provident Fund Payable", "Liability", "Payable"),
    ("esi_payable", "2320", "ESIC Payable", "Liability", "Payable"),
    ("pt_payable", "2330", "Professional Tax Payable", "Liability", "Payable"),
    ("tds_payable", "2340", "TDS on Salaries Payable", "Liability", "Payable"),
    ("employee_advances", "1150", "Employee Advances", "Asset", "Receivable"),
    ("incentive_expense", "5510", "Sales Incentives & Commissions", "Expense", None),
    ("incentive_payable", "2350", "Incentive Payable", "Liability", "Payable"),
    # Used by broker payouts and sample consumption, which posted to keys that
    # did not exist and silently skipped their journal entries.
    ("brokerage_payable", "2360", "Brokerage Payable", "Liability", "Payable"),
    ("brokerage_expense", "5520", "Brokerage & Commission Expense", "Expense", None),
    ("sample_expense", "5410", "Sample Expense", "Expense", None),
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
        spec = next((item for item in SYSTEM_ACCOUNTS if item[0] == system_key), None)
        if spec:
            from apps.core.models import Client
            client = Client.objects.filter(id=client_id).first()
            if client:
                account, _ = Account.objects.get_or_create(
                    client=client,
                    system_key=system_key,
                    defaults={
                        "code": spec[1],
                        "name": spec[2],
                        "type": spec[3],
                        "subtype": spec[4],
                        "is_system": True,
                    },
                )
                return account
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
    # Ledgers leave out both the reversed original and its mirror, so the pair
    # nets to zero. The mirror's own post ran before the status flip above;
    # refresh the party balances now that the original is excluded.
    for party_id in {line.party_id for line in entry.lines.all() if line.party_id}:
        recalculate_party_balance(entry.client_id, party_id)
    return reversal


def reverse_document_entries(*, client_id, source_document_type, source_document_id, user=None):
    """Reverse every un-reversed entry a document produced."""
    entries = JournalEntry.objects.filter(
        client_id=client_id,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        deleted_at__isnull=True,
        # A reversal entry carries the same source document; never reverse it.
        reversal_of__isnull=True,
    ).exclude(status="Reversed")
    return [reverse_entry(entry, user=user) for entry in entries]


# ---------------------------------------------------------------------------
# Inventory cost (COGS)
# ---------------------------------------------------------------------------
def inventory_unit_cost(client_id, item, location_id=None):
    """Weighted average cost per stock unit, from ``stock_balances``.

    ``apps.inventory.services._apply_to_balance`` accumulates every costed
    inward movement (GRN at landed cost, returns, transfers in) into
    ``inward_value`` / ``inward_qty``; their ratio is the WAC. The location's
    own WAC is used first, then the item's WAC across all locations. The item
    master's ``cost_price`` is only the last resort for an item that has never
    had a costed inward movement (e.g. migrated opening stock).
    """
    from apps.inventory.models import StockBalance
    from apps.masters.models import Item

    item_id = getattr(item, "id", item)
    balances = StockBalance.objects.filter(client_id=client_id, item_id=item_id)
    if location_id is not None:
        row = balances.filter(location_id=location_id).first()
        if row is not None and row.weighted_average_cost:
            return round4(row.weighted_average_cost)
    totals = balances.aggregate(
        qty=Coalesce(Sum("inward_qty"), Value(Decimal("0")),
                     output_field=DecimalField(max_digits=18, decimal_places=4)),
        value=Coalesce(Sum("inward_value"), Value(Decimal("0")),
                       output_field=DecimalField(max_digits=18, decimal_places=2)),
    )
    if D(totals["qty"]) > ZERO:
        return round4(D(totals["value"]) / D(totals["qty"]))

    cost_price = (
        item.cost_price
        if hasattr(item, "cost_price")
        else Item.objects.filter(pk=item_id).values_list("cost_price", flat=True).first()
    )
    return round4(cost_price) if cost_price else None


def _movement_unit_cost(client_id, item_id, references):
    """Average unit cost of the SALE movements posted by ``references``
    (``[(reference_type, reference_id), ...]``) for one item."""
    from apps.inventory.models import StockMovement

    condition = Q()
    for reference_type, reference_id in references:
        if reference_id:
            condition |= Q(reference_type=reference_type, reference_id=reference_id)
    if not condition:
        return None
    qty = ZERO
    value = ZERO
    for movement in StockMovement.objects.filter(
        condition, client_id=client_id, item_id=item_id, type="SALE",
        unit_cost__isnull=False,
    ):
        moved = -D(movement.quantity)
        qty += moved
        value += moved * D(movement.unit_cost)
    if qty <= ZERO:
        return None
    return round4(value / qty)


def sales_line_unit_cost(invoice, line):
    """Cost per stock unit of what an invoice line sold.

    The stock left either on the invoice itself or on the delivery challan(s)
    it bills; the cost frozen on those SALE movements is the cost of the goods
    that actually went out. Falls back to the current WAC.
    """
    from apps.sales.models import DeliveryChallanLine

    references = [("SalesInvoice", invoice.id)]
    if line.delivery_challan_line_id:
        references.append(
            ("DeliveryChallan", line.delivery_challan_line.delivery_challan_id)
        )
    if invoice.delivery_challan_id:
        references.append(("DeliveryChallan", invoice.delivery_challan_id))
    if line.sales_order_line_id:
        challan_ids = DeliveryChallanLine.objects.filter(
            sales_order_line_id=line.sales_order_line_id, deleted_at__isnull=True
        ).exclude(delivery_challan__status__in=["Draft", "Cancelled"]).values_list(
            "delivery_challan_id", flat=True
        )
        references.extend(("DeliveryChallan", cid) for cid in challan_ids)

    cost = _movement_unit_cost(invoice.client_id, line.item_id, references)
    if cost is None:
        cost = inventory_unit_cost(invoice.client_id, line.item, invoice.location_id)
    return cost


def _stock_qty(qty, line):
    from apps.masters import uom

    return D(uom.to_stock_qty(qty, getattr(line, "uom_factor", None) or Decimal("1")))


def invoice_cogs(invoice):
    """COGS of a sales invoice: stock qty x unit cost, per stock-holding line."""
    total = ZERO
    for line in invoice.line_items.filter(deleted_at__isnull=True).select_related(
        "item", "delivery_challan_line"
    ):
        if line.item_id is None or not line.item.holds_stock:
            continue
        unit_cost = sales_line_unit_cost(invoice, line)
        if unit_cost is None:
            continue
        total += _stock_qty(line.qty, line) * D(unit_cost)
    return round2(total)


def return_cogs(sales_return):
    """COGS to reverse for a credit note: returned stock qty x the unit cost
    the original invoice line was costed at."""
    total = ZERO
    invoice = sales_return.sales_invoice
    for line in sales_return.line_items.filter(deleted_at__isnull=True).select_related(
        "item", "sales_invoice_line", "sales_invoice_line__delivery_challan_line"
    ):
        invoice_line = line.sales_invoice_line
        if line.item_id is None or not line.item.holds_stock or invoice_line is None:
            continue
        unit_cost = sales_line_unit_cost(invoice, invoice_line)
        if unit_cost is None:
            continue
        total += _stock_qty(line.returned_qty, invoice_line) * D(unit_cost)
    return round2(total)


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

    cogs_total = invoice_cogs(invoice)
    if cogs_total > ZERO:
        postings.append(debit(system_account(client_id, "cogs"), cogs_total,
                              description=f"COGS {invoice.invoice_number}"))
        postings.append(credit(system_account(client_id, "stock"), cogs_total,
                               description=f"Inventory out {invoice.invoice_number}"))

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
    """Dr Sales Return, Dr GST Output | Cr Debtors. Dr Inventory | Cr COGS."""
    client_id = sales_return.client_id
    tax = round2(sales_return.total_tax)
    revenue = round2(sales_return.total - tax)
    postings = [
        debit(system_account(client_id, "sales_return"), revenue),
        debit(system_account(client_id, "gst_output"), tax),
        credit(system_account(client_id, "debtors"), sales_return.total, party=sales_return.party),
    ]
    cogs_total = return_cogs(sales_return)
    if cogs_total > ZERO:
        number = sales_return.credit_note_number or sales_return.return_number
        postings.append(debit(system_account(client_id, "stock"), cogs_total,
                              description=f"Inventory back {number}"))
        postings.append(credit(system_account(client_id, "cogs"), cogs_total,
                               description=f"COGS reversal {number}"))
    return post_entry(
        client=sales_return.client,
        postings=postings,
        entry_date=sales_return.doc_date,
        narration=f"Credit note {sales_return.credit_note_number or sales_return.return_number}",
        source_document_type="SalesReturn",
        source_document_id=sales_return.id,
        user=user,
    )


def _debit_signed(account, amount, *, description=None):
    """A debit that turns into a credit when ``amount`` is negative
    (e.g. a negative round-off), so the entry still balances."""
    amount = round2(amount)
    if amount < ZERO:
        return credit(account, -amount, description=description)
    return debit(account, amount, description=description)


def purchase_bill_postings(bill):
    """Debit side of a purchase bill, split per the chart of accounts.

    * stock-holding items (``Item.holds_stock``)  -> Dr Inventory (1200)
    * non-stock items / free-text lines            -> Dr Purchases (5000)
    * freight + other charges on the header        -> Dr Freight (5300)
    * GST                                          -> Dr GST Input (1400)

    The header's taxable value (after any header discount) is split between
    stock and non-stock in proportion to the lines' net amounts; round-off
    rides on the larger side so the entry balances to the paisa.
    """
    client_id = bill.client_id
    tax = round2(bill.total_tax)
    freight = round2(D(bill.freight_charges) + D(bill.other_charges))
    goods = round2(D(bill.total) - tax - freight)

    stock_net = ZERO
    non_stock_net = ZERO
    for line in bill.line_items.filter(deleted_at__isnull=True).select_related("item"):
        net = D(line.amount) - D(line.discount_amount)
        if line.item_id is not None and line.item.holds_stock:
            stock_net += net
        else:
            non_stock_net += net

    taxable = round2(bill.taxable_value)
    gross = stock_net + non_stock_net
    if gross > ZERO:
        stock_amt = round2(taxable * stock_net / gross)
        non_stock_amt = round2(taxable - stock_amt)
    else:
        stock_amt, non_stock_amt = ZERO, taxable

    round_off = round2(goods - stock_amt - non_stock_amt)
    if stock_amt >= non_stock_amt:
        stock_amt = round2(stock_amt + round_off)
    else:
        non_stock_amt = round2(non_stock_amt + round_off)

    return [
        _debit_signed(system_account(client_id, "stock"), stock_amt,
                      description="Stock purchased"),
        _debit_signed(system_account(client_id, "purchases"), non_stock_amt,
                      description="Non-stock purchases"),
        _debit_signed(system_account(client_id, "freight"), freight,
                      description="Freight and other charges"),
        _debit_signed(system_account(client_id, "gst_input"), tax),
        credit(system_account(client_id, "creditors"), bill.total, party=bill.party,
               description=f"Bill {bill.bill_number}"),
    ]


def post_purchase_bill(bill, *, user=None):
    """Dr Inventory / Purchases / Freight, Dr GST Input | Cr Creditors."""
    postings = purchase_bill_postings(bill)

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
    """Itemized payroll posting (api.md §11.4).
    Debits Salary Expense (Gross), Credits Bank (Net), Credits PF/ESI/PT/TDS/Advances.
    """
    client_id = payslip.client_id
    account = (
        payslip.bank_account.account
        if payslip.bank_account_id
        else system_account(client_id, "cash")
    )
    net_amount = round2(payslip.paid_amount or payslip.net_payable)
    advance_amount = round2(payslip.advance_recovery or Decimal("0.00"))

    components = list(payslip.components.all())
    deduction_map = {
        "pf": "pf_payable",
        "provident fund": "pf_payable",
        "epf": "pf_payable",
        "esi": "esi_payable",
        "esic": "esi_payable",
        "pt": "pt_payable",
        "professional tax": "pt_payable",
        "tds": "tds_payable",
        "tax": "tds_payable",
        "income tax": "tds_payable",
    }

    statutory_credits = []
    total_statutory = Decimal("0.00")

    for comp in components:
        if comp.kind == "deduction":
            c_name = comp.name.lower().strip()
            mapped_key = None
            for pattern, sys_key in deduction_map.items():
                if pattern in c_name:
                    mapped_key = sys_key
                    break
            comp_amt = round2(comp.amount)
            if comp_amt > Decimal("0.00"):
                target_key = mapped_key or "salary_payable"
                statutory_credits.append(
                    credit(system_account(client_id, target_key), comp_amt, description=comp.name)
                )
                total_statutory += comp_amt

    if advance_amount > Decimal("0.00"):
        statutory_credits.append(
            credit(system_account(client_id, "employee_advances"), advance_amount, description="Advance Recovery")
        )
        total_statutory += advance_amount

    gross_salary = net_amount + total_statutory

    if statutory_credits:
        postings = [
            debit(system_account(client_id, "salary_expense"), gross_salary),
            credit(account, net_amount),
        ] + statutory_credits
    else:
        postings = [
            debit(system_account(client_id, "salary_expense"), net_amount),
            credit(account, net_amount),
        ]

    return post_entry(
        client=payslip.client,
        postings=postings,
        entry_date=payslip.payment_date or timezone.localdate(),
        narration=f"Salary {payslip.period_month:%b %Y} - {payslip.employee.name}",
        source_document_type="Payslip",
        source_document_id=payslip.id,
        user=user,
    )


def post_salary_advance(advance, *, bank_account=None, user=None):
    """Dr Employee Advances | Cr Bank/Cash"""
    client_id = advance.client_id
    account = (
        bank_account.account
        if bank_account and getattr(bank_account, "account", None)
        else system_account(client_id, "bank")
    )
    amount = round2(advance.amount)
    return post_entry(
        client=advance.client,
        postings=[
            debit(system_account(client_id, "employee_advances"), amount),
            credit(account, amount),
        ],
        entry_date=advance.request_date or timezone.localdate(),
        narration=f"Salary Advance Disbursed - {advance.employee.name} (Ref: {advance.advance_code or advance.id})",
        source_document_type="SalaryAdvance",
        source_document_id=advance.id,
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
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False).aggregate(
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
        .exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
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
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
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
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
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
        .exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
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
