"""
Purchase services (api.md §6).

The two rules that make this module fabrication-specific rather than generic
ERP are both here:

  - **weight-variance receiving** (§6.3) -- steel arrives on a weighbridge, not
    a counter, and a receipt outside tolerance is forced to Pending Approval
    whatever the user submitted
  - **landed cost** (§6.4) -- freight is apportioned across lines by value and
    the resulting unit cost is what valuation uses
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    ValidationFailed,
)
from apps.core.money import ZERO, D, ROUNDING_TOLERANCE, round2, round4
from apps.core.numbering import allocate_number
from apps.inventory import services as stock
from apps.sales.services import (
    allocated_to,
    assert_no_dependents,
    recalculate_document,
    _default_location_id,
)

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorBill,
)

#: api.md §6.3 -- tolerancePct resolution order: line -> item -> 2.
DEFAULT_TOLERANCE_PCT = Decimal("2")


# ---------------------------------------------------------------------------
# Weight-variance receiving (api.md §6.3) -- the core fabrication rule
# ---------------------------------------------------------------------------
def resolve_tolerance(line, item):
    if line is not None and getattr(line, "tolerance_pct", None) is not None:
        return D(line.tolerance_pct)
    if item is not None and getattr(item, "tolerance_pct", None) is not None:
        return D(item.tolerance_pct)
    return DEFAULT_TOLERANCE_PCT


def weight_variance(*, theoretical_weight, ordered_qty, received_qty, received_weight):
    """api.md §6.3.

        expectedWeight = theoreticalWeight * orderedQty
        receivedWeight = weighed value (defaults to theoreticalWeight * receivedQty)
        variationPct   = (receivedWeight - expectedWeight) / expectedWeight * 100
    """
    theoretical_weight = D(theoretical_weight)
    expected = round4(theoretical_weight * D(ordered_qty))
    if received_weight is None:
        received_weight = round4(theoretical_weight * D(received_qty))
    else:
        received_weight = round4(received_weight)

    if expected == ZERO:
        return expected, received_weight, None

    variation = round4(
        (received_weight - expected) / expected * Decimal("100")
    )
    return expected, received_weight, variation


@transaction.atomic
def receive_bill_goods(bill, *, lines_payload, qc_status=None, user=None, location=None):
    """``POST /purchase/bills/{id}/receive-goods/`` (api.md §6.3, §6.4).

    Replaces ``receivePurchaseBillGoods(billId, overrides, qcStatus)``.

    A line may be addressed by ``lineId``, ``lineIndex`` or ``sku``, because
    all three appear in the frontend's call sites.
    """
    bill = PurchaseBill.objects.select_for_update().get(pk=bill.pk)
    if bill.status == "Cancelled":
        raise Conflict("This bill has been cancelled.", code=Codes.ALREADY_CANCELLED)
    if bill.goods_received:
        raise Conflict(
            "Goods have already been received against this bill.",
            code=Codes.ALREADY_DONE,
        )

    lines = list(bill.line_items.select_related("item").order_by("line_no"))
    if not lines:
        raise ValidationFailed(
            "This bill has no lines to receive.",
            field_errors={"lineItems": ["Nothing to receive."]},
        )

    overrides = _index_overrides(lines_payload, lines)
    location_id = (
        getattr(location, "id", location)
        or bill.location_id
        or _default_location_id(bill.client_id)
    )

    has_weight_item = False
    variance_breach = False

    for index, line in enumerate(lines):
        override = overrides.get(line.id, {})
        received_qty = D(override.get("receivedQty", override.get("received_qty", line.qty)))
        line.received_qty = round4(received_qty)
        line.batch_number = override.get("batchNumber") or override.get("batch_number") or line.batch_number

        if received_qty > D(line.qty) + ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                f"Receiving {round4(received_qty)} of {line.item_name} exceeds the "
                f"billed quantity ({round4(line.qty)}).",
                code=Codes.OVER_RECEIPT,
                payload={"lineId": str(line.id), "billed": str(round4(line.qty))},
            )

        item = line.item
        is_weight_item = bool(item and item.is_weight_item)
        line.is_weight_item = is_weight_item

        if is_weight_item and item and D(item.theoretical_weight) > ZERO:
            has_weight_item = True
            tolerance = resolve_tolerance(line, item)
            raw_weight = override.get("receivedWeight", override.get("received_weight"))
            expected, received_weight, variation = weight_variance(
                theoretical_weight=item.theoretical_weight,
                ordered_qty=line.qty,
                received_qty=received_qty,
                received_weight=D(raw_weight) if raw_weight not in (None, "") else None,
            )
            line.theoretical_weight = item.theoretical_weight
            line.tolerance_pct = tolerance
            line.received_weight = received_weight
            line.variation_pct = variation

            if variation is not None and abs(variation) > tolerance:
                variance_breach = True

    # api.md §6.3: a breach overrides whatever qcStatus the user submitted.
    final_qc = qc_status or "Approved"
    if variance_breach:
        final_qc = "Pending Approval"
    if final_qc not in [value for value, _ in [(s, s) for s in
                        ("Approved", "Pending Approval", "Rejected", "Rework")]]:
        raise ValidationFailed(
            "Unknown QC status.",
            field_errors={"qcStatus": ["Expected Approved, Pending Approval, Rejected or Rework."]},
        )

    # api.md §6.3: when ANY line is a weight item, the bill is revalued at the
    # received weight.
    if has_weight_item:
        _revalue_bill_at_received(bill, lines)
    else:
        for line in lines:
            line.received_qty = line.received_qty or line.qty

    apportion_landed_cost(bill, lines)

    PurchaseBillLine.objects.bulk_update(
        lines,
        [
            "received_qty", "received_weight", "theoretical_weight", "tolerance_pct",
            "variation_pct", "is_weight_item", "batch_number", "qty", "amount",
            "discount_amount", "tax_amount", "line_total", "base_unit_cost",
            "apportioned_cost", "landed_unit_cost", "updated_at",
        ],
    )

    receipt = _create_goods_receipt(bill, lines, location_id, final_qc, user=user)
    _post_receipt_stock(bill, lines, location_id, overrides, receipt, user=user)

    bill.goods_received = True
    bill.received_date = timezone.localdate()
    bill.qc_status = final_qc
    if bill.status == "Draft":
        bill.status = "Unpaid"
        bill.posted_at = timezone.now()
        bill.posted_by = user if getattr(user, "is_authenticated", False) else None
        if not bill.bill_number:
            bill.bill_number = allocate_number(bill.client, "BILL", bill.doc_date)
    bill.save()

    if bill.journal_entry_id is None:
        entry = ledger.post_purchase_bill(bill, user=user)
        if entry is not None:
            bill.journal_entry = entry
            bill.save(update_fields=["journal_entry", "updated_at"])

    _bump_po_received(lines)
    ledger.recalculate_party_balance(bill.client_id, bill.party_id)

    return {
        "bill": bill,
        "receipt": receipt,
        "qcStatus": final_qc,
        "varianceBreach": variance_breach,
    }


def _index_overrides(lines_payload, lines):
    """Accept ``lineId``, ``lineIndex`` or ``sku`` as the line key (api.md §6.4)."""
    by_id = {line.id: line for line in lines}
    by_index = {index: line for index, line in enumerate(lines)}
    by_sku = {}
    for line in lines:
        if line.sku:
            by_sku.setdefault(line.sku, line)

    resolved = {}
    for row in lines_payload or []:
        line = None
        if row.get("lineId") or row.get("line_id"):
            key = row.get("lineId") or row.get("line_id")
            line = next((candidate for candidate in lines if str(candidate.id) == str(key)), None)
        if line is None and row.get("lineIndex") is not None:
            line = by_index.get(int(row["lineIndex"]))
        if line is None and row.get("line_index") is not None:
            line = by_index.get(int(row["line_index"]))
        if line is None and row.get("sku"):
            line = by_sku.get(row["sku"])
        if line is None:
            raise ValidationFailed(
                "A receipt line did not match any line on this bill.",
                field_errors={"lines": [f"Unmatched line: {row}"]},
            )
        resolved[line.id] = row
    return resolved


def _revalue_bill_at_received(bill, lines):
    """api.md §6.3 -- recompute each line amount at the received quantity.

        amount = round2(qty * rate * (1 - disc/100) * (1 + tax/100))

    then sum, plus freight and other charges, and rewrite total / balance.
    The billed quantity becomes the received quantity, because that is what the
    vendor is owed for.
    """
    for line in lines:
        line.qty = round4(line.received_qty)

    # recalculate_document applies the canonical api.md §5.7 algorithm, which
    # is the same arithmetic expressed per-component -- keeping one
    # implementation rather than two that must agree.
    recalculate_document(bill, lines=lines, save=False)


def apportion_landed_cost(bill, lines):
    """db.md §6.2 -- freight and other charges apportioned across lines by value.

        landed_unit_cost = base_unit_cost + apportioned_cost / qty

    Stored in three parts so the apportionment stays auditable.
    """
    charges = round2(D(bill.freight_charges) + D(bill.other_charges))
    taxable_total = round2(sum((D(line.amount) - D(line.discount_amount) for line in lines), ZERO))

    running = ZERO
    for index, line in enumerate(lines):
        line.base_unit_cost = round4(line.rate)
        line_value = round2(D(line.amount) - D(line.discount_amount))

        if charges == ZERO or taxable_total == ZERO:
            share = ZERO
        elif index == len(lines) - 1:
            # The last line absorbs the rounding remainder so the shares always
            # add back to `charges` exactly.
            share = round2(charges - running)
        else:
            share = round2(charges * line_value / taxable_total)
            running += share

        line.apportioned_cost = share
        qty = D(line.received_qty) or D(line.qty)
        line.landed_unit_cost = (
            round4(line.base_unit_cost + share / qty) if qty > ZERO else line.base_unit_cost
        )
    return lines


def _create_goods_receipt(bill, lines, location_id, qc_status, *, user=None):
    """A first-class GRN, which db.md §6.1 recommends even though the frontend
    has none -- the bill-scoped endpoint above is what the page calls."""
    receipt = GoodsReceipt.objects.create(
        client=bill.client,
        grn_number=allocate_number(bill.client, "GRN"),
        purchase_order=bill.purchase_order,
        purchase_bill=bill,
        party=bill.party,
        receipt_date=timezone.localdate(),
        location_id=location_id,
        qc_status="Approved" if qc_status == "Approved" else "Pending",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    GoodsReceiptLine.objects.bulk_create(
        [
            GoodsReceiptLine(
                client=bill.client,
                goods_receipt=receipt,
                purchase_order_line=line.purchase_order_line,
                purchase_bill_line=line,
                item=line.item,
                ordered_qty=line.qty,
                received_qty=line.received_qty,
                weighed_qty=line.received_weight,
                batch_number=line.batch_number,
                unit_cost=line.landed_unit_cost,
                variation_pct=line.variation_pct,
            )
            for line in lines
            if line.item_id is not None
        ]
    )
    return receipt


def _post_receipt_stock(bill, lines, location_id, overrides, receipt, *, user=None):
    """Posts ``PURCHASE`` movements carrying ``weighedQty`` for weight items."""
    for line in lines:
        if line.item_id is None or not line.item.holds_stock:
            continue
        quantity = D(line.received_qty)
        if quantity <= ZERO:
            continue

        movement = stock.post_movement(
            client_id=bill.client_id,
            item=line.item_id,
            location=location_id,
            type="PURCHASE",
            quantity=quantity,
            weighed_qty=line.received_weight if line.is_weight_item else None,
            unit_cost=line.landed_unit_cost,
            reference_type="PurchaseBill",
            reference_id=bill.id,
            reference_number=bill.bill_number,
            source_document_type="GoodsReceipt",
            source_document_id=receipt.id,
            batch_number=line.batch_number,
            movement_date=timezone.localdate(),
            user=user,
        )

        # Serial-tracked lines add their serials to the item's pool on receipt.
        override = overrides.get(line.id, {})
        serial_numbers = override.get("serials") or []
        if serial_numbers and line.item.tracking_mode == "Serial":
            _register_serials(
                bill.client_id, line.item, serial_numbers, location_id, movement,
                line.batch_number,
            )


def _register_serials(client_id, item, serial_numbers, location_id, movement, batch_number):
    from apps.masters.models import ItemSerial

    existing = set(
        ItemSerial.objects.filter(
            client_id=client_id, item=item, serial_no__in=serial_numbers
        ).values_list("serial_no", flat=True)
    )
    duplicates = [s for s in serial_numbers if s in existing]
    if duplicates:
        raise BusinessRuleViolation(
            f"Serial number{'s' if len(duplicates) > 1 else ''} already on record: "
            f"{', '.join(duplicates)}.",
            code=Codes.SERIAL_MISMATCH,
            payload={"duplicates": duplicates},
        )
    ItemSerial.objects.bulk_create(
        [
            ItemSerial(
                client_id=client_id,
                item=item,
                serial_no=str(serial).strip(),
                location_id=location_id,
                status="available",
                batch_number=batch_number,
                received_movement=movement,
            )
            for serial in serial_numbers
            if str(serial).strip()
        ]
    )


def _bump_po_received(lines):
    for line in lines:
        if line.purchase_order_line_id is None:
            continue
        po_line = PurchaseOrderLine.objects.select_for_update().get(
            pk=line.purchase_order_line_id
        )
        po_line.received_qty = round4(D(po_line.received_qty) + D(line.received_qty))
        po_line.billed_qty = round4(D(po_line.billed_qty) + D(line.qty))
        po_line.save(update_fields=["received_qty", "billed_qty", "updated_at"])


# ---------------------------------------------------------------------------
# QC (api.md §6.4)
# ---------------------------------------------------------------------------
@transaction.atomic
def update_qc_status(bill, status, *, note=None, user=None):
    """Only ``Approved`` releases stock for sale or dispatch."""
    if status not in ("Approved", "Pending Approval", "Rejected", "Rework"):
        raise ValidationFailed(
            "Unknown QC status.",
            field_errors={"status": ["Expected Approved, Pending Approval, Rejected or Rework."]},
        )

    bill.qc_status = status
    bill.qc_note = note
    bill.save(update_fields=["qc_status", "qc_note", "updated_at"])

    GoodsReceipt.objects.filter(purchase_bill=bill, deleted_at__isnull=True).update(
        qc_status="Approved" if status == "Approved" else (
            "Rejected" if status == "Rejected" else "On Hold"
        ),
        qc_note=note,
        qc_by=user if getattr(user, "is_authenticated", False) else None,
        qc_at=timezone.now(),
    )
    return bill


# ---------------------------------------------------------------------------
# PO billed status (api.md §6.2)
# ---------------------------------------------------------------------------
def po_billed_status(order):
    """``getPoBilledStatus`` moved server-side.

    The UI renders this instead of the stored status, so it is returned on the
    PO detail as well as from ``/billed-status/``. Cancelled bills are excluded
    from the rollup.
    """
    lines = list(order.line_items.filter(deleted_at__isnull=True).order_by("line_no"))

    active_bills = list(
        PurchaseBill.objects.filter(
            purchase_order=order, deleted_at__isnull=True
        ).exclude(status="Cancelled")
    )
    active_bill_ids = [bill.id for bill in active_bills]

    billed_by_line = {}
    if active_bill_ids:
        rows = (
            PurchaseBillLine.objects.filter(
                purchase_bill_id__in=active_bill_ids,
                purchase_order_line__isnull=False,
                deleted_at__isnull=True,
            )
            .values("purchase_order_line_id")
            .annotate(
                billed=Coalesce(
                    Sum("qty"), Value(Decimal("0.0000")),
                    output_field=DecimalField(max_digits=18, decimal_places=4),
                )
            )
        )
        billed_by_line = {row["purchase_order_line_id"]: row["billed"] for row in rows}

    line_rows = []
    total_ordered = total_billed = ZERO
    for line in lines:
        ordered = D(line.qty)
        billed = D(billed_by_line.get(line.id, ZERO))
        total_ordered += ordered
        total_billed += billed
        line_rows.append(
            {
                "lineId": str(line.id),
                "sku": line.sku,
                "itemName": line.item_name,
                "orderedQty": round4(ordered),
                "billedQty": round4(billed),
                "remainingQty": round4(max(ordered - billed, ZERO)),
            }
        )

    if order.status == "Cancelled":
        status = "Cancelled"
    elif total_ordered > ZERO and total_billed >= total_ordered:
        status = "Billed"
    elif total_billed > ZERO:
        status = "Partially Billed"
    else:
        status = order.status

    return {
        "status": status,
        "totalOrderedQty": round4(total_ordered),
        "totalBilledQty": round4(total_billed),
        "totalRemainingQty": round4(max(total_ordered - total_billed, ZERO)),
        "lines": line_rows,
        "activeBills": [
            {"id": str(bill.id), "billNumber": bill.bill_number} for bill in active_bills
        ],
    }


def assert_po_cancellable(order):
    """api.md §6.2 -- a PO with any active bill cannot be cancelled."""
    blocking = list(
        PurchaseBill.objects.filter(purchase_order=order, deleted_at__isnull=True)
        .exclude(status="Cancelled")
        .values("id", "bill_number")
    )
    if blocking:
        names = ", ".join(row["bill_number"] or str(row["id"]) for row in blocking)
        raise Conflict(
            f"This purchase order is billed on {names}. Cancel the bill first.",
            code=Codes.HAS_DEPENDENTS,
            payload={"bills": [{"id": str(r["id"]), "billNumber": r["bill_number"]}
                               for r in blocking]},
        )


def auto_po_suggestions(client_id):
    """``GET /purchase/orders/auto-suggestions/`` (api.md §6.2).

    Items below their reorder level, grouped by vendor. Replaces AutoPOModal's
    client-side scan, which under-reports the moment the item list is paginated
    (api-integration.md §9.1.3).
    """
    from apps.masters.models import Item

    items = list(
        Item.objects.filter(
            client_id=client_id, deleted_at__isnull=True, lifecycle_status="Active"
        )
        .exclude(item_kind="Service")
        .select_related("vendor")
    )
    stock.annotate_items_with_stock(client_id, items)

    grouped = {}
    for item in items:
        if D(item.reorder_level) <= ZERO:
            continue
        if D(item.available_qty) > D(item.reorder_level):
            continue

        vendor_id = str(item.vendor_id) if item.vendor_id else "unassigned"
        bucket = grouped.setdefault(
            vendor_id,
            {
                "vendorId": str(item.vendor_id) if item.vendor_id else None,
                "vendorName": item.vendor.name if item.vendor_id else "Unassigned",
                "items": [],
            },
        )
        shortfall = round4(D(item.reorder_level) * 2 - D(item.available_qty))
        bucket["items"].append(
            {
                "itemId": str(item.id),
                "sku": item.sku,
                "name": item.name,
                "uom": item.uom,
                "availableQty": round4(item.available_qty),
                "reorderLevel": round4(item.reorder_level),
                "suggestedQty": shortfall if shortfall > ZERO else round4(item.reorder_level),
                "costPrice": round4(item.cost_price),
                "status": item.stock_status,
            }
        )
    return list(grouped.values())


def vendor_advance_balance(client_id, party_id):
    """``GET /purchase/vendors/{id}/advance-balance/`` -- unapplied advances.

    A query over ``payment_allocations``, not a balance column (db.md §5.4).
    """
    rows = PaymentOut.objects.filter(
        client_id=client_id, party_id=party_id, status="Active", deleted_at__isnull=True
    ).aggregate(
        paid=Coalesce(Sum("amount"), Value(Decimal("0.00")),
                      output_field=DecimalField(max_digits=18, decimal_places=2)),
        allocated=Coalesce(Sum("allocated_amount"), Value(Decimal("0.00")),
                           output_field=DecimalField(max_digits=18, decimal_places=2)),
    )
    advance = round2(rows["paid"] - rows["allocated"])
    return {
        "partyId": str(party_id),
        "totalPaid": rows["paid"],
        "allocated": rows["allocated"],
        "advanceBalance": advance if advance > ZERO else ZERO,
    }


def bill_outstanding(bill):
    from apps.core.money import ageing_bucket

    paid = round2(allocated_to(bill.client_id, "PurchaseBill", bill.id))
    outstanding = round2(D(bill.total) - paid)
    outstanding = outstanding if outstanding > ZERO else ZERO
    today = timezone.localdate()
    days_overdue = (today - bill.due_date).days if bill.due_date and outstanding > ZERO else 0
    return {
        "total": round2(bill.total),
        "paid": paid,
        "outstanding": outstanding,
        "dueDate": bill.due_date,
        "daysOverdue": max(days_overdue, 0),
        "ageingBucket": ageing_bucket(days_overdue),
    }


def refresh_bill_payment_status(bill):
    from apps.core.money import derive_payment_status

    if bill.status == "Cancelled":
        return bill
    bill.amount_paid = round2(allocated_to(bill.client_id, "PurchaseBill", bill.id))
    bill.status = derive_payment_status(
        bill.total, bill.amount_paid, finalized=bill.posted_at is not None
    )
    bill.save(update_fields=["amount_paid", "status", "updated_at"])
    return bill


# ---------------------------------------------------------------------------
# Payments out (api.md §6.5)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_payment_out(
    *, client, party, amount, payment_date, mode, bank_account=None,
    reference_number=None, notes=None, allocations=None, bill=None, user=None,
):
    from apps.sales.models import PaymentAllocation

    amount = round2(amount)
    if amount <= ZERO:
        raise BusinessRuleViolation(
            "Payment amount must be greater than zero.", code="PAYMENT_AMOUNT_INVALID"
        )
    if mode != "Cash" and bank_account is None:
        raise ValidationFailed(
            "Choose the bank account this payment was made from.",
            field_errors={"bankAccountId": ["Required for non-cash payments."]},
        )

    payment = PaymentOut.objects.create(
        client=client,
        payment_number=allocate_number(client, "PAY-OUT", payment_date),
        party=party,
        payment_date=payment_date,
        amount=amount,
        mode=mode,
        bank_account=bank_account,
        reference_number=reference_number,
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    requested = list(allocations or [])
    if not requested and bill is not None:
        requested = [{"billId": bill.id, "amount": amount}]

    allocated = ZERO
    for row in requested:
        bill_id = row.get("billId") or row.get("bill_id") or row.get("documentId")
        requested_amount = round2(row.get("amount"))
        if not bill_id or requested_amount <= ZERO:
            continue

        target = (
            PurchaseBill.objects.select_for_update()
            .filter(pk=bill_id, client_id=client.id, deleted_at__isnull=True)
            .first()
        )
        if target is None:
            raise ValidationFailed(
                "That bill no longer exists.", field_errors={"billId": ["Not found."]}
            )
        if target.status == "Cancelled":
            raise BusinessRuleViolation(
                "Cannot record payment against a cancelled bill.",
                code=Codes.PAYMENT_ON_CANCELLED,
            )

        already = round2(allocated_to(client.id, "PurchaseBill", target.id))
        balance = round2(D(target.total) - already)
        if balance <= ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                "This bill is already fully paid.", code=Codes.ALREADY_SETTLED
            )
        if requested_amount > balance + ROUNDING_TOLERANCE:
            raise BusinessRuleViolation(
                f"Payment amount ({requested_amount}) exceeds the remaining bill "
                f"balance ({balance}).",
                code=Codes.PAYMENT_EXCEEDS_BALANCE,
            )

        PaymentAllocation.objects.create(
            client_id=client.id,
            payment_id=payment.id,
            payment_side="out",
            document_type="PurchaseBill",
            document_id=target.id,
            amount=requested_amount,
            allocated_by=user if getattr(user, "is_authenticated", False) else None,
        )
        allocated += requested_amount
        refresh_bill_payment_status(target)

    payment.allocated_amount = round2(allocated)
    payment.save(update_fields=["allocated_amount", "updated_at"])

    entry = ledger.post_payment_out(payment, user=user)
    if entry is not None:
        payment.journal_entry = entry
        payment.save(update_fields=["journal_entry", "updated_at"])

    ledger.recalculate_party_balance(client.id, party)
    return payment


@transaction.atomic
def cancel_payment_out(payment, *, reason=None, user=None):
    from apps.sales.models import PaymentAllocation

    payment = PaymentOut.objects.select_for_update().get(pk=payment.pk)
    if payment.status == "Cancelled":
        raise Conflict("This payment is already cancelled.", code=Codes.ALREADY_CANCELLED)

    bill_ids = list(
        PaymentAllocation.objects.filter(
            client_id=payment.client_id, payment_side="out", payment_id=payment.id,
            document_type="PurchaseBill", deleted_at__isnull=True,
        ).values_list("document_id", flat=True)
    )
    PaymentAllocation.objects.filter(
        client_id=payment.client_id, payment_side="out", payment_id=payment.id
    ).update(deleted_at=timezone.now())

    payment.status = "Cancelled"
    payment.allocated_amount = ZERO
    payment.cancelled_at = timezone.now()
    payment.cancellation_reason = reason
    payment.save(
        update_fields=["status", "allocated_amount", "cancelled_at",
                       "cancellation_reason", "updated_at"]
    )
    ledger.reverse_document_entries(
        client_id=payment.client_id,
        source_document_type="PaymentOut",
        source_document_id=payment.id,
        user=user,
    )
    for target in PurchaseBill.objects.filter(pk__in=bill_ids):
        refresh_bill_payment_status(target)
    ledger.recalculate_party_balance(payment.client_id, payment.party_id)
    return payment


# ---------------------------------------------------------------------------
# Cancellation (api.md §6.9)
# ---------------------------------------------------------------------------
@transaction.atomic
def cancel_bill(bill, *, reason=None, user=None):
    """Reverses stock and ledger (api.md §6.4)."""
    from apps.sales.models import PaymentAllocation
    from apps.sales.services import _reverse_document

    bill = PurchaseBill.objects.select_for_update().get(pk=bill.pk)
    if bill.status == "Cancelled":
        raise Conflict("This bill is already cancelled.", code=Codes.ALREADY_CANCELLED)

    if PaymentAllocation.objects.filter(
        client_id=bill.client_id, document_type="PurchaseBill", document_id=bill.id,
        deleted_at__isnull=True,
    ).exists():
        raise BusinessRuleViolation(
            "Cannot cancel a bill that has recorded payments.",
            code="BILL_HAS_PAYMENTS",
        )

    assert_no_dependents(
        bill,
        [("Debit note", bill.returns.filter(deleted_at__isnull=True).exclude(status="Cancelled"))],
    )

    _reverse_document(bill, "PurchaseBill", user=user, reason=reason)

    for line in bill.line_items.all():
        if line.purchase_order_line_id:
            po_line = PurchaseOrderLine.objects.select_for_update().get(
                pk=line.purchase_order_line_id
            )
            po_line.billed_qty = max(round4(D(po_line.billed_qty) - D(line.qty)), ZERO)
            po_line.received_qty = max(
                round4(D(po_line.received_qty) - D(line.received_qty)), ZERO
            )
            po_line.save(update_fields=["billed_qty", "received_qty", "updated_at"])

    GoodsReceipt.objects.filter(purchase_bill=bill, deleted_at__isnull=True).update(
        deleted_at=timezone.now()
    )
    return bill


# ---------------------------------------------------------------------------
# Vendor-bill matching (VendorBill -> PO + GRN, read-only computation)
# ---------------------------------------------------------------------------
def compute_vendor_bill_matching(client_id, bill):
    """Compare a vendor bill's lines against its PO and GRN.

    Pure computation -- performs no DB writes. All Decimal values are
    converted to float in the output so the result is JSON-serializable.
    """
    if not isinstance(bill, VendorBill):
        bill = VendorBill.objects.filter(pk=getattr(bill, "pk", bill)).first()
        if bill is None:
            raise ValidationFailed(
                "That vendor bill no longer exists.",
                field_errors={"vendorBill": ["Not found."]},
            )

    bill_lines = list(
        bill.line_items.filter(deleted_at__isnull=True)
        .select_related("item")
        .order_by("line_no")
    )

    po = bill.purchase_order
    if po is not None and getattr(po, "deleted_at", None) is not None:
        po = None
    po_lines = []
    if po is not None:
        po_lines = list(
            PurchaseOrderLine.objects.filter(
                purchase_order=po, deleted_at__isnull=True
            )
            .select_related("item")
            .order_by("line_no")
        )

    grn = None
    if bill.goods_receipt_id is not None:
        grn = (
            GoodsReceipt.objects.filter(
                pk=bill.goods_receipt_id, deleted_at__isnull=True
            )
            .prefetch_related("lines__item")
            .first()
        )
    if grn is None and po is not None:
        grn = (
            GoodsReceipt.objects.filter(
                client_id=client_id or bill.client_id,
                purchase_order=po,
                deleted_at__isnull=True,
            )
            .order_by("-receipt_date", "-created_at")
            .prefetch_related("lines__item")
            .first()
        )
    grn_lines = list(grn.lines.all()) if grn is not None else []

    def _sku(value):
        return (value or "").strip().lower() or None

    def _po_line_key(line):
        item_id = getattr(line, "item_id", None)
        sku = _sku(getattr(line, "sku", None)) or _sku(
            getattr(getattr(line, "item", None), "sku", None)
        )
        return item_id, sku, getattr(line, "line_no", None)

    po_by_item = {}
    po_by_sku = {}
    po_by_line_no = {}
    for po_line in po_lines:
        item_id, sku, line_no = _po_line_key(po_line)
        if item_id is not None:
            po_by_item.setdefault(str(item_id), po_line)
        if sku is not None:
            po_by_sku.setdefault(sku, po_line)
        if line_no is not None:
            po_by_line_no.setdefault(line_no, po_line)

    grn_by_item = {}
    grn_by_sku = {}
    for grn_line in grn_lines:
        item_id = getattr(grn_line, "item_id", None)
        sku = _sku(getattr(getattr(grn_line, "item", None), "sku", None))
        if item_id is not None:
            grn_by_item.setdefault(str(item_id), grn_line)
        if sku is not None:
            grn_by_sku.setdefault(sku, grn_line)

    def _match_po(bill_line):
        item_id = getattr(bill_line, "item_id", None)
        if item_id is not None and str(item_id) in po_by_item:
            return po_by_item[str(item_id)]
        sku = _sku(getattr(bill_line, "sku", None)) or _sku(
            getattr(getattr(bill_line, "item", None), "sku", None)
        )
        if sku is not None and sku in po_by_sku:
            return po_by_sku[sku]
        line_no = getattr(bill_line, "line_no", None)
        if line_no is not None and line_no in po_by_line_no:
            return po_by_line_no[line_no]
        return None

    def _match_grn(bill_line):
        item_id = getattr(bill_line, "item_id", None)
        if item_id is not None and str(item_id) in grn_by_item:
            return grn_by_item[str(item_id)]
        sku = _sku(getattr(bill_line, "sku", None)) or _sku(
            getattr(getattr(bill_line, "item", None), "sku", None)
        )
        if sku is not None and sku in grn_by_sku:
            return grn_by_sku[sku]
        return None

    rows = []
    total_po_qty = ZERO
    total_bill_qty = ZERO
    total_grn_qty = ZERO
    total_po_amount = ZERO
    total_bill_amount = ZERO
    first_rate_diff = None
    all_match = True

    for bill_line in bill_lines:
        po_line = _match_po(bill_line) if po is not None else None
        grn_line = _match_grn(bill_line) if grn is not None else None

        bill_qty = D(getattr(bill_line, "qty", 0))
        bill_rate = D(getattr(bill_line, "rate", 0))
        bill_amount = D(getattr(bill_line, "amount", 0))
        po_qty = D(getattr(po_line, "qty", 0)) if po_line is not None else ZERO
        po_rate = D(getattr(po_line, "rate", 0)) if po_line is not None else ZERO
        po_amount = D(getattr(po_line, "amount", 0)) if po_line is not None else ZERO
        grn_qty = (
            D(getattr(grn_line, "received_qty", 0))
            if grn_line is not None
            else ZERO
        )

        qty_match = bill_qty == po_qty
        rate_match = bill_rate == po_rate
        amount_match = bill_amount == po_amount
        line_status = "Match" if (qty_match and rate_match and amount_match) else "Mismatch"
        if line_status != "Match":
            all_match = False
            if first_rate_diff is None:
                first_rate_diff = bill_rate - po_rate

        total_po_qty += po_qty
        total_bill_qty += bill_qty
        total_grn_qty += grn_qty
        total_po_amount += po_amount
        total_bill_amount += bill_amount

        fabric = (
            getattr(bill_line, "item_name", None)
            or getattr(po_line, "item_name", None)
            or getattr(getattr(bill_line, "item", None), "name", None)
        )
        sku = getattr(bill_line, "sku", None) or getattr(
            getattr(bill_line, "item", None), "sku", None
        )
        rows.append(
            {
                "lineNo": getattr(bill_line, "line_no", None),
                "fabric": fabric,
                "sku": sku,
                "poQty": float(po_qty),
                "grnQty": float(grn_qty),
                "billQty": float(bill_qty),
                "poRate": float(po_rate),
                "billRate": float(bill_rate),
                "poAmount": float(po_amount),
                "billAmount": float(bill_amount),
                "qtyMatch": qty_match,
                "rateMatch": rate_match,
                "amountMatch": amount_match,
                "status": line_status,
            }
        )

    overall = "Match" if all_match else "Mismatch"
    summary = {
        "poQty": float(total_po_qty),
        "billQty": float(total_bill_qty),
        "grnQty": float(total_grn_qty),
        "poAmount": float(total_po_amount),
        "billAmount": float(total_bill_amount),
        "qtyDiff": float(total_bill_qty - total_po_qty),
        "rateDiff": float(first_rate_diff) if first_rate_diff is not None else 0.0,
        "amountDiff": float(total_bill_amount - total_po_amount),
    }

    result = {
        "poNumber": getattr(po, "po_number", None),
        "grnNumber": getattr(grn, "grn_number", None),
        "lines": rows,
        "summary": summary,
        "overallStatus": overall,
    }
    if grn is None:
        result["note"] = "No GRN found for this purchase order."
    return result
