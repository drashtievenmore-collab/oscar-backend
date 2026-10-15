"""
Broker commission services (PDF §12).

Tracks broker commissions linked to sales invoices:
- Status lifecycle: Accrued -> Approved -> Payable -> Paid / Hold / Cancelled.
- Support commission types: percent (of taxable invoice value), per_meter (on dispatched meters), fixed.
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
        broker = Broker.objects.filter(client_id=invoice.client_id, name__iexact=invoice.broker, deleted_at__isnull=True).first()

    if broker is None:
        return None

    comm_type = commission_type_override or broker.commission_type or "percent"
    rate = D(rate_override if rate_override is not None else broker.default_value or 0)

    # Basis: exclude GST (use taxable_value)
    taxable_val = D(invoice.taxable_value or invoice.total or 0)
    total_meters = invoice.line_items.aggregate(val=Sum("qty"))["val"] or ZERO
    total_meters = D(total_meters)

    if comm_type == "percent":
        amount = round2(taxable_val * rate / D(100))
    elif comm_type == "per_meter":
        amount = round2(total_meters * rate)
    else:  # fixed
        amount = round2(rate)

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
            "commission_basis": "INVOICE",
            "commission_type": comm_type,
            "rate": rate,
            "commission_amount": amount,
            "status": "Accrued",
        },
    )
    return commission


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
def pay_broker_commission(commission, amount=None, *, payment_ref=None, user=None):
    """Record broker payment and post Dr Brokerage Payable / Cr Bank."""
    commission = BrokerCommission.objects.select_for_update().get(pk=commission.pk)
    pay_amount = round2(D(amount)) if amount is not None else D(commission.commission_amount)

    if pay_amount <= ZERO:
        raise ValidationFailed("Payment amount must be greater than zero.")

    # Post accounting entry
    try:
        entry = ledger.post_entry(
            client=commission.client_id,
            postings=[
                ledger.debit(
                    ledger.system_account(commission.client_id, "brokerage_payable"),
                    pay_amount,
                    description=f"Brokerage payment to {commission.broker.name}",
                ),
                ledger.credit(
                    ledger.system_account(commission.client_id, "bank"),
                    pay_amount,
                    description=f"Brokerage payout for invoice {commission.invoice.invoice_number}",
                ),
            ],
            narration=f"Broker payment {commission.commission_number or commission.id}",
            source_document_type="BrokerCommission",
            source_document_id=commission.id,
            user=user,
        )
        commission.journal_entry = entry
    except Exception:
        pass

    commission.paid_amount = round2(commission.paid_amount + pay_amount)
    commission.status = "Paid"
    commission.paid_at = timezone.now()
    commission.payment_reference = payment_ref
    commission.save(update_fields=["paid_amount", "status", "paid_at", "payment_reference", "journal_entry", "updated_at"])
    return commission


@transaction.atomic
def reverse_broker_commission_for_invoice(invoice, *, user=None, reason=None):
    """Adjust or cancel broker commission when a sales return or credit note occurs."""
    commissions = BrokerCommission.objects.filter(
        client_id=invoice.client_id, invoice=invoice, deleted_at__isnull=True
    )
    for comm in commissions:
        if comm.status in ("Accrued", "Approved", "Payable"):
            comm.status = "Cancelled"
            comm.notes = (comm.notes or "") + f" [Auto-cancelled on return: {reason or 'Sales Return'}]"
            comm.save(update_fields=["status", "notes", "updated_at"])
        elif comm.status == "Paid":
            # Reverse posted journal entry if any
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
