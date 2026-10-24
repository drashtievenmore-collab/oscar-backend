"""
Stock services (api.md §7, db.md §7).

This module owns the movement ledger. Everything that changes stock goes
through :func:`post_movement`; nothing else writes ``stock_movements``, and
nothing at all updates or deletes one -- corrections are new reversing
movements (api.md §7.2).

``calculate_item_stock`` is ``ERPContext.calculateItemStock`` moved server-side
(db.md §7.3). Every stock read -- item detail, line-picker availability,
low-stock tiles, auto-PO suggestions -- goes through it, so the UI and the
reorder engine can never disagree.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation, Codes, ValidationFailed
from apps.core.money import ZERO, D, round2, round4

from .models import (
    REVERSAL_OF,
    SALES_RETURN_TYPES,
    SELF_REVERSING_TYPES,
    StockBalance,
    StockMovement,
)

ZERO_QTY = Decimal("0.0000")


def _dec(field="quantity"):
    return Coalesce(
        Sum(field), Value(ZERO_QTY), output_field=DecimalField(max_digits=18, decimal_places=4)
    )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
@transaction.atomic
def post_movement(
    *,
    client_id,
    item,
    location,
    type,
    quantity,
    weighed_qty=None,
    unit_cost=None,
    reference_type=None,
    reference_id=None,
    reference_number=None,
    source_document_type=None,
    source_document_id=None,
    batch_number=None,
    movement_date=None,
    notes=None,
    user=None,
    original_movement=None,
    uom=None,
    lot_taka=None,
):
    """Append one movement and update the balance in the same transaction.

    ``quantity`` is signed: positive in, negative out. Callers pass the sign
    that matches the movement type rather than relying on this function to
    guess, because a ``SALE`` of -5 and an ``ADJUSTMENT`` of -5 are different
    facts that happen to share a sign.
    """
    quantity = round4(quantity)
    if quantity == ZERO:
        raise ValidationFailed(
            "A stock movement cannot be for zero quantity.",
            field_errors={"quantity": ["Must not be zero."]},
        )

    item_id = getattr(item, "id", item)
    location_id = getattr(location, "id", location)

    if location_id is None:
        raise ValidationFailed(
            "A stock movement needs a location.",
            field_errors={"locationId": ["Required."]},
        )

    from apps.masters.models import Item
    from apps.inventory.models import LotTaka

    # Resolve Item to check tracking_type
    item_obj = item if hasattr(item, "tracking_type") else Item.objects.filter(pk=item_id).first()
    item_tracking = getattr(item_obj, "tracking_type", "NONE")

    lot_taka_obj = None
    if lot_taka is not None:
        lot_taka_id = getattr(lot_taka, "id", lot_taka)
        # Locked: two documents drawing on one taka must serialise, or both
        # could pass the availability check below.
        lot_taka_obj = (
            LotTaka.objects.select_for_update()
            .filter(client_id=client_id, id=lot_taka_id, deleted_at__isnull=True)
            .first()
        )
        if lot_taka_obj is None:
            raise ValidationFailed(
                "That lot / taka does not exist.",
                field_errors={"lotTakaId": ["Not found."]},
            )
        if str(lot_taka_obj.item_id) != str(item_id):
            raise ValidationFailed(
                f"Taka {lot_taka_obj.taka_id or lot_taka_obj.taka_number} belongs to a "
                "different item.",
                field_errors={"lotTakaId": ["Item mismatch."]},
            )

    # Lot/taka-tracked items cannot move without naming the lot/taka -- that
    # is what keeps LotTaka.available_quantity equal to the ledger.
    if lot_taka_obj is None and item_tracking in ("LOT", "TAKA"):
        raise ValidationFailed(
            f"Item '{getattr(item_obj, 'name', item_id)}' requires lot/taka tracking.",
            field_errors={"lotTaka": ["Lot/Taka is required for this item."]},
        )

    if lot_taka_obj is not None:
        quantity_in_lot = round4(quantity)
        if quantity_in_lot < ZERO and str(lot_taka_obj.location_id) != str(location_id):
            raise ValidationFailed(
                f"Taka {lot_taka_obj.taka_id or lot_taka_obj.taka_number} is not at that "
                "location.",
                field_errors={"locationId": ["The taka is stored elsewhere."]},
            )
        new_avail = round4(D(lot_taka_obj.available_quantity) + quantity_in_lot)
        if new_avail < ZERO:
            raise BusinessRuleViolation(
                f"Not enough quantity on taka "
                f"{lot_taka_obj.taka_id or lot_taka_obj.taka_number} "
                f"(lot {lot_taka_obj.lot_number}). Requested {abs(quantity_in_lot)}, "
                f"available {round4(lot_taka_obj.available_quantity)}.",
                code=Codes.INSUFFICIENT_STOCK,
                payload={
                    "lotTakaId": str(lot_taka_obj.id),
                    "requested": str(abs(quantity_in_lot)),
                    "available": str(round4(lot_taka_obj.available_quantity)),
                },
            )
        if (
            quantity_in_lot > ZERO
            and D(lot_taka_obj.available_quantity) > ZERO
            and str(lot_taka_obj.location_id) != str(location_id)
        ):
            # One taka record cannot sit in two places at once; callers split
            # it first (see receive_into_lot / relocate_lot).
            raise ValidationFailed(
                f"Taka {lot_taka_obj.taka_id or lot_taka_obj.taka_number} still has stock "
                "at another location; split it before receiving it here.",
                field_errors={"lotTakaId": ["Split the taka first."]},
            )
        if batch_number is None:
            batch_number = lot_taka_obj.lot_number

    # api.md §7.2 -- orphan movements are only allowed for ADJUSTMENT, and only
    # with a reason. The database enforces it too; this is the friendly half.
    if reference_id is None and type != "ADJUSTMENT":
        raise ValidationFailed(
            "Every stock movement must reference a document.",
            field_errors={"referenceId": ["Required for this movement type."]},
        )
    if type == "ADJUSTMENT" and not notes:
        raise ValidationFailed(
            "A stock adjustment needs a reason.",
            field_errors={"reason": ["Required for a manual adjustment."]},
        )

    if uom is None:
        # ``quantity`` is in the item's stock unit; record which one that is.
        uom = getattr(item, "uom", None)
        if uom is None:
            uom = item_obj.uom if item_obj else None

    movement = StockMovement.objects.create(
        client_id=client_id,
        item_id=item_id,
        location_id=location_id,
        type=type,
        quantity=quantity,
        uom=uom,
        weighed_qty=round4(weighed_qty) if weighed_qty is not None else None,
        unit_cost=round4(unit_cost) if unit_cost is not None else None,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_number=reference_number,
        source_document_type=source_document_type or reference_type,
        source_document_id=source_document_id or reference_id,
        batch_number=batch_number,
        lot_taka=lot_taka_obj,
        movement_date=movement_date or timezone.localdate(),
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        original_movement=original_movement,
    )

    if lot_taka_obj is not None:
        # Never clamped: a negative result was rejected above.
        lot_taka_obj.available_quantity = round4(
            D(lot_taka_obj.available_quantity) + quantity
        )
        update_fields = ["available_quantity", "updated_at"]
        if quantity > ZERO and str(lot_taka_obj.location_id) != str(location_id):
            # Stock arriving somewhere carries the taka with it (transfer in,
            # QC routing, returns) -- the location follows the ledger.
            lot_taka_obj.location_id = location_id
            update_fields.append("location")
        if lot_taka_obj.available_quantity <= ZERO and type == "SALE":
            lot_taka_obj.status = LotTaka.STATUS_DISPATCHED
            update_fields.append("status")
        lot_taka_obj.save(update_fields=update_fields)

    if original_movement is not None:
        # The one write that must survive the append-only rule: back-filling
        # the link on the original row (db.md §7.1).
        StockMovement.objects.filter(pk=original_movement.pk).update(
            reversal_movement=movement
        )

    _apply_to_balance(movement)

    try:
        if getattr(item_obj, "item_kind", None) == "Fabric" or getattr(item_obj, "fabric_quality", None):
            evaluate_combined_grey_stock_alert(client_id)
    except Exception:
        pass

    return movement



def _apply_to_balance(movement):
    """Keep ``stock_balances`` in step with the ledger, in the same transaction.

    db.md §7.2 offers a matview or a trigger-maintained table; this is the
    table, so a finalized invoice and the stock tile can never disagree.
    """
    balance, _ = StockBalance.objects.select_for_update().get_or_create(
        client_id=movement.client_id,
        item_id=movement.item_id,
        location_id=movement.location_id,
        defaults={"on_hand": ZERO_QTY, "damaged": ZERO_QTY},
    )

    effective = movement.effective_quantity

    if movement.type == "FAULTY":
        # db.md §7.2 -- FAULTY quantity is excluded from on_hand and counted
        # separately as `damaged`.
        balance.damaged = (balance.damaged or ZERO) + abs(effective)
    else:
        balance.on_hand = (balance.on_hand or ZERO) + effective

    if effective > ZERO and movement.unit_cost is not None:
        balance.inward_qty = (balance.inward_qty or ZERO) + effective
        balance.inward_value = round2(
            (balance.inward_value or ZERO) + effective * movement.unit_cost
        )

    balance.save(update_fields=["on_hand", "damaged", "inward_qty", "inward_value", "updated_at"])
    return balance


@transaction.atomic
def reverse_movements(*, reference_type, reference_id, client_id, user=None, notes=None):
    """Post the matching ``*_REVERSAL`` for every movement of a document.

    api.md §6.9 step 2: reversing movements are linked back through
    ``original_movement`` / ``reversal_movement``. Already-reversed movements
    are skipped, so cancelling twice cannot double-reverse.
    """
    # Undo newest-first: a taka moved A -> B -> C must come back C -> B -> A,
    # because every outward leg checks where the taka currently is.
    originals = list(
        StockMovement.objects.select_for_update().filter(
            client_id=client_id,
            reference_type=reference_type,
            reference_id=reference_id,
            reversal_movement__isnull=True,
            # A reversal is never itself reversed by a second cancel.
            original_movement__isnull=True,
        ).exclude(type__in=list(REVERSAL_OF.keys())).order_by("-created_at")
    )

    reversals = []
    for original in originals:
        reversal_type = _reversal_type_for(original.type)
        if reversal_type is None:
            continue
        reversals.append(
            post_movement(
                client_id=client_id,
                item=original.item_id,
                location=original.location_id,
                type=reversal_type,
                quantity=-original.quantity,
                weighed_qty=(
                    -original.weighed_qty if original.weighed_qty is not None else None
                ),
                unit_cost=original.unit_cost,
                reference_type=original.reference_type,
                reference_id=original.reference_id,
                reference_number=original.reference_number,
                batch_number=original.batch_number,
                notes=notes or f"Reversal of {original.type}",
                user=user,
                original_movement=original,
                uom=original.uom,
                lot_taka=original.lot_taka_id,
                source_document_type=original.source_document_type,
                source_document_id=original.source_document_id,
            )
        )
    return reversals


def _reversal_type_for(movement_type):
    for reversal, original in REVERSAL_OF.items():
        if original == movement_type:
            return reversal
    if movement_type in SALES_RETURN_TYPES:
        return "RETURN_CANCELLATION"
    if movement_type == "PURCHASE_RETURN":
        return "PURCHASE"
    if movement_type in SELF_REVERSING_TYPES:
        return movement_type
    return None


# ---------------------------------------------------------------------------
# System stock locations (Phase 1.4)
# ---------------------------------------------------------------------------
#: role -> (code, name, location type). Created per client on first use.
SYSTEM_LOCATIONS = {
    "QC_HOLD": ("SYS-QC-HOLD", "QC Hold", "Zone"),
    "REPROCESS": ("SYS-REPROCESS", "Reprocess Stock", "Zone"),
    "SECOND_QUALITY": ("SYS-SECOND-QUALITY", "Second Quality Stock", "Zone"),
    "REJECT": ("SYS-REJECT", "Reject / Scrap", "Scrap"),
}

#: Stock at these locations is physically on hand but not available to sell
#: or issue unless the caller names the location explicitly.
BLOCKED_LOCATION_CODES = tuple(code for code, _, _ in SYSTEM_LOCATIONS.values())

#: The condition vocabulary for returns and dispositions.
CONDITIONS = ("Good", "Hold", "Reprocess", "Second Quality", "Reject")

_CONDITION_ALIASES = {
    "good": "Good", "usable": "Good", "ok": "Good", "pass": "Good", "passed": "Good",
    "hold": "Hold", "qc hold": "Hold", "on hold": "Hold",
    "reprocess": "Reprocess", "rework": "Reprocess",
    "second quality": "Second Quality", "second": "Second Quality",
    "seconds": "Second Quality", "second_quality": "Second Quality",
    "reject": "Reject", "rejected": "Reject", "scrap": "Reject", "wastage": "Reject",
}

#: condition -> (system location role or None for "the good location",
#:               LotTaka.qc_status)
CONDITION_ROUTING = {
    "Good": (None, "Passed"),
    "Hold": ("QC_HOLD", "On Hold"),
    "Reprocess": ("REPROCESS", "Reprocess"),
    "Second Quality": ("SECOND_QUALITY", "Passed"),
    "Reject": ("REJECT", "Rejected"),
}


def normalize_condition(value, *, field="condition"):
    """Map the UI's spellings onto :data:`CONDITIONS`; reject anything else."""
    key = str(value or "").strip().lower().replace("-", " ")
    condition = _CONDITION_ALIASES.get(key)
    if condition is None:
        raise ValidationFailed(
            f"Unknown condition '{value}'.",
            field_errors={field: [f"Expected one of: {', '.join(CONDITIONS)}."]},
        )
    return condition


