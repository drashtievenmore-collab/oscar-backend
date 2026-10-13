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

from .models import REVERSAL_OF, StockBalance, StockMovement

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
            from apps.masters.models import Item

            uom = Item.objects.filter(pk=item_id).values_list("uom", flat=True).first()

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
        movement_date=movement_date or timezone.localdate(),
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        original_movement=original_movement,
    )

    if original_movement is not None:
        # The one write that must survive the append-only rule: back-filling
        # the link on the original row (db.md §7.1).
        StockMovement.objects.filter(pk=original_movement.pk).update(
            reversal_movement=movement
        )

    _apply_to_balance(movement)
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
    originals = StockMovement.objects.select_for_update().filter(
        client_id=client_id,
        reference_type=reference_type,
        reference_id=reference_id,
        reversal_movement__isnull=True,
    ).exclude(type__in=list(REVERSAL_OF.keys()))

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
            )
        )
    return reversals


def _reversal_type_for(movement_type):
    for reversal, original in REVERSAL_OF.items():
        if original == movement_type:
            return reversal
    if movement_type == "PURCHASE_RETURN":
        return "PURCHASE"
    return None


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
    return {
        row["item_id"]: max(row["reserved"] or ZERO, ZERO) for row in rows
    }


def balances_for(client_id, item_ids=None, location_id=None):
    """``on_hand`` and ``damaged`` per item, optionally at one location."""
    queryset = StockBalance.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if item_ids is not None:
        queryset = queryset.filter(item_id__in=item_ids)
    if location_id:
        queryset = queryset.filter(location_id=location_id)

    rows = queryset.values("item_id").annotate(
        on_hand=_dec("on_hand"), damaged=_dec("damaged")
    )
    return {row["item_id"]: row for row in rows}


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
    reserved = reserved_quantity(client_id, [item_id]).get(item_id, ZERO) or ZERO
    available = on_hand - reserved

    return {
        "onHand": on_hand,
        "reserved": reserved,
        "damaged": damaged,
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
        reserved = reserved_map.get(item.id, ZERO) or ZERO
        item.on_hand_qty = on_hand
        item.reserved_qty = reserved
        item.damaged_qty = damaged
        item.available_qty = on_hand - reserved
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
            qc_status__in=["Pending", "On Hold", "Rejected"],
            lines__item_id=item_id,
        )
        .select_related("purchase_bill")
        .order_by("-receipt_date")
        .first()
    )
    if receipt is None:
        return {"blocked": False, "billId": None, "reason": None}
    return {
        "blocked": True,
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
