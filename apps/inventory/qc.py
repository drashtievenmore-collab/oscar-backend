"""
QC disposition of received goods (Phase 1.4).

Goods received on a purchase bill are posted to the per-client **QC Hold**
system location. Nothing is available for sale until a QC disposition moves it
out, taka by taka, with real movements:

    good           -> QC_PASS            -> the receipt's own (godown) location
    second quality -> QC_SECOND_QUALITY  -> Second Quality Stock
    reprocess      -> QC_REPROCESS       -> Reprocess Stock
    reject         -> QC_REJECT          -> Reject / Scrap
    hold           -> stays at QC Hold (taka marked On Hold)

Every GRN line is handled -- never just ``lines.first()`` -- and a partly
dispositioned taka is split so one taka record never spans two locations.
"""
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.exceptions import Conflict, ValidationFailed
from apps.core.money import ZERO, D, round4

from . import services as stock
from .models import LotTaka, StockMovement

#: Allocation order: a taka is filled category by category in this order, so
#: ``hold`` (which does not move) always takes whatever is left at QC Hold.
CATEGORIES = ("good", "second", "reprocess", "reject", "hold")

CATEGORY_CONDITION = {
    "good": "Good",
    "second": "Second Quality",
    "reprocess": "Reprocess",
    "reject": "Reject",
    "hold": "Hold",
}

CATEGORY_MOVEMENT = {
    "good": "QC_PASS",
    "second": "QC_SECOND_QUALITY",
    "reprocess": "QC_REPROCESS",
    "reject": "QC_REJECT",
    "hold": "QC_HOLD",
}

#: Request keys accepted for each category.
CATEGORY_KEYS = {
    "good": ("goodQty",),
    "second": ("secondQualityQty", "secondQty"),
    "reprocess": ("reprocessQty",),
    "reject": ("rejectedQty", "rejectQty"),
    "hold": ("holdQty",),
}

#: GRN verdict -> purchase-bill QC status (both vocabularies are validated).
BILL_STATUS_FOR = {
    "Approved": "Approved",
    "Partially Approved": "Partially Approved",
    "On Hold": "Pending Approval",
    "Rework": "Rework",
    "Rejected": "Rejected",
}

TOLERANCE = Decimal("0.001")


def parse_quantities(data):
    """``{category: Decimal}`` from a request body; negatives are refused."""
    result = {}
    for category, keys in CATEGORY_KEYS.items():
        raw = next((data.get(key) for key in keys if data.get(key) not in (None, "")), None)
        try:
            value = round4(D(raw or 0))
        except Exception:  # noqa: BLE001 - reported as a field error
            raise ValidationFailed(
                f"Invalid {category} quantity.", field_errors={keys[0]: ["Not a number."]}
            )
        if value < ZERO:
            raise ValidationFailed(
                "Disposition quantities must not be negative.",
                field_errors={keys[0]: ["Must be zero or more."]},
            )
        result[category] = value
    return result


def verdict_for(cumulative, hold_now):
    """The GRN QC status implied by everything dispositioned so far."""
    if hold_now > ZERO:
        return "On Hold"
    passed = cumulative.get("good", ZERO) + cumulative.get("second", ZERO)
    if passed <= ZERO:
        return "Rework" if cumulative.get("reprocess", ZERO) > ZERO else "Rejected"
    if (
        cumulative.get("reprocess", ZERO)
        + cumulative.get("reject", ZERO)
        + cumulative.get("second", ZERO)
    ) > ZERO:
        return "Partially Approved"
    return "Approved"


def _references(receipt):
    """Movements carry the bill as reference (so cancelling the bill reverses
    them) and the GRN as source document."""
    if receipt.purchase_bill_id:
        bill = receipt.purchase_bill
        return "PurchaseBill", bill.id, bill.bill_number or receipt.grn_number
    return "GoodsReceipt", receipt.id, receipt.grn_number


def receipt_has_stock(receipt):
    return StockMovement.objects.filter(
        client_id=receipt.client_id, source_document_id=receipt.id
    ).exists()