def system_location(client_id, role):
    """The per-client system location for ``role``, created on first use.

    The QC Hold location used to be created by name only (with an empty code);
    such a row is adopted rather than duplicated.
    """
    from apps.masters.models import Location

    code, name, location_type = SYSTEM_LOCATIONS[role]
    location = Location.objects.filter(
        client_id=client_id, code=code, deleted_at__isnull=True
    ).first()
    if location is not None:
        return location
    legacy = (
        Location.objects.filter(client_id=client_id, name=name, deleted_at__isnull=True)
        .filter(Q(code="") | Q(code__isnull=True))
        .first()
    )
    if legacy is not None:
        legacy.code = code
        legacy.save(update_fields=["code", "updated_at"])
        return legacy
    return Location.objects.create(
        client_id=client_id,
        code=code,
        name=name,
        type=location_type,
        description="System location -- stock here is not available for sale.",
    )


def condition_location_id(client_id, condition, good_location_id):
    """Where stock in ``condition`` goes. Good returns to ``good_location_id``."""
    role, _ = CONDITION_ROUTING[condition]
    if role is None:
        if not good_location_id:
            raise ValidationFailed(
                "Choose the location good stock goes back to.",
                field_errors={"locationId": ["Required for Good condition."]},
            )
        return good_location_id
    return system_location(client_id, role).id


def blocked_location_ids(client_id):
    from apps.masters.models import Location

    return list(
        Location.objects.filter(
            client_id=client_id, code__in=BLOCKED_LOCATION_CODES
        ).values_list("id", flat=True)
    )


# ---------------------------------------------------------------------------
# Lot / taka movement helpers -- every lot quantity change goes through these
# ---------------------------------------------------------------------------
def _unique_taka_number(client_id, lot_number, base):
    from .models import LotTaka

    candidate = base
    counter = 1
    while LotTaka.objects.filter(
        client_id=client_id, lot_number=lot_number, taka_number=candidate,
        deleted_at__isnull=True,
    ).exists():
        counter += 1
        candidate = f"{base}{counter}"
    return candidate


