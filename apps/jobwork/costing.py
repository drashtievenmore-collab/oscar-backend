"""Job-work costs: lot cost ledger and the books (PDF §08, §09).

Processing, reprocess and other job-work charges are direct costs of the
finished lot. They are posted to the lot cost ledger (LotCostEntry) and, so
they also reach the books, accrued as Dr Inventory / Cr Job Work Charges
Payable against the processor.

Both ledgers are append-only: re-saving a row posts only the difference
between what it should cost now and what was already posted for it.
"""
import logging

from django.db.models import Sum

from apps.accounting import services as ledger
from apps.core.money import ZERO, D, round2, round4

logger = logging.getLogger(__name__)

ACCRUAL_ACCOUNT = "jobwork_payable"


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------
def _accrued(order, source_type, source_id):
    from apps.accounting.models import JournalLine

    rows = JournalLine.objects.filter(
        client_id=order.client_id,
        journal_entry__source_document_type=source_type,
        journal_entry__source_document_id=source_id,
        account__system_key=ACCRUAL_ACCOUNT,
        deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    totals = rows.aggregate(d=Sum("debit"), c=Sum("credit"))
    return round2(D(totals["c"] or ZERO) - D(totals["d"] or ZERO))


def sync_jobwork_accrual(order, *, source_type, source_id, amount, narration, user=None):
    """Bring the accrual for one job-work row to ``amount``.

    Dr Inventory / Cr Job Work Charges Payable (processor), or the reverse
    when the row now costs less (edited down, cancelled or deleted).
    """
    target = round2(D(amount or 0))
    delta = round2(target - _accrued(order, source_type, source_id))
    if delta == ZERO:
        return None
    stock = ledger.system_account(order.client_id, "stock")
    payable = ledger.system_account(order.client_id, ACCRUAL_ACCOUNT)
    value = abs(delta)
    party = order.vendor
    if delta > ZERO:
        postings = [
            ledger.debit(stock, value, description=narration),
            ledger.credit(payable, value, party=party, description=narration),
        ]
    else:
        postings = [
            ledger.debit(payable, value, party=party, description=narration),
            ledger.credit(stock, value, description=narration),
        ]
    return ledger.post_entry(
        client=order.client_id,
        postings=postings,
        narration=f"{narration} ({order.jwo_no})",
        source_document_type=source_type,
        source_document_id=source_id,
        user=user,
    )


# ---------------------------------------------------------------------------
# Lot cost ledger
# ---------------------------------------------------------------------------
def _posted(client_id, source_type, source_id, lot_number):
    from apps.inventory.models import LotCostEntry

    total = LotCostEntry.objects.filter(
        client_id=client_id,
        source_document_type=source_type,
        source_document_id=source_id,
        lot_number=lot_number,
        deleted_at__isnull=True,
    ).aggregate(v=Sum("amount"))["v"]
    return D(total or ZERO)


def total_posted(client_id, source_type, source_id):
    """Everything on the lot cost ledger for one source row, across lots."""
    from apps.inventory.models import LotCostEntry

    total = LotCostEntry.objects.filter(
        client_id=client_id,
        source_document_type=source_type,
        source_document_id=source_id,
        deleted_at__isnull=True,
    ).aggregate(v=Sum("amount"))["v"]
    return D(total or ZERO)


def reprocess_source_lots(order, reprocess):
    """The lots a reprocess row works on, with their received metres.

    The row names the receipt it reprocesses (``inward_no``): a JWO inward
    of this order, or a GRN. Without one, every lot received on the order.
    """
    from apps.inventory.models import LotTaka
    from apps.jobwork.models import JWOInwardTaka

    takas = JWOInwardTaka.objects.filter(
        inward__job_work_order=order,
        inward__deleted_at__isnull=True,
        received_taka__isnull=False,
    ).select_related("received_taka")
    if reprocess.inward_no:
        named = takas.filter(inward__no=reprocess.inward_no)
        if named.exists():
            takas = named
        else:
            grn_lots = LotTaka.objects.filter(
                client_id=order.client_id,
                source_document_number=reprocess.inward_no,
                deleted_at__isnull=True,
            )
            if grn_lots.exists():
                return _group([(t.lot_number, D(t.received_quantity or 0)) for t in grn_lots])
    lots = _group([(t.received_taka.lot_number, D(t.meters or 0)) for t in takas])
    if lots:
        return lots
    # A receipt entered as a quantity only has no received takas: cost the
    # lots that were sent out on this order instead.
    from apps.jobwork.models import JWOOutwardTaka

    sent = JWOOutwardTaka.objects.filter(
        outward__job_work_order=order, outward__deleted_at__isnull=True, lot_taka__isnull=False,
    ).select_related("lot_taka")
    return _group([(t.lot_taka.lot_number, D(t.meters or 0)) for t in sent])


def _group(pairs):
    lots = {}
    for lot_number, meters in pairs:
        if lot_number:
            lots[lot_number] = lots.get(lot_number, ZERO) + meters
    return lots


def _split(amount, weights):
    """Split ``amount`` across ``weights`` (by metres); rounding goes to the last."""
    total = sum(weights.values(), ZERO)
    keys = list(weights)
    if not keys:
        return {}
    if total <= ZERO:
        weights = {k: D(1) for k in keys}
        total = D(len(keys))
    shares, given = {}, ZERO
    for key in keys[:-1]:
        share = round2(amount * weights[key] / total)
        shares[key] = share
        given += share
    shares[keys[-1]] = round2(amount - given)
    return shares


def sync_reprocess_cost(order, reprocess, *, rate=None, user=None):
    """Post a reprocess round's charge to the lots being reprocessed.

    PDF §08: reprocess cost is added to the same lot's cost history, never
    overwriting the original processing entry, so the lot's landed cost per
    good metre includes every round. ``rate`` is per metre; when omitted the
    rate already posted for this row (else the order's rate) is kept.
    """
    from apps.inventory import costing_services

    source_type = "JWOReprocess"
    lots = reprocess_source_lots(order, reprocess)
    if not lots:
        # No taka-level detail anywhere on the order: the charge still
        # reaches the books, but there is no lot to load it on.
        logger.warning(
            "Reprocess %s on JWO %s has no lot to cost; accruing the charge only.",
            reprocess.no, order.jwo_no,
        )
    already = sum((_posted(order.client_id, source_type, reprocess.id, lot) for lot in lots), ZERO)
    qty = D(reprocess.qty or 0)
    if rate is None:
        # Keep the rate already charged for this round (amount / metres posted).
        from apps.inventory.models import LotCostEntry

        posted = LotCostEntry.objects.filter(
            client_id=order.client_id, source_document_type=source_type,
            source_document_id=reprocess.id, deleted_at__isnull=True,
        ).aggregate(a=Sum("amount"), q=Sum("quantity"))
        posted_qty = D(posted["q"] or ZERO)
        rate = (D(posted["a"] or ZERO) / posted_qty) if posted_qty > ZERO else D(order.rate or 0)
    target = ZERO if reprocess.status == "Cancelled" else round2(D(rate) * qty)

    for lot_number, share in _split(target, lots).items():
        delta = round2(share - _posted(order.client_id, source_type, reprocess.id, lot_number))
        if delta == ZERO:
            continue
        costing_services.post_lot_cost(
            client_id=order.client_id,
            lot_number=lot_number,
            cost_type="Reprocess",
            amount=delta,
            # Metres of this posting, so amount / metres stays the round's rate.
            quantity=round4(delta / D(rate)) if D(rate) else ZERO,
            source_document_type=source_type,
            source_document_id=reprocess.id,
            source_document_number=reprocess.no or order.jwo_no,
            user=user,
            is_approved=True,
            remarks=(
                f"Reprocess {reprocess.no} on JWO {order.jwo_no}"
                + (" (adjustment)" if already else "")
            ),
        )
    sync_jobwork_accrual(
        order, source_type=source_type, source_id=reprocess.id, amount=target,
        narration=f"Reprocess charges {reprocess.no}", user=user,
    )
    return target