def receipt_pool(receipt):
    """The stock of ``receipt`` still awaiting QC, as a list of units.

    A unit is one taka (``lot`` set) or, for items not held on takas, the
    item's quantity held at QC Hold for this receipt.
    """
    qc_location = stock.system_location(receipt.client_id, "QC_HOLD")
    lines = list(receipt.lines.filter(deleted_at__isnull=True).select_related("item"))
    line_by_id = {line.id: line for line in lines}

    def line_for(item_id, line_id=None):
        if line_id and line_id in line_by_id:
            return line_by_id[line_id]
        return next((line for line in lines if line.item_id == item_id), None)

    units = []
    lot_item_ids = set(
        LotTaka.objects.filter(
            client_id=receipt.client_id, source_document_id=receipt.id
        ).values_list("item_id", flat=True)
    )
    lots = LotTaka.objects.filter(
        client_id=receipt.client_id,
        source_document_id=receipt.id,
        location_id=qc_location.id,
        available_quantity__gt=0,
        deleted_at__isnull=True,
    ).order_by("created_at", "taka_number")
    for lot in lots:
        units.append({
            "line": line_for(lot.item_id, lot.source_line_id),
            "item_id": lot.item_id,
            "lot": lot,
            "qty": round4(lot.available_quantity),
            "from": qc_location.id,
        })

    seen_items = set()
    for line in lines:
        if line.item_id in lot_item_ids or line.item_id in seen_items:
            continue
        seen_items.add(line.item_id)
        held = StockMovement.objects.filter(
            client_id=receipt.client_id,
            source_document_id=receipt.id,
            item_id=line.item_id,
            location_id=qc_location.id,
        ).aggregate(total=Sum("quantity"))["total"] or ZERO
        held = round4(held)
        if held > ZERO:
            units.append({
                "line": line, "item_id": line.item_id, "lot": None,
                "qty": held, "from": qc_location.id,
            })
    return units, qc_location


def pool_quantity(receipt):
    units, _ = receipt_pool(receipt)
    return round4(sum((unit["qty"] for unit in units), ZERO))


def _allocate(units, quantities):
    """Spread the category totals over the units in order -> [(unit, cat, qty)]."""
    remaining = dict(quantities)
    plan = []
    for unit in units:
        unit_left = unit["qty"]
        for category in CATEGORIES:
            if unit_left <= ZERO:
                break
            take = min(remaining.get(category, ZERO), unit_left)
            if take > ZERO:
                plan.append((unit, category, round4(take)))
                remaining[category] -= take
                unit_left -= take
    return plan


def _load_meta(receipt):
    note = (receipt.qc_note or "").strip()
    if note.startswith("{") and note.endswith("}"):
        try:
            return json.loads(note)
        except ValueError:
            return {}
    return {}