def split_lot(lot, *, location_id, suffix, source_document_type=None,
              source_document_id=None, source_document_number=None, qc_status=None,
              remarks=None):
    """A new, empty child taka of ``lot`` at ``location_id``.

    The caller posts the movement that fills it, so the child's
    ``available_quantity`` starts at zero and only the ledger changes it.
    """
    from apps.core.numbering import allocate_number

    from .models import LotTaka

    taka_code = allocate_number(lot.client_id, "TAK")
    # Optional traceability fields (present only when the model defines them).
    field_names = {f.name for f in LotTaka._meta.get_fields()}
    extra = {}
    for name in ("fabric_quality", "design", "shade"):
        if name in field_names:
            extra[f"{name}_id"] = getattr(lot, f"{name}_id", None)
    if "parent_taka" in field_names:
        extra["parent_taka"] = lot
    return LotTaka.objects.create(
        **extra,
        client_id=lot.client_id,
        taka_id=taka_code,
        barcode=taka_code,
        lot_number=lot.lot_number,
        taka_number=_unique_taka_number(
            lot.client_id, lot.lot_number, f"{lot.taka_number}-{suffix}"
        ),
        item_id=lot.item_id,
        supplier_id=lot.supplier_id,
        source_document_type=source_document_type or lot.source_document_type,
        source_document_number=source_document_number or lot.source_document_number,
        source_document_id=source_document_id or lot.source_document_id,
        source_line_id=lot.source_line_id,
        location_id=location_id,
        received_date=lot.received_date,
        received_quantity=ZERO_QTY,
        available_quantity=ZERO_QTY,
        uom=lot.uom,
        qc_status=qc_status or lot.qc_status,
        status="Active",
        tag_status="Pending",
        width=lot.width,
        gsm=lot.gsm,
        rate=lot.rate,
        remarks=remarks,
    )


@transaction.atomic
def relocate_lot(*, client_id, lot, quantity, to_location_id, out_type, in_type,
                 reference_type, reference_id, reference_number=None,
                 source_document_type=None, source_document_id=None,
                 movement_date=None, notes=None, user=None, qc_status=None,
                 split_suffix="S"):
    """Move ``quantity`` of a taka to another location, splitting if partial.

    Posts an ``out_type`` movement at the taka's current location and an
    ``in_type`` movement at the destination; returns ``(taka_now_holding_the
    quantity, [movements])``. Moving to the same location only re-labels the
    QC status -- no movement is needed.
    """
    from .models import LotTaka

    lot = LotTaka.objects.select_for_update().filter(
        client_id=client_id, pk=getattr(lot, "pk", lot), deleted_at__isnull=True
    ).first()
    if lot is None:
        raise ValidationFailed("That lot / taka does not exist.", field_errors={"lotTakaId": ["Not found."]})
    quantity = round4(quantity)
    if quantity <= ZERO:
        raise ValidationFailed(
            "Quantity to move must be greater than zero.",
            field_errors={"qty": ["Must be greater than zero."]},
        )
    available = round4(lot.available_quantity)
    if quantity > available:
        raise BusinessRuleViolation(
            f"Taka {lot.taka_id or lot.taka_number} has only {available} available; "
            f"cannot move {quantity}.",
            code=Codes.INSUFFICIENT_STOCK,
            payload={"lotTakaId": str(lot.id), "available": str(available)},
        )

    if str(lot.location_id) == str(to_location_id):
        if qc_status and lot.qc_status != qc_status and quantity == available:
            lot.qc_status = qc_status
            lot.save(update_fields=["qc_status", "updated_at"])
        return lot, []

    unit_cost = D(lot.rate) if lot.rate else None
    common = dict(
        client_id=client_id,
        item=lot.item_id,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_number=reference_number,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        movement_date=movement_date,
        notes=notes,
        user=user,
        unit_cost=unit_cost,
    )
    out_movement = post_movement(
        location=lot.location_id, type=out_type, quantity=-quantity, lot_taka=lot, **common
    )
    if quantity == available:
        target = lot
    else:
        target = split_lot(
            lot, location_id=to_location_id, suffix=split_suffix,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
            qc_status=qc_status,
            remarks=f"Split from taka {lot.taka_id or lot.taka_number} by {reference_number or reference_type}",
        )
    in_movement = post_movement(
        location=to_location_id, type=in_type, quantity=quantity, lot_taka=target, **common
    )
    target.refresh_from_db()
    if target is not lot:
        target.received_quantity = quantity
        target.stock_movement = in_movement
    if qc_status:
        target.qc_status = qc_status
        if qc_status in ("Passed", "Approved"):
            target.status = "Active"
        elif qc_status in ("Rejected", "Reject"):
            target.status = "Reject"
        elif qc_status == "On Hold":
            target.status = "QC Hold"
    target.save()
    return target, [out_movement, in_movement]


@transaction.atomic
def receive_into_lot(*, client_id, lot, quantity, location_id, type, reference_type,
                     reference_id, reference_number=None, source_document_type=None,
                     source_document_id=None, movement_date=None, notes=None, user=None,
                     qc_status=None, unit_cost=None, split_suffix="R"):
    """Bring ``quantity`` of a taka back in at ``location_id`` (returns).

    If the taka still holds stock somewhere else, the returned part becomes a
    child taka at the destination so one record never spans two locations.
    """
    from .models import LotTaka

    lot = LotTaka.objects.select_for_update().filter(
        client_id=client_id, pk=getattr(lot, "pk", lot), deleted_at__isnull=True
    ).first()
    if lot is None:
        raise ValidationFailed("That lot / taka does not exist.", field_errors={"lotTakaId": ["Not found."]})
    target = lot
    if D(lot.available_quantity) > ZERO and str(lot.location_id) != str(location_id):
        target = split_lot(
            lot, location_id=location_id, suffix=split_suffix,
            source_document_type=source_document_type,
            source_document_id=source_document_id, qc_status=qc_status,
            remarks=f"Returned via {reference_number or reference_type}",
        )
    movement = post_movement(
        client_id=client_id, item=lot.item_id, location=location_id, type=type,
        quantity=round4(quantity), lot_taka=target, reference_type=reference_type,
        reference_id=reference_id, reference_number=reference_number,
        source_document_type=source_document_type, source_document_id=source_document_id,
        movement_date=movement_date, notes=notes, user=user,
        unit_cost=unit_cost if unit_cost is not None else (D(lot.rate) if lot.rate else None),
    )
    target.refresh_from_db()
    if target is not lot:
        target.received_quantity = round4(quantity)
        target.stock_movement = movement
    if qc_status:
        target.qc_status = qc_status
        if qc_status in ("Passed", "Approved"):
            target.status = "Active"
        elif qc_status in ("Rejected", "Reject"):
            target.status = "Reject"
        elif qc_status == "On Hold":
            target.status = "QC Hold"
    target.save()
    return target, movement


@transaction.atomic
def relocate_item_qty(*, client_id, item, quantity, from_location_id, to_location_id,
                      out_type, in_type, reference_type, reference_id,
                      reference_number=None, source_document_type=None,
                      source_document_id=None, movement_date=None, notes=None,
                      user=None, unit_cost=None):
    """Item-level move for stock that is not held on lots/takas."""
    quantity = round4(quantity)
    if str(from_location_id) == str(to_location_id) or quantity == ZERO:
        return []
    balance = balances_for(client_id, [getattr(item, "id", item)], from_location_id).get(
        getattr(item, "id", item), {}
    )
    on_hand = D(balance.get("on_hand", ZERO) or ZERO)
    if quantity > on_hand:
        raise BusinessRuleViolation(
            f"Only {round4(on_hand)} on hand at the source location; cannot move {quantity}.",
            code=Codes.INSUFFICIENT_STOCK,
            payload={"available": str(round4(on_hand)), "requested": str(quantity)},
        )
    common = dict(
        client_id=client_id, item=item, reference_type=reference_type,
        reference_id=reference_id, reference_number=reference_number,
        source_document_type=source_document_type, source_document_id=source_document_id,
        movement_date=movement_date, notes=notes, user=user, unit_cost=unit_cost,
    )
    return [
        post_movement(location=from_location_id, type=out_type, quantity=-quantity, **common),
        post_movement(location=to_location_id, type=in_type, quantity=quantity, **common),
    ]


