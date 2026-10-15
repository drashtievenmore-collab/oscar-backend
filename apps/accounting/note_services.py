"""Standalone Debit and Credit Notes services (PDF §14).

Handles standalone rate difference, discount, and return debit/credit notes
with two-step approval and journal entry posting.
"""
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

from . import services as ledger
from .models import StandaloneNote


@transaction.atomic
def create_standalone_note(
    *,
    client,
    party,
    note_type="CREDIT_NOTE",
    taxable_amount=ZERO,
    cgst=ZERO,
    sgst=ZERO,
    igst=ZERO,
    against_document_type="INVOICE",
    against_document_id=None,
    against_document_number=None,
    return_reason=None,
    reason_notes=None,
    notes=None,
    user=None,
    status="Draft",
):
    """Create a new Standalone Debit or Credit Note."""
    taxable = round2(D(taxable_amount or 0))
    c_tax = round2(D(cgst or 0))
    s_tax = round2(D(sgst or 0))
    i_tax = round2(D(igst or 0))
    total = round2(taxable + c_tax + s_tax + i_tax)

    if total <= ZERO:
        raise ValidationFailed(
            "Note total must be greater than zero.",
            field_errors={"taxableAmount": ["Must be greater than zero."]},
        )

    client_id = getattr(client, "id", client)
    prefix = "CN" if note_type == "CREDIT_NOTE" else "DN"
    note_number = allocate_number(client, prefix)

    note = StandaloneNote.objects.create(
        client=client,
        note_type=note_type,
        note_number=note_number,
        note_date=timezone.localdate(),
        party=party,
        against_document_type=against_document_type,
        against_document_id=against_document_id,
        against_document_number=against_document_number,
        return_reason=return_reason,
        reason_notes=reason_notes,
        taxable_amount=taxable,
        cgst=c_tax,
        sgst=s_tax,
        igst=i_tax,
        total_amount=total,
        status=status or "Draft",
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    record_audit(
        client=client_id,
        actor=user,
        action="standalone_note_create",
        entity_type="StandaloneNote",
        entity_id=note.id,
        entity_label=note.note_number,
        description=f"{note.get_note_type_display()} {note.note_number} created for {party.name} (₹{total}).",
        after={"status": note.status, "total": str(total)},
    )
    return note


@transaction.atomic
def approve_standalone_note(note, *, user=None):
    """Draft / Pending Approval → Approved (PDF §14)."""
    note = note.__class__.objects.select_for_update().get(pk=note.pk)
    if note.status not in ("Draft", "Pending Approval"):
        raise ValidationFailed(
            f"Cannot approve note in status '{note.status}'.",
            field_errors={"status": ["Must be Draft or Pending Approval."]},
        )

    before = {"status": note.status}
    note.status = "Approved"
    note.approved_by = user if getattr(user, "is_authenticated", False) else None
    note.approved_at = timezone.now()
    note.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    record_audit(
        client=note.client_id,
        actor=user,
        action="standalone_note_approve",
        entity_type="StandaloneNote",
        entity_id=note.id,
        entity_label=note.note_number,
        description=f"{note.get_note_type_display()} {note.note_number} approved.",
        before=before,
        after={"status": "Approved"},
    )
    return note


@transaction.atomic
def post_standalone_note(note, *, user=None):
    """Approved → Posted (PDF §14). Posts journal entry to ledger."""
    note = note.__class__.objects.select_for_update().get(pk=note.pk)
    if note.status not in ("Approved", "Draft"):
        raise ValidationFailed(
            f"Note must be approved before posting (currently '{note.status}').",
            field_errors={"status": ["Must be Approved."]},
        )
    if note.journal_entry_id:
        raise Conflict("Note has already been posted to the ledger.", code=Codes.ALREADY_DONE)

    client = note.client
    client_id = note.client_id
    total = note.total_amount
    taxable = note.taxable_amount
    party = note.party

    is_customer = party.type in ("Customer", "Both")
    postings = []

    if note.note_type == "CREDIT_NOTE":
        if is_customer:
            # Credit note to customer (e.g. rate difference discount, rebate):
            # Dr Sales Returns / General Expense
            # Cr Sundry Debtors (Party)
            sales_ret_acc = ledger.system_account(client_id, "sales_return")
            debtors_acc = ledger.system_account(client_id, "debtors")
            postings.append(
                ledger.debit(sales_ret_acc, total, description=f"Credit note {note.note_number} - {party.name}")
            )
            postings.append(
                ledger.credit(debtors_acc, total, party=party, description=f"Credit note {note.note_number}")
            )
        else:
            # Credit note received from vendor:
            # Dr Sundry Creditors (Party)
            # Cr Purchase Returns / General Expense
            creditors_acc = ledger.system_account(client_id, "creditors")
            purch_ret_acc = ledger.system_account(client_id, "purchase_return")
            postings.append(
                ledger.debit(creditors_acc, total, party=party, description=f"Credit note {note.note_number}")
            )
            postings.append(
                ledger.credit(purch_ret_acc, total, description=f"Credit note from {party.name}")
            )
    else:  # DEBIT_NOTE
        if is_customer:
            # Debit note issued to customer (e.g. price increase, undercharge):
            # Dr Sundry Debtors (Party)
            # Cr Sales
            debtors_acc = ledger.system_account(client_id, "debtors")
            sales_acc = ledger.system_account(client_id, "sales")
            postings.append(
                ledger.debit(debtors_acc, total, party=party, description=f"Debit note {note.note_number}")
            )
            postings.append(
                ledger.credit(sales_acc, total, description=f"Debit note {note.note_number} - {party.name}")
            )
        else:
            # Debit note to vendor (e.g. rate difference debit, penalty, shortage):
            # Dr Sundry Creditors (Party)
            # Cr Purchase Returns / Other Direct Income
            creditors_acc = ledger.system_account(client_id, "creditors")
            purch_ret_acc = ledger.system_account(client_id, "purchase_return")
            postings.append(
                ledger.debit(creditors_acc, total, party=party, description=f"Debit note {note.note_number}")
            )
            postings.append(
                ledger.credit(purch_ret_acc, total, description=f"Debit note to {party.name}")
            )

    entry = ledger.post_entry(
        client=client,
        postings=postings,
        narration=f"Standalone {note.get_note_type_display()} {note.note_number} ({note.reason_notes or 'Rate diff / discount'})",
        source_document_type="StandaloneNote",
        source_document_id=note.id,
        user=user,
    )

    note.journal_entry = entry
    note.status = "Posted"
    note.save(update_fields=["journal_entry", "status", "updated_at"])

    record_audit(
        client=client_id,
        actor=user,
        action="standalone_note_post",
        entity_type="StandaloneNote",
        entity_id=note.id,
        entity_label=note.note_number,
        description=f"{note.get_note_type_display()} {note.note_number} posted to ledger.",
        after={"status": "Posted", "journalEntryId": str(entry.id) if entry else None},
    )
    return note


@transaction.atomic
def cancel_standalone_note(note, *, user=None, reason=None):
    """Cancel standalone note and reverse journal entry if posted."""
    note = note.__class__.objects.select_for_update().get(pk=note.pk)
    if note.status == "Cancelled":
        raise Conflict("Note is already cancelled.", code=Codes.ALREADY_DONE)

    if note.journal_entry_id:
        ledger.reverse_document_entries(
            client_id=note.client_id,
            source_document_type="StandaloneNote",
            source_document_id=note.id,
            user=user,
        )

    before = {"status": note.status}
    note.status = "Cancelled"
    note.notes = f"{note.notes or ''}\nCancelled: {reason or 'No reason provided'}".strip()
    note.save(update_fields=["status", "notes", "updated_at"])

    record_audit(
        client=note.client_id,
        actor=user,
        action="standalone_note_cancel",
        entity_type="StandaloneNote",
        entity_id=note.id,
        entity_label=note.note_number,
        description=f"{note.get_note_type_display()} {note.note_number} cancelled: {reason or ''}.",
        before=before,
        after={"status": "Cancelled"},
    )
    return note