@transaction.atomic
def dispose_receipt(
    receipt,
    *,
    quantities,
    line_quantities=None,
    user=None,
    remarks=None,
    measurements=None,
    job_work_order=None,
    require_job_work_order=True,
    movement_date=None,
):
    """Apply a QC result to a goods receipt and move its stock accordingly.

    ``quantities`` is ``{category: qty}`` for the whole receipt (in stock
    units); ``line_quantities`` optionally gives the same per GRN line as
    ``[{"lineId": .., "goodQty": .., ...}]`` and then takes precedence.
    The quantities must account for exactly what is still at QC Hold.
    """
    from apps.purchase.models import GoodsReceipt, PurchaseBill

    receipt = (
        GoodsReceipt.objects.select_for_update(of=("self",))
        .select_related("purchase_bill", "party")
        .get(pk=receipt.pk)
    )
    units, qc_location = receipt_pool(receipt)
    pool_total = round4(sum((unit["qty"] for unit in units), ZERO))
    record_only = False

    if line_quantities:
        plan = []
        totals = {category: ZERO for category in CATEGORIES}
        for row in line_quantities:
            line_id = str(row.get("lineId") or row.get("line_id") or "")
            line_units = [u for u in units if u["line"] is not None and str(u["line"].id) == line_id]
            line_pool = round4(sum((u["qty"] for u in line_units), ZERO))
            row_qty = parse_quantities(row)
            row_total = round4(sum(row_qty.values(), ZERO))
            if abs(row_total - line_pool) > TOLERANCE:
                raise ValidationFailed(
                    f"Line quantities ({row_total}) must equal the quantity awaiting QC "
                    f"on that line ({line_pool}).",
                    field_errors={"lines": [f"Line {line_id}: expected {line_pool}."]},
                )
            plan.extend(_allocate(line_units, row_qty))
            for category, value in row_qty.items():
                totals[category] += value
        quantities = totals
    else:
        quantities = {category: round4(D(quantities.get(category) or 0)) for category in CATEGORIES}

    requested_total = round4(sum(quantities.values(), ZERO))
    if requested_total <= ZERO:
        raise ValidationFailed(
            "Enter the disposition quantities before approving.",
            field_errors={"goodQty": ["Total must be greater than zero."]},
        )

    if not units:
        if receipt_has_stock(receipt):
            raise Conflict(
                f"Nothing from {receipt.grn_number} is awaiting QC -- it has already been "
                "dispositioned.",
                code="QC_ALREADY_DONE",
            )
        # A manual register entry that never posted stock: record the result
        # only. No movement is claimed.
        record_only = True
        plan = []
    elif not line_quantities:
        if abs(requested_total - pool_total) > TOLERANCE:
            raise ValidationFailed(
                f"Disposition total ({requested_total}) must equal the quantity awaiting "
                f"QC for {receipt.grn_number} ({pool_total}).",
                field_errors={"goodQty": [f"Expected a total of {pool_total}."]},
            )
        plan = _allocate(units, quantities)

    if quantities["reprocess"] > ZERO and job_work_order is None and require_job_work_order:
        raise ValidationFailed(
            "Choose the job work order the reprocess quantity is sent on.",
            field_errors={"jwoId": ["Required when reprocess quantity is entered."]},
        )

    reference_type, reference_id, reference_number = _references(receipt)
    good_location_id = receipt.location_id
    if str(good_location_id) == str(qc_location.id):
        raise ValidationFailed(
            f"{receipt.grn_number} has no godown location to release good stock to.",
            field_errors={"locationId": ["The receipt location is the QC Hold location."]},
        )
    on_date = movement_date or timezone.localdate()
    note = f"QC {receipt.grn_number}" + (f": {remarks}" if remarks else "")

    reject_by_line = {}
    moved = 0
    for unit, category, qty in plan:
        condition = CATEGORY_CONDITION[category]
        _, lot_qc_status = stock.CONDITION_ROUTING[condition]
        if category == "hold":
            if unit["lot"] is not None:
                LotTaka.objects.filter(pk=unit["lot"].pk).update(
                    qc_status="On Hold", qc_notes=remarks or None, updated_at=timezone.now()
                )
            continue
        destination = stock.condition_location_id(
            receipt.client_id, condition, good_location_id
        )
        movement_type = CATEGORY_MOVEMENT[category]
        if unit["lot"] is not None:
            target, movements = stock.relocate_lot(
                client_id=receipt.client_id,
                lot=unit["lot"],
                quantity=qty,
                to_location_id=destination,
                out_type=movement_type,
                in_type=movement_type,
                reference_type=reference_type,
                reference_id=reference_id,
                reference_number=reference_number,
                source_document_type="GoodsReceipt",
                source_document_id=receipt.id,
                movement_date=on_date,
                notes=note,
                user=user,
                qc_status=lot_qc_status,
                split_suffix="QC",
            )
            if remarks:
                LotTaka.objects.filter(pk=target.pk).update(qc_notes=remarks)
        else:
            line = unit["line"]
            unit_cost = D(line.unit_cost) if line is not None and line.unit_cost else None
            if unit_cost is not None and line.uom_factor:
                unit_cost = unit_cost / D(line.uom_factor)
            movements = stock.relocate_item_qty(
                client_id=receipt.client_id,
                item=unit["item_id"],
                quantity=qty,
                from_location_id=unit["from"],
                to_location_id=destination,
                out_type=movement_type,
                in_type=movement_type,
                reference_type=reference_type,
                reference_id=reference_id,
                reference_number=reference_number,
                source_document_type="GoodsReceipt",
                source_document_id=receipt.id,
                movement_date=on_date,
                notes=note,
                user=user,
                unit_cost=unit_cost,
            )
        moved += len(movements)
        if category == "reject" and unit["line"] is not None:
            reject_by_line[unit["line"].id] = reject_by_line.get(unit["line"].id, ZERO) + qty

    # Rejected quantity per GRN line, in the line's (purchase) unit.
    for line in receipt.lines.filter(id__in=list(reject_by_line.keys())):
        factor = D(line.uom_factor or 1) or Decimal("1")
        added = round4(reject_by_line[line.id] / factor)
        line.rejected_qty = min(round4(D(line.rejected_qty) + added), D(line.received_qty))
        line.save(update_fields=["rejected_qty", "updated_at"])

    reprocess_order = None
    if quantities["reprocess"] > ZERO and job_work_order is not None:
        reprocess_order = _create_reprocess_order(
            receipt, job_work_order, quantities["reprocess"], remarks, user
        )

    meta = _load_meta(receipt)
    cumulative = {
        category: D(str(meta.get("cumulative", {}).get(category, 0)))
        for category in ("good", "second", "reprocess", "reject")
    }
    for category in ("good", "second", "reprocess", "reject"):
        cumulative[category] += quantities[category]
    verdict = verdict_for(cumulative, quantities["hold"])

    previous_status = receipt.qc_status
    meta = {
        "goodQty": float(cumulative["good"]),
        "secondQualityQty": float(cumulative["second"]),
        "reprocessQty": float(cumulative["reprocess"]),
        "rejectedQty": float(cumulative["reject"]),
        "holdQty": float(quantities["hold"]),
        "cumulative": {k: str(v) for k, v in cumulative.items()},
        "remarks": remarks or "",
        "measurements": measurements or meta.get("measurements") or [],
        "stockPosted": not record_only,
        "reprocessNo": getattr(reprocess_order, "no", None) or meta.get("reprocessNo"),
        "updatedAt": timezone.now().isoformat(),
    }
    receipt.qc_status = verdict
    receipt.qc_note = json.dumps(meta)
    receipt.qc_at = timezone.now()
    receipt.qc_by = user if getattr(user, "is_authenticated", False) else None
    receipt.save(update_fields=["qc_status", "qc_note", "qc_at", "qc_by", "updated_at"])

    if receipt.purchase_bill_id:
        PurchaseBill.objects.filter(pk=receipt.purchase_bill_id).update(
            qc_status=BILL_STATUS_FOR[verdict],
            qc_note=remarks or None,
            updated_at=timezone.now(),
        )

    from apps.core.audit import record_audit

    record_audit(
        client=receipt.client_id,
        actor=user,
        action="qc_disposition",
        entity_type="GoodsReceipt",
        entity_id=receipt.id,
        entity_label=receipt.grn_number,
        description=f"QC disposition: {verdict}",
        from_value=previous_status,
        to_value=verdict,
        after={k: str(v) for k, v in quantities.items()},
        comments=remarks or None,
    )

    return {
        "verdict": verdict,
        "receipt": receipt,
        "quantities": quantities,
        "stockPosted": not record_only,
        "movements": moved,
        "reprocessOrder": reprocess_order,
    }