# ---------------------------------------------------------------------------
# Derived stock (db.md §7.3) -- this *is* calculateItemStock
# ---------------------------------------------------------------------------
def reserved_quantity(client_id, item_ids=None):
    """Open sales-order lines reserve stock (api.md §5.4, db.md §5.2).

    ``reserved`` = sum of ``qty - dispatched_qty`` over lines of orders whose
    stage is not Delivered, Invoiced or Cancelled. Reservation is deliberately
    not a stored column -- the same rule ``calculateItemStock`` uses today.
    """
    from apps.sales.models import NON_RESERVING_STAGES, SalesOrderLine

    queryset = SalesOrderLine.objects.filter(
        client_id=client_id,
        sales_order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    ).exclude(sales_order__stage__in=NON_RESERVING_STAGES)

    if item_ids is not None:
        queryset = queryset.filter(item_id__in=item_ids)

    rows = (
        queryset.values("item_id")
        .annotate(
            reserved=Coalesce(
                Sum((F("qty") - F("dispatched_qty")) * F("uom_factor")),
                Value(ZERO_QTY),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )
        )
    )
    result = {item_id: ZERO_QTY for item_id in item_ids} if item_ids is not None else {}
    for row in rows:
        result[row["item_id"]] = max(row["reserved"] or ZERO, ZERO)
    return result


def balances_for(client_id, item_ids=None, location_id=None):
    """``on_hand`` and ``damaged`` per item, optionally at one location."""
    queryset = StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if item_ids is not None:
        queryset = queryset.filter(item_id__in=item_ids)
    if location_id:
        queryset = queryset.filter(location_id=location_id)

    # ``blocked`` is stock physically on hand at a system location (QC Hold,
    # Reprocess, Second Quality, Reject): counted in on_hand, never available
    # unless the caller asks about that location explicitly.
    blocked_filter = Q(location__code__in=BLOCKED_LOCATION_CODES)
    # ``blocked`` is annotated first: once ``on_hand`` is an annotation name,
    # Sum("on_hand") would refer to the aggregate instead of the column.
    rows = queryset.values("item_id").annotate(
        blocked=Coalesce(
            Sum("on_hand", filter=blocked_filter),
            Value(ZERO_QTY),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        ),
    ).annotate(
        on_hand=_dec("on_hand"),
        damaged=_dec("damaged"),
    )
    result = {row["item_id"]: row for row in rows}
    if location_id:
        for row in result.values():
            row["blocked"] = ZERO_QTY
    return result


def stock_status(available, reorder_level):
    """api.md §4.2 -- the derived item status.

    ``available <= reorderLevel/2`` -> Critical; ``<= reorderLevel`` -> Low
    Stock; else Optimal. Never stored (db.md §12).
    """
    available = D(available)
    reorder_level = D(reorder_level)
    if reorder_level > ZERO and available <= reorder_level / 2:
        return "Critical"
    if reorder_level > ZERO and available <= reorder_level:
        return "Low Stock"
    if reorder_level == ZERO and available <= ZERO:
        return "Critical"
    return "Optimal"


def calculate_item_stock(client_id, item, location_id=None):
    """``{ onHand, reserved, damaged, available, status }`` for one item."""
    item_id = getattr(item, "id", item)
    reorder_level = getattr(item, "reorder_level", None)
    if reorder_level is None:
        from apps.masters.models import Item

        reorder_level = (
            Item.objects.filter(pk=item_id).values_list("reorder_level", flat=True).first()
            or ZERO
        )

    balance = balances_for(client_id, [item_id], location_id).get(item_id, {})
    on_hand = balance.get("on_hand", ZERO) or ZERO
    damaged = balance.get("damaged", ZERO) or ZERO
    blocked = balance.get("blocked", ZERO) or ZERO
    reserved = reserved_quantity(client_id, [item_id]).get(item_id, ZERO) or ZERO
    available = on_hand - blocked - reserved

    return {
        "onHand": on_hand,
        "reserved": reserved,
        "damaged": damaged,
        "blocked": blocked,
        "available": available,
        "status": stock_status(available, reorder_level),
    }


def stock_by_location(client_id, item_id):
    """``GET /inventory/items/{id}/stock/`` -- the per-location breakdown."""
    rows = (
        StockBalance.objects.filter(
            client_id=client_id, item_id=item_id, deleted_at__isnull=True
        )
        .select_related("location")
        .order_by("location__name")
    )
    return [
        {
            "locationId": str(row.location_id),
            "locationName": row.location.name,
            "onHand": row.on_hand,
            "damaged": row.damaged,
        }
        for row in rows
    ]


def annotate_items_with_stock(client_id, items):
    """Attach derived stock to a page of items without an N+1.

    db.md §15 flags the item list as one of the screens that will hurt first;
    this resolves the whole page in two aggregate queries.
    """
    items = list(items)
    if not items:
        return items
    item_ids = [item.id for item in items]
    balances = balances_for(client_id, item_ids)
    reserved_map = reserved_quantity(client_id, item_ids)

    for item in items:
        balance = balances.get(item.id, {})
        on_hand = balance.get("on_hand", ZERO) or ZERO
        damaged = balance.get("damaged", ZERO) or ZERO
        blocked = balance.get("blocked", ZERO) or ZERO
        reserved = reserved_map.get(item.id, ZERO) or ZERO
        item.on_hand_qty = on_hand
        item.reserved_qty = reserved
        item.damaged_qty = damaged
        item.blocked_qty = blocked
        item.available_qty = on_hand - blocked - reserved
        item.stock_status = stock_status(item.available_qty, item.reorder_level)
    return items


# ---------------------------------------------------------------------------
# Guards used by the document services
# ---------------------------------------------------------------------------
def assert_sufficient_stock(client_id, item, quantity, location_id=None, label=None):
    """422 INSUFFICIENT_STOCK when a dispatch would go negative.

    api-integration.md §5.5 shows 422 messages verbatim to the user, so the
    message names the item and the number the user can act on.
    """
    from apps.masters.models import Item

    item_obj = item if hasattr(item, "sku") else Item.objects.filter(pk=item).first()
    if item_obj is None:
        raise ValidationFailed("Unknown item.", field_errors={"itemId": ["Not found."]})
    if not item_obj.holds_stock:
        return  # Service items hold no stock (api.md §4.2).

    stock = calculate_item_stock(client_id, item_obj, location_id)
    if D(quantity) > stock["available"]:
        raise BusinessRuleViolation(
            f"Not enough stock for {label or item_obj.name}. "
            f"Requested {round4(quantity)}, available {round4(stock['available'])}.",
            code=Codes.INSUFFICIENT_STOCK,
            payload={
                "itemId": str(item_obj.id),
                "sku": item_obj.sku,
                "requested": str(round4(quantity)),
                "available": str(round4(stock["available"])),
            },
        )


