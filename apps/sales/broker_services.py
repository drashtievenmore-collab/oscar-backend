"""
Broker commission services (PDF §12).

Tracks broker commissions linked to sales invoices:
- Status lifecycle: Accrued -> Approved -> Payable -> Partially Paid -> Paid / Hold / Cancelled.
- Support commission types: percent (of taxable invoice value), per_meter (on dispatched meters), per_taka (on dispatched takas), fixed.
- Broker payment posting: Dr Brokerage Payable / Cr Bank.
- Automatic reversal on Sales Return / Claim.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.masters.models import Broker
from apps.sales.models import BrokerCommission


@transaction.atomic
def calculate_broker_commission(invoice, *, user=None, rate_override=None, commission_type_override=None):
    """Calculate and accrue broker commission on a finalized sales invoice."""
    broker = invoice.broker_fk
    if broker is None and invoice.broker:
        broker = Broker.objects.filter(
            client_id=invoice.client_id, name__iexact=invoice.broker, deleted_at__isnull=True
        ).first()

    # If still not found, check linked sales order / quotation / lead
    so = invoice.sales_order
    if broker is None and so and so.broker_fk:
        broker = so.broker_fk
    if broker is None and so and so.quotation and so.quotation.broker_fk:
        broker = so.quotation.broker_fk

    if broker is None:
        return None

    # Determine agreed terms from deal / lead if available
    agreed_type = None
    agreed_rate = None
    if so and so.quotation and so.quotation.crm_lead:
        lead = so.quotation.crm_lead
        cvals = lead.custom_values or {}
        agreed_type = cvals.get("commission_basis") or cvals.get("commission_type")
        if cvals.get("commission_rate") is not None:
            try:
                agreed_rate = D(str(cvals.get("commission_rate")))
            except Exception:
                pass

    comm_type = commission_type_override or agreed_type or broker.commission_type or "percent"
    rate = D(rate_override if rate_override is not None else (agreed_rate if agreed_rate is not None else broker.default_value or 0))

    # Basis: exclude GST (use taxable_value)
    taxable_val = D(invoice.taxable_value or invoice.total or 0)
    amount = commission_amount(invoice, comm_type, rate)

    comm_no = allocate_number(invoice.client_id, "BRK-COMM")
    commission, created = BrokerCommission.objects.update_or_create(
        client_id=invoice.client_id,
        invoice=invoice,
        broker=broker,
        defaults={
            "commission_number": comm_no,
            "party": invoice.party,
            "salesperson": invoice.salesperson_fk,
            "invoice_date": invoice.doc_date,
            "invoice_amount": taxable_val,
            "commission_basis": "QUANTITY" if comm_type in ("per_meter", "per_taka") else "INVOICE",
            "commission_type": comm_type,
            "rate": rate,
            "commission_amount": amount,
            "status": "Accrued",
        },
    )
    return commission


def commission_amount(invoice, comm_type, rate):
    """Brokerage on one invoice for a commission type and rate."""
    rate = D(rate or 0)
    if comm_type == "percent":
        # Basis: exclude GST (use taxable_value)
        return round2(D(invoice.taxable_value or invoice.total or 0) * rate / D(100))
    if comm_type == "per_meter":
        total_meters = D(invoice.line_items.aggregate(val=Sum("qty"))["val"] or ZERO)
        return round2(total_meters * rate)
    if comm_type == "per_taka":
        # Calculate eligible takas from invoice lines
        takas_count = 0
        for line in invoice.line_items.all():
            if getattr(line, "taka_count", None):
                takas_count += int(line.taka_count)
            elif line.line_taka_allocations.exists():
                takas_count += line.line_taka_allocations.count()
            else:
                takas_count += 1
        return round2(D(max(1, takas_count)) * rate)
    return round2(rate)  # fixed


# ---------------------------------------------------------------------------
# Rate changes go through approval (PDF §16 "Brokerage % change")
# ---------------------------------------------------------------------------
RATE_CHANGE_TYPE = "brokerage_rate_change"
RATE_APPROVER_ROLE_CODES = ["SALES_MGR", "OWNER"]
COMMISSION_TYPES = ("percent", "per_meter", "per_taka", "fixed")


def request_rate_change(target, *, rate, commission_type, user):
    """Hold a change of a broker's default rate or one commission's rate
    for approval. ``target`` is a ``masters.Broker`` or a ``BrokerCommission``."""
    from apps.core.approval_services import raise_approval

    is_commission = isinstance(target, BrokerCommission)
    old_rate = target.rate if is_commission else target.default_value
    old_type = target.commission_type
    new_type = commission_type or old_type
    if new_type not in COMMISSION_TYPES:
        raise ValidationFailed(
            "Unknown commission type.", field_errors={"commissionType": [f"One of {', '.join(COMMISSION_TYPES)}."]}
        )
    if is_commission and target.status in ("Paid", "Partially Paid", "Cancelled"):
        raise Conflict(
            f"Commission {target.commission_number} is {target.status}; its rate can no longer change.",
            code="COMMISSION_LOCKED",
        )
    label = target.commission_number if is_commission else (target.name or target.code)
    return raise_approval(
        client_id=target.client_id,
        document_type=RATE_CHANGE_TYPE,
        document_id=target.id,
        document_number=label,
        requested_by=user,
        reason=(
            f"Brokerage change on {'commission' if is_commission else 'broker'} {label}: "
            f"{old_type} {round4(D(old_rate or 0))} -> {new_type} {round4(D(rate))}"
        ),
        approver_role_code=RATE_APPROVER_ROLE_CODES[0],
        allowed_role_codes=RATE_APPROVER_ROLE_CODES,
        old_values={"rate": str(old_rate), "commissionType": old_type},
        new_values={
            "target": "BrokerCommission" if is_commission else "Broker",
            "rate": str(D(rate)),
            "commissionType": new_type,
        },
    )


@transaction.atomic
def apply_rate_change(req):
    """Apply an approved rate change. A commission keeps its number; its
    amount is recomputed, and if already approved its accrual is trued up."""
    values = req.new_values or {}
    rate = D(values["rate"])
    comm_type = values.get("commissionType")
    if values.get("target") == "Broker":
        broker = Broker.objects.select_for_update().get(client_id=req.client_id, id=req.document_id)
        broker.default_value = rate
        broker.commission_type = comm_type or broker.commission_type
        broker.save(update_fields=["default_value", "commission_type", "updated_at"])
        return broker

    commission = BrokerCommission.objects.select_for_update().select_related("invoice").get(
        client_id=req.client_id, id=req.document_id
    )
    if commission.status in ("Paid", "Partially Paid", "Cancelled"):
        raise Conflict(
            f"Commission {commission.commission_number} is {commission.status}; the rate change cannot apply.",
            code="COMMISSION_LOCKED",
        )
    commission.rate = rate
    commission.commission_type = comm_type or commission.commission_type
    commission.commission_basis = (
        "QUANTITY" if commission.commission_type in ("per_meter", "per_taka") else "INVOICE"
    )
    commission.commission_amount = commission_amount(commission.invoice, commission.commission_type, rate)
    commission.save(update_fields=[
        "rate", "commission_type", "commission_basis", "commission_amount", "updated_at",
    ])
    if commission.status in ("Approved", "Payable"):
        _sync_accrual(commission, user=req.decided_by, note="Brokerage rate change")
    return commission


ACCRUAL_SOURCE = "BrokerCommissionAccrual"


def _accrued_amount(commission):
    """Net brokerage payable booked for this commission (credits − debits)."""
    from apps.accounting.models import JournalLine

    rows = JournalLine.objects.filter(
        client_id=commission.client_id,
        journal_entry__source_document_type=ACCRUAL_SOURCE,
        journal_entry__source_document_id=commission.id,
        account__system_key="brokerage_payable",
        deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    totals = rows.aggregate(d=Sum("debit"), c=Sum("credit"))
    return round2(D(totals["c"] or ZERO) - D(totals["d"] or ZERO))


def _sync_accrual(commission, *, user=None, note="Brokerage accrual"):
    """Bring the booked accrual in line with ``commission_amount``:
    Dr Brokerage Expense / Cr Brokerage Payable (or the reverse to reduce)."""
    target = ZERO if commission.status == "Cancelled" else round2(D(commission.commission_amount))
    delta = round2(target - _accrued_amount(commission))
    if delta == ZERO:
        return None
    expense = ledger.system_account(commission.client_id, "brokerage_expense")
    payable = ledger.system_account(commission.client_id, "brokerage_payable")
    amount = abs(delta)
    postings = (
        [ledger.debit(expense, amount, description=note), ledger.credit(payable, amount, description=note)]
        if delta > ZERO else
        [ledger.debit(payable, amount, description=note), ledger.credit(expense, amount, description=note)]
    )
    return ledger.post_entry(
        client=commission.client_id,
        postings=postings,
        narration=f"{note}: {commission.commission_number or commission.id}",
        source_document_type=ACCRUAL_SOURCE,
        source_document_id=commission.id,
        user=user,
    )


@transaction.atomic
def approve_broker_commission(commission, *, user=None):
    """Transition commission: Accrued -> Approved -> Payable."""
    commission = BrokerCommission.objects.select_for_update().get(pk=commission.pk)
    if commission.status not in ("Accrued", "Hold"):
        raise Conflict(f"Commission is currently {commission.status}, cannot approve.")

    commission.status = "Approved"
    commission.approved_by = user if getattr(user, "is_authenticated", False) else None
    commission.approved_at = timezone.now()
    commission.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    # The expense is recognised once approved, so the payout has a payable to clear.
    _sync_accrual(commission, user=user)
    return commission


@transaction.atomic
def make_broker_payable(commission, *, user=None):
    """Mark approved commission as payable."""
    commission = BrokerCommission.objects.select_for_update().get(pk=commission.pk)
    if commission.status != "Approved":
        raise Conflict("Only Approved commissions can be marked as Payable.")

    commission.status = "Payable"
    commission.save(update_fields=["status", "updated_at"])
    return commission


@transaction.atomic
def pay_broker_commission(commission, amount=None, *, payment_ref=None, payment_method=None, bank_account=None, user=None, remarks=None):
    """Record broker payment and post Dr Brokerage Payable / Cr Bank."""
    commission = BrokerCommission.objects.select_for_update().get(pk=commission.pk)
    if commission.status not in ("Approved", "Payable", "Partially Paid"):
        raise Conflict(
            f"Commission is {commission.status}; only an approved / payable commission can be paid."
        )
    outstanding = round2(D(commission.commission_amount) - D(commission.paid_amount))

    pay_amount = round2(D(amount)) if amount is not None else outstanding
    if not pay_amount.is_finite() or pay_amount <= ZERO:
        raise ValidationFailed("Payment amount must be greater than zero.")
    if pay_amount > outstanding:
        raise ValidationFailed(
            f"Payment amount (₹ {pay_amount:,.2f}) cannot exceed outstanding balance (₹ {outstanding:,.2f})."
        )

    # Commissions approved before accruals were posted get theirs now.
    _sync_accrual(commission, user=user)

    # Post the payout; a failure rolls the whole payment back rather than
    # marking it Paid with nothing in the books.
    if bank_account is not None and not hasattr(bank_account, "account_id"):
        from apps.accounting.models import BankAccount
        bank_account = BankAccount.objects.filter(pk=bank_account, client_id=commission.client_id).first()
    credit_account = (
        bank_account.account if bank_account is not None and getattr(bank_account, "account_id", None)
        else ledger.system_account(commission.client_id, "bank")
    )
    entry = ledger.post_entry(
        client=commission.client_id,
        postings=[
            ledger.debit(
                ledger.system_account(commission.client_id, "brokerage_payable"),
                pay_amount,
                description=f"Brokerage payout to {commission.broker.name} ({payment_method or 'Bank Transfer'})",
            ),
            ledger.credit(
                credit_account,
                pay_amount,
                description=f"Brokerage payout for invoice {commission.invoice.invoice_number}",
            ),
        ],
        narration=f"Broker payment {commission.commission_number or commission.id}: {remarks or ''}",
        source_document_type="BrokerCommission",
        source_document_id=commission.id,
        user=user,
    )
    commission.journal_entry = entry

    new_total_paid = round2(D(commission.paid_amount) + pay_amount)
    commission.paid_amount = new_total_paid
    if new_total_paid >= D(commission.commission_amount):
        commission.status = "Paid"
    else:
        commission.status = "Partially Paid"
    commission.paid_at = timezone.now()
    if payment_ref:
        commission.payment_reference = payment_ref
    if remarks:
        commission.notes = (commission.notes or "") + f" [Paid ₹{pay_amount:,.2f}: {remarks}]"
    commission.save(update_fields=["paid_amount", "status", "paid_at", "payment_reference", "notes", "journal_entry", "updated_at"])
    return commission


@transaction.atomic
def reverse_broker_commission_for_invoice(invoice, *, user=None, return_val=None, reason=None):
    """Adjust or cancel broker commission when a sales return or credit note occurs."""
    commissions = BrokerCommission.objects.filter(
        client_id=invoice.client_id, invoice=invoice, deleted_at__isnull=True
    )
    for comm in commissions:
        if comm.status in ("Accrued", "Approved", "Payable", "Partially Paid"):
            if return_val and D(return_val) < D(comm.invoice_amount) and D(comm.invoice_amount) > ZERO:
                # Proportional reduction
                reduction = round2(D(comm.commission_amount) * (D(return_val) / D(comm.invoice_amount)))
                comm.commission_amount = max(ZERO, round2(D(comm.commission_amount) - reduction))
                comm.notes = (comm.notes or "") + f" [Adjusted -₹{reduction} on return: {reason or 'Return'}]"
                comm.save(update_fields=["commission_amount", "notes", "updated_at"])
            else:
                comm.status = "Cancelled"
                comm.notes = (comm.notes or "") + f" [Auto-cancelled on return: {reason or 'Sales Return'}]"
                comm.save(update_fields=["status", "notes", "updated_at"])
            # Keep the booked expense/payable equal to what is still owed.
            _sync_accrual(comm, user=user, note="Brokerage adjustment on return")
        elif comm.status == "Paid":
            if comm.journal_entry_id:
                try:
                    ledger.reverse_document_entries(
                        client_id=comm.client_id,
                        source_document_type="BrokerCommission",
                        source_document_id=comm.id,
                        user=user,
                    )
                except Exception:
                    pass
            comm.status = "Cancelled"
            comm.notes = (comm.notes or "") + f" [Reversed on return: {reason or 'Sales Return'}]"
            comm.save(update_fields=["status", "notes", "updated_at"])