def _create_reprocess_order(receipt, job_work_order, quantity, remarks, user):
    from apps.core.numbering import allocate_number
    from apps.jobwork.models import JWOReprocess

    on_date = timezone.localdate()
    return JWOReprocess.objects.create(
        client_id=receipt.client_id,
        job_work_order=job_work_order,
        no=allocate_number(receipt.client, "RP", on_date),
        inward_no=receipt.grn_number,
        date=on_date,
        qty=quantity,
        reason=remarks or f"Reprocess from QC of {receipt.grn_number}",
        status="Pending",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


#: Bill-level QC verdict -> the category the whole remaining pool goes to.
BILL_VERDICT_CATEGORY = {
    "Approved": "good",
    "Rejected": "reject",
    "Rework": "reprocess",
    "Pending Approval": "hold",
}


@transaction.atomic
def dispose_all(receipt, verdict, *, user=None, note=None):
    """Send everything still at QC Hold for ``receipt`` one way (bill-level QC).

    ``Pending Approval`` keeps the goods at QC Hold and marks the GRN On Hold.
    """
    category = BILL_VERDICT_CATEGORY.get(verdict)
    if category is None:
        raise ValidationFailed(
            "Unknown QC status.",
            field_errors={"status": ["Expected Approved, Pending Approval, Rejected or Rework."]},
        )
    total = pool_quantity(receipt)
    if total <= ZERO:
        if receipt_has_stock(receipt):
            return None  # nothing left at QC Hold -- already dispositioned
        total = round4(sum(
            (D(line.stock_qty or line.received_qty or 0) for line in receipt.lines.all()), ZERO
        ))
        if total <= ZERO:
            return None
    quantities = {key: ZERO for key in CATEGORIES}
    quantities[category] = total
    return dispose_receipt(
        receipt,
        quantities=quantities,
        user=user,
        remarks=note,
        require_job_work_order=False,
    )