def qc_block_for(client_id, item_id):
    """``GET /inventory/items/{id}/qc-block/`` (api.md §6.4).

    Goods whose receipt is not Approved are blocked from sale. Implemented as a
    query over receipts rather than a flag on ``items``, per db.md §6.1.
    """
    from apps.purchase.models import GoodsReceipt

    receipt = (
        GoodsReceipt.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            # Rejected goods are moved to the Reject location by the QC
            # disposition, so they no longer hold the item's other stock.
            qc_status__in=["Pending", "On Hold"],
            lines__item_id=item_id,
        )
        .select_related("purchase_bill")
        .order_by("-receipt_date")
        .first()
    )
    if receipt is None:
        return {"blocked": False, "billId": None, "reason": None}
    # Goods awaiting QC sit at the QC Hold location and are excluded from
    # availability there, so a pending receipt only blocks the sale when the
    # item has no released (non-QC-Hold) stock to sell from.
    released = calculate_item_stock(client_id, item_id)["available"]
    return {
        "blocked": D(released) <= ZERO,
        "billId": str(receipt.purchase_bill_id) if receipt.purchase_bill_id else None,
        "goodsReceiptId": str(receipt.id),
        "qcStatus": receipt.qc_status,
        "reason": receipt.qc_note or f"Goods receipt {receipt.grn_number} is {receipt.qc_status}.",
    }


def assert_not_qc_blocked(client_id, item_id, label=None):
    block = qc_block_for(client_id, item_id)
    if block["blocked"]:
        raise BusinessRuleViolation(
            f"{label or 'This item'} is held by quality control and cannot be sold yet.",
            code=Codes.QC_BLOCKED,
            detail=block["reason"],
            payload=block,
        )


# ---------------------------------------------------------------------------
# Serial handling (db.md §4.3)
# ---------------------------------------------------------------------------
def resolve_serials(client_id, item_id, serial_numbers, *, expected_status="available"):
    """Look up serials and check they are in the state the caller expects."""
    from apps.masters.models import ItemSerial

    serial_numbers = [str(value).strip() for value in (serial_numbers or []) if str(value).strip()]
    if not serial_numbers:
        return []

    found = list(
        ItemSerial.objects.select_for_update().filter(
            client_id=client_id, item_id=item_id, serial_no__in=serial_numbers,
            deleted_at__isnull=True,
        )
    )
    found_numbers = {serial.serial_no for serial in found}
    missing = [number for number in serial_numbers if number not in found_numbers]
    if missing:
        raise BusinessRuleViolation(
            f"Unknown serial number{'s' if len(missing) > 1 else ''}: {', '.join(missing)}.",
            code=Codes.SERIAL_MISMATCH,
            payload={"missing": missing},
        )

    if expected_status:
        wrong = [s.serial_no for s in found if s.status != expected_status]
        if wrong:
            code = (
                Codes.SERIAL_ALREADY_SOLD
                if expected_status == "available"
                else Codes.SERIAL_MISMATCH
            )
            raise BusinessRuleViolation(
                f"Serial{'s' if len(wrong) > 1 else ''} not {expected_status}: {', '.join(wrong)}.",
                code=code,
                payload={"serials": wrong},
            )
    return found


def assert_serial_count(item, quantity, serials, label=None):
    """api.md §4.2 -- reject a line whose serial selection does not match its qty."""
    if getattr(item, "tracking_mode", "Quantity") != "Serial":
        return
    count = len(serials or [])
    if D(count) != D(quantity):
        raise BusinessRuleViolation(
            f"{label or item.name} is serial-tracked: select exactly "
            f"{round4(quantity)} serial number(s), not {count}.",
            code=Codes.SERIAL_MISMATCH,
            payload={"expected": str(round4(quantity)), "provided": count},
        )


@transaction.atomic
def set_serial_status(serials, status, *, movement=None, warranty_card=None, location=None):
    from apps.masters.models import ItemSerial

    ids = [getattr(serial, "id", serial) for serial in serials]
    if not ids:
        return 0
    updates = {"status": status}
    if movement is not None and status == "sold":
        updates["sold_movement"] = movement
    if movement is not None and status == "available":
        updates["received_movement"] = movement
    if warranty_card is not None:
        updates["warranty_card"] = warranty_card
    if location is not None:
        updates["location_id"] = getattr(location, "id", location)
    return ItemSerial.objects.filter(pk__in=ids).update(**updates)


def link_line_serials(client_id, line_table, line_id, serials):
    """Record the per-line serial selection (db.md §3.2)."""
    from apps.masters.models import DocumentLineSerial

    rows = [
        DocumentLineSerial(
            client_id=client_id,
            line_table=line_table,
            line_id=line_id,
            serial_id=getattr(serial, "id", serial),
        )
        for serial in serials or []
    ]
    if rows:
        DocumentLineSerial.objects.bulk_create(rows, ignore_conflicts=True)
    return rows


def serials_for_lines(client_id, line_table, line_ids):
    """The reverse lookup, batched, so a document detail is not an N+1."""
    from apps.masters.models import DocumentLineSerial

    rows = (
        DocumentLineSerial.objects.filter(
            client_id=client_id, line_table=line_table, line_id__in=list(line_ids)
        )
        .select_related("serial")
        .values_list("line_id", "serial__serial_no")
    )
    grouped = {}
    for line_id, serial_no in rows:
        grouped.setdefault(line_id, []).append(serial_no)
    return grouped


def previously_returned_serials(client_id, sales_invoice_id):
    """api.md §5.9 -- a serial cannot be returned twice.

    The frontend filters ``previouslyReturnedSerials`` client-side; this is the
    server-side enforcement the spec asks for.
    """
    from apps.masters.models import DocumentLineSerial
    from apps.sales.models import SalesReturnLine

    line_ids = SalesReturnLine.objects.filter(
        client_id=client_id,
        sales_return__sales_invoice_id=sales_invoice_id,
        deleted_at__isnull=True,
    ).exclude(sales_return__status="Cancelled").values_list("id", flat=True)

    return set(
        DocumentLineSerial.objects.filter(
            client_id=client_id, line_table="sales_return_lines", line_id__in=list(line_ids)
        ).values_list("serial__serial_no", flat=True)
    )


# ---------------------------------------------------------------------------
# Phase 2.3 — WIP Locations, Ageing, Slow-Moving Stock, Traceability
# ---------------------------------------------------------------------------
def wip_location_for_party(client_id, party):
    """Virtual stock location for an external processor or agency (Phase 2.3)."""
    from apps.masters.models import Location

    party_id = getattr(party, "id", party)
    party_name = getattr(party, "name", str(party_id))
    party_code = getattr(party, "code", None)
    code_suffix = party_code or str(party_id)[:8].upper()
    code = f"SYS-WIP-{code_suffix}"

    loc = Location.objects.filter(client_id=client_id, code=code).first()
    if loc is None:
        loc = Location.objects.create(
            client_id=client_id,
            code=code,
            name=f"WIP - {party_name}",
            type="Zone",
            is_active=True,
        )
    return loc


def get_stock_ageing(client_id, location_id=None, item_id=None):
    """Real ageing computed from the inward date of each lot/taka (Phase 2.3).

    Buckets: 0-30, 31-60, 61-90, 91-180, 180+.
    """
    from .models import LotTaka

    today = timezone.localdate()
    qs = LotTaka.objects.filter(
        client_id=client_id,
        available_quantity__gt=0,
        deleted_at__isnull=True,
    ).select_related("item", "location")

    if location_id:
        qs = qs.filter(location_id=location_id)
    if item_id:
        qs = qs.filter(item_id=item_id)

    buckets_def = [
        {"key": "0-30", "min": 0, "max": 30, "label": "0 - 30 Days"},
        {"key": "31-60", "min": 31, "max": 60, "label": "31 - 60 Days"},
        {"key": "61-90", "min": 61, "max": 90, "label": "61 - 90 Days"},
        {"key": "91-180", "min": 91, "max": 180, "label": "91 - 180 Days"},
        {"key": "180+", "min": 181, "max": None, "label": "180+ Days"},
    ]

    bucket_data = {
        b["key"]: {"key": b["key"], "label": b["label"], "qty": ZERO, "value": ZERO, "takaCount": 0}
        for b in buckets_def
    }

    quality_map = {}
    item_map = {}

    total_qty = ZERO
    total_val = ZERO

    for lt in qs:
        rec_d = lt.received_date or today
        age = (today - rec_d).days
        if age < 0:
            age = 0

        target_bucket = None
        for b in buckets_def:
            if b["max"] is None:
                if age >= b["min"]:
                    target_bucket = b["key"]
                    break
            elif b["min"] <= age <= b["max"]:
                target_bucket = b["key"]
                break
        if not target_bucket:
            target_bucket = "180+"

        qty = round4(lt.available_quantity)
        rate = D(lt.rate or (getattr(lt.item, "cost_price", None) or 0))
        val = round2(qty * rate)

        bucket_data[target_bucket]["qty"] += qty
        bucket_data[target_bucket]["value"] += val
        bucket_data[target_bucket]["takaCount"] += 1

        total_qty += qty
        total_val += val

        qual = (getattr(lt.item, "fabric_quality", None) or getattr(lt.item, "name", None) or "Other").strip()
        if qual not in quality_map:
            quality_map[qual] = {"quality": qual, "qty": ZERO, "value": ZERO, "takas": 0}
        quality_map[qual]["qty"] += qty
        quality_map[qual]["value"] += val
        quality_map[qual]["takas"] += 1

        it_name = getattr(lt.item, "name", "Unknown")
        if it_name not in item_map:
            item_map[it_name] = {"itemName": it_name, "sku": getattr(lt.item, "sku", ""), "qty": ZERO, "value": ZERO}
        item_map[it_name]["qty"] += qty
        item_map[it_name]["value"] += val

    return {
        "summary": {
            "totalQty": float(total_qty),
            "totalValue": float(total_val),
            "totalTakas": sum(b["takaCount"] for b in bucket_data.values()),
        },
        "buckets": [
            {
                "key": b["key"],
                "label": b["label"],
                "qty": float(bucket_data[b["key"]]["qty"]),
                "value": float(bucket_data[b["key"]]["value"]),
                "takaCount": bucket_data[b["key"]]["takaCount"],
                "pct": round((float(bucket_data[b["key"]]["qty"]) / (float(total_qty) or 1.0)) * 100, 1),
            }
            for b in buckets_def
        ],
        "byQuality": [
            {"quality": q["quality"], "qty": float(q["qty"]), "value": float(q["value"]), "takas": q["takas"]}
            for q in sorted(quality_map.values(), key=lambda x: x["qty"], reverse=True)[:10]
        ],
        "byItem": [
            {"itemName": it["itemName"], "sku": it["sku"], "qty": float(it["qty"]), "value": float(it["value"])}
            for it in sorted(item_map.values(), key=lambda x: x["qty"], reverse=True)[:10]
        ],
    }


def get_slow_moving_stock(client_id, threshold_days=60):
    """Items/takas with holding age >= threshold_days and zero movement (Phase 2.3)."""
    from .models import LotTaka

    today = timezone.localdate()
    cutoff_date = today - timezone.timedelta(days=threshold_days)

    qs = LotTaka.objects.filter(
        client_id=client_id,
        available_quantity__gt=0,
        received_date__lte=cutoff_date,
        deleted_at__isnull=True,
    ).select_related("item", "location", "supplier").order_by("received_date")

    rows = []
    for lt in qs:
        age = (today - lt.received_date).days if lt.received_date else 0
        rate = D(lt.rate or (getattr(lt.item, "cost_price", None) or 0))
        qty = round4(lt.available_quantity)
        rows.append({
            "id": str(lt.id),
            "takaId": lt.taka_id or lt.barcode,
            "lotNo": lt.lot_number,
            "takaRoll": lt.taka_number,
            "itemCode": getattr(lt.item, "sku", "-"),
            "itemName": getattr(lt.item, "name", "-"),
            "quality": getattr(lt.item, "fabric_quality", None) or getattr(lt.item, "name", None),
            "shade": getattr(lt.item, "fabric_color", None),
            "availableQty": float(qty),
            "uom": lt.uom or getattr(lt.item, "uom", "Mtr"),
            "location": lt.location.name if lt.location else None,
            "supplier": lt.supplier.name if lt.supplier else None,
            "receivedDate": lt.received_date.strftime("%d-%m-%Y") if lt.received_date else "",
            "ageDays": age,
            "rate": float(rate),
            "value": float(round2(qty * rate)),
            "status": lt.status,
            "qcStatus": lt.qc_status,
        })
    return {"thresholdDays": threshold_days, "count": len(rows), "results": rows}


def trace_lot_lineage(client_id, lot_taka_id_or_number):
    """Complete traceability tracing finished lot back to source grey lots (Phase 2.3)."""
    from .models import LotTaka, StockMovement
    from apps.jobwork.models import JWOOutwardTaka, JWOInwardTaka

    target_str = str(lot_taka_id_or_number).strip()
    lt = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
        Q(id=target_str) if len(target_str) == 36 else (Q(taka_id__iexact=target_str) | Q(lot_number__iexact=target_str) | Q(barcode__iexact=target_str))
    ).select_related("item", "supplier", "location", "parent_taka").prefetch_related("parent_lots", "child_takas").first()

    if lt is None:
        return None

    def serialize_taka(t):
        if not t:
            return None
        it = t.item
        return {
            "id": str(t.id),
            "takaId": t.taka_id or t.barcode,
            "lotNo": t.lot_number,
            "takaRoll": t.taka_number,
            "itemCode": it.sku if it else "-",
            "itemName": it.name if it else None,
            "quality": getattr(it, "fabric_quality", None) or (it.name if it else ""),
            "design": getattr(it, "fabric_design", None),
            "shade": getattr(it, "fabric_color", None),
            "status": t.status,
            "qcStatus": t.qc_status,
            "receivedQty": float(t.received_quantity),
            "availableQty": float(t.available_quantity),
            "uom": t.uom,
            "location": t.location.name if t.location else None,
            "receivedDate": t.received_date.strftime("%d-%m-%Y") if t.received_date else "",
            "sourceDocType": t.source_document_type,
            "sourceDocNo": t.source_document_number,
            "width": float(t.width) if t.width else None,
            "weight": float(t.roll_weight) if t.roll_weight else None,
            "gsm": float(t.gsm) if t.gsm else None,
        }

    # Trace ancestors through both links, all the way back: a split / processed
    # taka points at its parent_taka, a merged batch at its parent_lots, and a
    # finished lot can sit on top of either (PDF §06 parent-child traceability).
    ancestors = []
    visited = {str(lt.id)}
    queue = [lt]
    while queue:
        node = queue.pop(0)
        parents = list(node.parent_lots.filter(deleted_at__isnull=True))
        if node.parent_taka_id:
            parents.insert(0, node.parent_taka)
        for parent in parents:
            if parent is None or str(parent.id) in visited:
                continue
            visited.add(str(parent.id))
            ancestors.append(serialize_taka(parent))
            queue.append(parent)

    # Trace descendants
    # A processed taka can point back through both links; list it once.
    descendants = []
    seen_children = set()
    for c in [*lt.child_takas.filter(deleted_at__isnull=True), *lt.derived_lots.filter(deleted_at__isnull=True)]:
        if str(c.id) not in seen_children:
            seen_children.add(str(c.id))
            descendants.append(serialize_taka(c))

    # Stock movements
    movs = StockMovement.objects.filter(
        client_id=client_id, lot_taka_id=lt.id
    ).select_related("location").order_by("movement_date", "created_at")
    movements_list = [
        {
            "id": str(m.id),
            "date": m.movement_date.strftime("%d-%m-%Y"),
            "type": m.type,
            "qty": float(m.quantity),
            "location": m.location.name if m.location else "-",
            "refType": m.reference_type,
            "refNo": m.reference_number,
            "notes": m.notes,
        }
        for m in movs
    ]

    # Process / Job Work links
    outwards = JWOOutwardTaka.objects.filter(lot_taka=lt).select_related("outward__job_work_order")
    inwards = JWOInwardTaka.objects.filter(Q(source_taka=lt) | Q(received_taka=lt)).select_related("inward__job_work_order")

    job_work_history = []
    for ow in outwards:
        jwo = ow.outward.job_work_order
        job_work_history.append({
            "type": "Outward",
            "jwoNo": jwo.jwo_no,
            "vendor": jwo.vendor_name,
            "process": jwo.process,
            "challanNo": ow.outward.challan_no or ow.outward.no,
            "date": ow.outward.date.strftime("%d-%m-%Y") if ow.outward.date else "",
            "meters": float(ow.meters),
        })
    for iw in inwards:
        jwo = iw.inward.job_work_order
        job_work_history.append({
            "type": "Inward",
            "jwoNo": jwo.jwo_no,
            "vendor": jwo.vendor_name,
            "process": jwo.process,
            "challanNo": iw.inward.challan_no or iw.inward.no,
            "date": iw.inward.date.strftime("%d-%m-%Y") if iw.inward.date else "",
            "meters": float(iw.meters),
        })

    # QC Inspections
    from .models import QCInspectionLine
    qc_lines = QCInspectionLine.objects.filter(lot_taka=lt).select_related("inspection").prefetch_related("defects__defect")
    qc_history = []
    for ql in qc_lines:
        insp = ql.inspection
        defects = [
            {"defect": d.defect.name, "category": d.defect.category, "count": d.defect_count, "points": d.points}
            for d in ql.defects.all()
        ]
        qc_history.append({
            "inspectionNo": insp.inspection_number,
            "date": insp.inspection_date.strftime("%d-%m-%Y") if insp.inspection_date else "",
            "grade": ql.grade,
            "pointsPer100M": float(ql.points_per_100_sq_meter),
            "disposition": ql.disposition,
            "defects": defects,
            "remarks": ql.remarks,
        })

    return {
        "target": serialize_taka(lt),
        "ancestors": ancestors,
        "descendants": descendants,
        "movements": movements_list,
        "jobWorkHistory": job_work_history,
        "qcHistory": qc_history,
    }


# ---------------------------------------------------------------------------
# Combined Grey Stock Calculation & Minimum-Level Alert Engine
# ---------------------------------------------------------------------------
def calculate_combined_grey_stock(client_id, location_id=None):
    """
    Authoritative calculation of combined grey fabric stock across all fabric qualities.

    Buckets:
    - Physical Grey Stock: Fabric items/takas physically at company godowns (non-WIP,
      non-blocked) that are NOT finished, not rejected, not on QC hold.
    - Reserved Grey Stock: Open sales order line commitments reserving grey stock
      (and takas explicitly in Reserved status).
    - Available Grey Stock: max(0, Physical Grey Stock - Reserved Grey Stock).
    - External Job Work WIP: Material dispatched to outside processors (SYS-WIP-*),
      broken down by processor/vendor.
    - Excluded from Available Grey: Finished fabric, QC Hold (SYS-QC-HOLD),
      Reprocess (SYS-REPROCESS), Second Quality (SYS-SECOND-QUALITY),
      Reject/Scrap (SYS-REJECT / FAULTY), Samples issued, and External WIP.

    Unit Safety:
    - Aggregates exclusively in linear length (Meters / Mtr).
    - Incompatible units are excluded from the meter sum and reported in incompatible_items.
    """
    from apps.masters import uom as uom_master
    from apps.masters.models import Item, Location, Party
    from apps.inventory.models import LotTaka, StockBalance

    book = uom_master.UnitBook(client_id)

    # 1. Identify grey fabric items
    items_qs = Item.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(item_kind="Service").filter(
        Q(item_kind="Fabric") | Q(category__name__icontains="Fabric") | Q(fabric_quality__isnull=False)
    ).select_related("category")

    grey_items = []
    incompatible_items = []
    for item in items_qs:
        f_stage = (getattr(item, "fabric_stage", "") or "").lower()
        f_qual = (item.fabric_quality or "").lower()
        cat_name = (item.category.name if item.category else "").lower()
        name_lower = (item.name or "").lower()
        if f_stage == "finished" or "finished" in f_qual or "finished" in cat_name or "finished" in name_lower:
            continue

        resolved_uom = book.resolve(item.uom)
        is_length = False
        if resolved_uom and getattr(resolved_uom, "uom_type", None) == "Length":
            is_length = True
        elif (item.uom or "").lower() in ("mtr", "meter", "m", "meters"):
            is_length = True

        if is_length:
            grey_items.append(item)
        else:
            incompatible_items.append({
                "id": str(item.id),
                "sku": item.sku,
                "name": item.name,
                "uom": item.uom,
            })

    grey_item_map = {item.id: item for item in grey_items}
    grey_item_ids = list(grey_item_map.keys())

    if not grey_item_ids:
        return {
            "physical_grey_stock": ZERO_QTY,
            "reserved_grey_stock": ZERO_QTY,
            "available_grey_stock": ZERO_QTY,
            "external_wip": ZERO_QTY,
            "wip_by_party": [],
            "uom": "Mtr",
            "qualities_count": 0,
            "takas_count": 0,
            "incompatible_items": incompatible_items,
        }

    # 2. Location mapping
    role_by_code = {code: role for role, (code, _, _) in SYSTEM_LOCATIONS.items()}
    all_locations = list(Location.objects.filter(client_id=client_id, deleted_at__isnull=True))
    location_role = {}
    wip_locations = {}
    for loc in all_locations:
        if loc.code in role_by_code:
            location_role[loc.id] = role_by_code[loc.code]
        elif (loc.code and loc.code.startswith("SYS-WIP-")) or (loc.name and loc.name.startswith("WIP - ")):
            location_role[loc.id] = "EXTERNAL_WIP"
            wip_locations[loc.id] = loc
        else:
            location_role[loc.id] = "GOOD"

    # 3. Balances
    balances_qs = StockBalance.objects.filter(
        client_id=client_id, deleted_at__isnull=True, item_id__in=grey_item_ids
    )
    if location_id:
        balances_qs = balances_qs.filter(location_id=location_id)
    balance_rows = list(balances_qs.select_related("item", "location"))

    # 4. WIP by party & External WIP
    external_wip_qty = ZERO
    wip_by_party_dict = {}
    parties = {p.code: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True) if p.code}
    parties_by_name = {p.name: p for p in Party.objects.filter(client_id=client_id, deleted_at__isnull=True)}

    for b in balance_rows:
        role = location_role.get(b.location_id, "GOOD")
        if role == "EXTERNAL_WIP":
            on_hand = round4(b.on_hand or ZERO)
            external_wip_qty += on_hand
            loc = wip_locations.get(b.location_id)
            loc_code = loc.code if loc else ""
            loc_name = loc.name if loc else ""
            suffix = loc_code.replace("SYS-WIP-", "") if loc_code.startswith("SYS-WIP-") else ""
            party = parties.get(suffix)
            if not party and loc_name.startswith("WIP - "):
                party = parties_by_name.get(loc_name[6:].strip())
            party_id = str(party.id) if party else None
            party_name = party.name if party else (loc_name[6:].strip() if loc_name.startswith("WIP - ") else (loc_name or "External Processor"))
            entry = wip_by_party_dict.setdefault(b.location_id, {
                "partyId": party_id,
                "partyName": party_name,
                "locationCode": loc_code,
                "locationName": loc_name,
                "qty": ZERO,
            })
            entry["qty"] += on_hand

    wip_by_party = [
        {
            "partyId": v["partyId"],
            "partyName": v["partyName"],
            "party": v["partyName"],
            "locationCode": v["locationCode"],
            "locationName": v["locationName"],
            "qty": float(v["qty"]),
            "quantity": v["qty"],
        }
        for v in wip_by_party_dict.values()
    ]

    # 5. Taka-level analysis of GOOD stock for real Grey partitioning
    good_takas = LotTaka.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        available_quantity__gt=0,
        item_id__in=grey_item_ids,
    )
    if location_id:
        good_takas = good_takas.filter(location_id=location_id)

    physical_grey_qty = ZERO
    taka_reserved_qty = ZERO
    taka_on_hand_by_item_loc = {}
    grey_takas_count = 0

    for lt in good_takas:
        if location_role.get(lt.location_id, "GOOD") != "GOOD":
            continue
        taka_on_hand_by_item_loc[(lt.item_id, lt.location_id)] = (
            taka_on_hand_by_item_loc.get((lt.item_id, lt.location_id), ZERO) + D(lt.available_quantity)
        )
        lt_qty = round4(lt.available_quantity)

        if lt.status == "Reserved":
            taka_reserved_qty += lt_qty
        elif (
            lt.status == "Finished"
            or (lt.source_document_type in ("JobWorkInward", "JWOInward", "Production"))
            or lt.parent_taka_id is not None
        ):
            # Finished fabric -- excluded from grey stock
            pass
        elif lt.status in ("Available", "Active"):
            physical_grey_qty += lt_qty
            grey_takas_count += 1

    # 6. Loose stock for grey items (balances on hand minus takas)
    for b in balance_rows:
        if location_role.get(b.location_id, "GOOD") != "GOOD":
            continue
        loose = round4(D(b.on_hand or ZERO) - taka_on_hand_by_item_loc.get((b.item_id, b.location_id), ZERO))
        if loose > ZERO:
            physical_grey_qty += loose

    # 7. Sales order reservations for grey items
    so_reserved_map = reserved_quantity(client_id, grey_item_ids)
    so_reserved_qty = sum((round4(v) for v in so_reserved_map.values()), ZERO)
    reserved_grey_qty = max(so_reserved_qty, taka_reserved_qty)

    # 8. Available Grey Stock
    available_grey_qty = max(ZERO, physical_grey_qty - reserved_grey_qty)

    qualities = {item.fabric_quality or item.name for item in grey_items if item.fabric_quality or item.name}

    return {
        "physical_grey_stock": round4(physical_grey_qty),
        "reserved_grey_stock": round4(reserved_grey_qty),
        "available_grey_stock": round4(available_grey_qty),
        "external_wip": round4(external_wip_qty),
        "wip_by_party": wip_by_party,
        "uom": "Mtr",
        "qualities_count": len(qualities),
        "takas_count": grey_takas_count,
        "incompatible_items": incompatible_items,
    }


def get_combined_grey_stock_setting(client_id):
    """Return configured minimum threshold and alert preference for combined grey stock."""
    from apps.core.models import AlertSetting
    setting = AlertSetting.objects.filter(client_id=client_id, alert_key="combined_grey_stock_low").first()
    if setting is None:
        return {
            "threshold": ZERO_QTY,
            "is_enabled": True,
            "is_configured": False,
            "uom": "Mtr",
            "channels": ["in_app", "email"],
            "setting_id": None,
        }
    threshold = round4(setting.threshold_amount or ZERO)
    return {
        "threshold": threshold,
        "is_enabled": bool(setting.is_enabled),
        "is_configured": bool(threshold > ZERO),
        "uom": "Mtr",
        "channels": setting.channels or ["in_app"],
        "setting_id": str(setting.id),
    }


@transaction.atomic
def set_combined_grey_stock_setting(client_id, threshold, is_enabled=True, channels=None, user=None):
    """Persist the minimum grey stock threshold in backend AlertSetting."""
    from apps.core.audit import record_audit, snapshot
    from apps.core.models import AlertSetting

    threshold = round4(D(threshold or 0))
    setting, created = AlertSetting.objects.select_for_update().get_or_create(
        client_id=client_id,
        alert_key="combined_grey_stock_low",
        defaults={
            "name": "Combined Total Grey Stock Level",
            "description": "Combined available grey fabric stock across all qualities falls below configured minimum threshold.",
            "threshold_amount": threshold,
            "is_enabled": bool(is_enabled),
            "channels": channels or ["in_app", "email"],
        },
    )
    if not created:
        before = snapshot(setting)
        setting.threshold_amount = threshold
        if is_enabled is not None:
            setting.is_enabled = bool(is_enabled)
        if channels is not None:
            setting.channels = channels
        setting.save(update_fields=["threshold_amount", "is_enabled", "channels", "updated_at"])
        record_audit(
            client=client_id,
            actor=user,
            action="update",
            entity_type="AlertSetting",
            entity_id=setting.id,
            entity_label="Combined Total Grey Stock Level",
            description=f"Combined grey stock minimum threshold set to {threshold} Mtr (enabled: {setting.is_enabled})",
            before=before,
            after=snapshot(setting),
        )

    # Immediately evaluate alert and return state
    evaluate_combined_grey_stock_alert(client_id)
    return get_combined_grey_stock_setting(client_id)


def evaluate_combined_grey_stock_alert(client_id):
    """
    Evaluates combined grey fabric inventory against the configured minimum threshold.
    - If disabled or threshold <= 0, auto-resolves any existing active incident.
    - If available grey stock < threshold, triggers or updates active incident.
    - If available grey stock >= threshold, auto-resolves existing active incident.
    """
    from apps.core import alert_services
    from apps.core.models import AlertIncident

    setting = get_combined_grey_stock_setting(client_id)
    calc = calculate_combined_grey_stock(client_id)
    available = calc["available_grey_stock"]
    threshold = setting["threshold"]

    active_incident = AlertIncident.objects.filter(
        client_id=client_id,
        alert_key="combined_grey_stock_low",
        entity_type="GreyStock",
        status="Active",
    ).first()

    if not setting["is_enabled"] or not setting["is_configured"]:
        if active_incident:
            active_incident.status = "Resolved"
            active_incident.resolved_at = timezone.now()
            active_incident.save(update_fields=["status", "resolved_at", "updated_at"])
        return {
            "alert_triggered": False,
            "incident": None,
            "status": "Not Configured" if not setting["is_configured"] else "Disabled",
            "stock": calc,
            "setting": setting,
        }

    if available < threshold:
        shortage = round4(threshold - available)
        severity = "Critical" if available <= round4(threshold / Decimal("2")) else "Warning"
        title = f"Combined Grey Stock Low: {round2(available)} Mtr (Min {round2(threshold)} Mtr)"
        message = (
            f"Combined available grey fabric stock ({round2(available)} Mtr) is below the configured "
            f"minimum threshold of {round2(threshold)} Mtr across {calc['qualities_count']} fabric qualities. "
            f"Shortage: {round2(shortage)} Mtr. "
            f"(Physical Godown Grey: {round2(calc['physical_grey_stock'])} Mtr, Reserved: {round2(calc['reserved_grey_stock'])} Mtr, "
            f"External WIP: {round2(calc['external_wip'])} Mtr)."
        )
        metadata = {
            "current_stock": float(available),
            "physical_stock": float(calc["physical_grey_stock"]),
            "reserved_stock": float(calc["reserved_grey_stock"]),
            "external_wip": float(calc["external_wip"]),
            "threshold": float(threshold),
            "shortage": float(shortage),
            "uom": "Mtr",
            "qualities_count": calc["qualities_count"],
            "takas_count": calc["takas_count"],
            "metric": "available_grey_stock",
        }

        incident = alert_services.trigger_incident(
            client_id=client_id,
            alert_key="combined_grey_stock_low",
            severity=severity,
            title=title,
            message=message,
            entity_type="GreyStock",
            entity_id=str(client_id),
            entity_number="GREY-STOCK",
            metadata=metadata,
        )
        return {
            "alert_triggered": True,
            "incident": incident,
            "status": "Critical" if severity == "Critical" else "Low Stock",
            "shortage": shortage,
            "stock": calc,
            "setting": setting,
        }
    else:
        # Stock recovered above threshold
        if active_incident:
            active_incident.status = "Resolved"
            active_incident.resolved_at = timezone.now()
            active_incident.save(update_fields=["status", "resolved_at", "updated_at"])
        return {
            "alert_triggered": False,
            "incident": None,
            "status": "Optimal",
            "shortage": ZERO_QTY,
            "stock": calc,
            "setting": setting,
        }


