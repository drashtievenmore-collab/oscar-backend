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
from django.utils.dateparse import parse_date

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
from apps.masters import uom
from apps.sales.services import (
    allocated_to,
    assert_no_dependents,
    recalculate_document,
    _default_location_id,
)

from .models import (
    PURCHASE_TYPE_GREY_STOCK,
    PURCHASE_TYPE_VENDOR_A,
    PURCHASE_TYPE_FINAL_JOB_WORK,
    GREY_STOCK_PURCHASE_TYPES,
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    VendorBill,
    VendorBillLine,
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

    # Phase 1.4: received goods wait at QC Hold until QC passes them, so the
    # default is "Pending Approval", never an implicit "Approved".
    # api.md §6.3: a weight-variance breach overrides whatever was submitted.
    final_qc = qc_status or "Pending Approval"
    if variance_breach:
        final_qc = "Pending Approval"
    if final_qc not in ("Approved", "Pending Approval", "Rejected", "Rework"):
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

    # Stock was posted to QC Hold above. A verdict given at receipt time is a
    # QC decision like any other: it moves the goods out with real movements.
    if final_qc != "Pending Approval":
        from apps.inventory import qc as qc_service

        qc_service.dispose_all(receipt, final_qc, user=user, note=None)
        receipt.refresh_from_db()
        bill.refresh_from_db()

    return {
        "bill": bill,
        "receipt": receipt,
        "qcStatus": bill.qc_status,
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
        # The intended godown; the goods themselves sit at QC Hold until the
        # QC disposition releases them here.
        location_id=location_id,
        qc_status="Pending",
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
                uom=line.uom,
                uom_factor=line.uom_factor or 1,
                stock_uom=line.item.uom,
                stock_qty=round4(uom.to_stock_qty(line.received_qty, line.uom_factor)),
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
    """Posts ``PURCHASE`` movements into the **QC Hold** location.

    Received goods are never available until QC passes them (Phase 1.4); the
    QC disposition moves them to ``location_id`` (the receipt's godown).
    Lot/taka items get one movement per taka, so every taka's
    ``available_quantity`` is maintained by the ledger itself.
    """
    qc_hold_location_id = stock.system_location(bill.client_id, "QC_HOLD").id

    for line in lines:
        if line.item_id is None or not line.item.holds_stock:
            continue
        # received_qty is in the purchase unit; stock is kept in the item's unit.
        factor = D(line.uom_factor or 1)
        quantity = round4(uom.to_stock_qty(line.received_qty, factor))
        if quantity <= ZERO:
            continue

        override = overrides.get(line.id, {})
        unit_cost = D(line.landed_unit_cost) / factor if line.landed_unit_cost is not None else None
        common = dict(
            client_id=bill.client_id,
            item=line.item,
            location=qc_hold_location_id,
            type="PURCHASE",
            unit_cost=unit_cost,
            reference_type="PurchaseBill",
            reference_id=bill.id,
            reference_number=bill.bill_number,
            source_document_type="GoodsReceipt",
            source_document_id=receipt.id,
            movement_date=timezone.localdate(),
            user=user,
        )

        takas = _register_lot_taka_on_receipt(
            bill, line, qc_hold_location_id, receipt, quantity, override=override
        )
        first_movement = None
        if takas:
            weight_total = (
                D(line.received_weight) if line.is_weight_item and line.received_weight else None
            )
            weight_left = weight_total
            for index, (taka, taka_qty) in enumerate(takas):
                weighed = None
                if weight_total is not None:
                    if index == len(takas) - 1:
                        weighed = weight_left
                    else:
                        weighed = round4(weight_total * taka_qty / quantity)
                        weight_left -= weighed
                movement = stock.post_movement(
                    quantity=taka_qty,
                    weighed_qty=weighed,
                    lot_taka=taka,
                    batch_number=taka.lot_number,
                    **common,
                )
                taka.stock_movement = movement
                taka.save(update_fields=["stock_movement", "updated_at"])
                first_movement = first_movement or movement

                # Auto-post Grey purchase cost to lot cost ledger (PDF §09)
                try:
                    from apps.inventory import costing_services
                    taka_rate = unit_cost or (D(line.rate) if line.rate else (D(line.base_unit_cost) if line.base_unit_cost else ZERO))
                    cost_amount = round2(D(taka_qty) * D(taka_rate))
                    if cost_amount > ZERO:
                        costing_services.post_lot_cost(
                            client_id=bill.client_id,
                            lot_number=taka.lot_number,
                            cost_type="Grey purchase",
                            amount=cost_amount,
                            quantity=taka_qty,
                            source_document_type="PurchaseBill",
                            source_document_number=bill.bill_number,
                            lot_taka=taka,
                            user=user,
                            is_approved=True,
                            remarks=f"GRN against purchase bill {bill.bill_number}",
                        )
                except Exception:
                    pass
        else:
            first_movement = stock.post_movement(
                quantity=quantity,
                weighed_qty=line.received_weight if line.is_weight_item else None,
                batch_number=line.batch_number,
                **common,
            )

        # Serial-tracked lines add their serials to the item's pool on receipt.
        serial_numbers = override.get("serials") or []
        if serial_numbers and line.item.tracking_mode == "Serial":
            _register_serials(
                bill.client_id, line.item, serial_numbers, qc_hold_location_id, first_movement,
                line.batch_number,
            )


def _register_lot_taka_on_receipt(bill, line, location_id, receipt, received_quantity, override=None):
    """Create the LotTaka rows for a received line; returns ``[(taka, qty)]``.

    The takas start with ``available_quantity = 0`` -- the PURCHASE movement
    posted per taka is what fills them. Returns ``[]`` for items that are not
    held on lots/takas.
    """
    from apps.core.numbering import allocate_number
    from apps.inventory.models import LotTaka

    item = line.item
    tracked = getattr(item, "tracking_type", "NONE") in ("LOT", "TAKA")
    is_fabric = (
        tracked
        or getattr(item, "item_kind", None) == "Fabric"
        or bool(getattr(item, "fabric_quality", None))
        or getattr(item, "tracking_mode", None) == "Batch"
    )
    override = override or {}
    takas_spec = override.get("takas") or override.get("rolls") or []
    if not is_fabric and not line.batch_number and not takas_spec:
        return []

    lot_num = line.batch_number or allocate_number(bill.client, "LOT")
    if not line.batch_number:
        line.batch_number = lot_num
        line.save(update_fields=["batch_number", "updated_at"])
        receipt.lines.filter(purchase_bill_line=line).update(batch_number=lot_num)
    stock_uom = getattr(item, "uom", None) or line.uom
    rate = line.landed_unit_cost or line.base_unit_cost or getattr(line, "rate", 0) or 0
    grn_line = receipt.lines.filter(purchase_bill_line=line).first()

    if takas_spec:
        specs = []
        for idx, spec in enumerate(takas_spec):
            raw_qty = spec.get("qty", spec.get("quantity"))
            if raw_qty in (None, ""):
                raise ValidationFailed(
                    "Every taka needs its quantity.",
                    field_errors={"takas": [f"Taka {idx + 1}: quantity is required."]},
                )
            specs.append((idx, spec, round4(D(raw_qty))))
        spec_total = round4(sum((qty for _, _, qty in specs), ZERO))
        if abs(spec_total - received_quantity) > Decimal("0.001"):
            raise ValidationFailed(
                f"Taka quantities ({spec_total}) must add up to the received quantity "
                f"of {line.item_name} ({received_quantity}).",
                field_errors={"takas": ["Taka total does not match the received quantity."]},
            )
    else:
        specs = [(0, {}, received_quantity)]

    created = []
    for idx, spec, spec_qty in specs:
        if spec_qty <= ZERO:
            raise ValidationFailed(
                "Taka quantity must be greater than zero.",
                field_errors={"takas": [f"Taka {idx + 1}: quantity must be > 0."]},
            )
        base_number = str(
            spec.get("takaRoll") or spec.get("taka_number")
            or LotTaka.objects.filter(
                client_id=bill.client_id, lot_number=lot_num, deleted_at__isnull=True
            ).count() + 1
        )
        taka_code = allocate_number(bill.client, "TAK")
        roll_weight = spec.get("rollWeight") or spec.get("weight")
        width = spec.get("width")
        created.append((
            LotTaka.objects.create(
                client=bill.client,
                taka_id=taka_code,
                lot_number=lot_num,
                taka_number=stock._unique_taka_number(bill.client_id, lot_num, base_number),
                item=item,
                supplier=bill.party,
                source_document_type="GRN",
                source_document_number=receipt.grn_number,
                source_document_id=receipt.id,
                source_line_id=grn_line.id if grn_line else None,
                location_id=location_id,
                received_date=receipt.receipt_date or timezone.localdate(),
                received_quantity=spec_qty,
                available_quantity=ZERO,
                uom=stock_uom,
                qc_status="Pending",
                status="Active",
                barcode=taka_code,
                tag_status="Pending",
                print_count=0,
                # kg is only ever stored as roll_weight -- never in a meter field.
                roll_weight=round4(D(roll_weight)) if roll_weight not in (None, "") else None,
                width=D(str(width)) if width not in (None, "") else None,
                rate=rate,
            ),
            spec_qty,
        ))
    return created


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
        # The bill may be in another (compatible) unit than the order.
        po_line.received_qty = round4(
            D(po_line.received_qty) + uom.between_lines(line.received_qty, line, po_line)
        )
        po_line.billed_qty = round4(
            D(po_line.billed_qty) + uom.between_lines(line.qty, line, po_line)
        )
        po_line.save(update_fields=["received_qty", "billed_qty", "updated_at"])


# ---------------------------------------------------------------------------
# QC (api.md §6.4)
# ---------------------------------------------------------------------------
@transaction.atomic
def update_qc_status(bill, status, *, note=None, user=None):
    """Bill-level QC verdict (api.md §6.4) -- applied to every receipt of the bill.

    Whatever is still at QC Hold moves with real movements: Approved releases
    it to the receipt's godown, Rejected sends it to the Reject location,
    Rework to Reprocess; Pending Approval keeps it held (GRN On Hold).
    Item-level splits (good + reject + ...) go through the QC disposition
    screen (``apps.inventory.qc.dispose_receipt``) instead.
    """
    from apps.inventory import qc as qc_service

    if status not in ("Approved", "Pending Approval", "Rejected", "Rework"):
        raise ValidationFailed(
            "Unknown QC status.",
            field_errors={"status": ["Expected Approved, Pending Approval, Rejected or Rework."]},
        )

    receipts = list(
        GoodsReceipt.objects.filter(purchase_bill=bill, deleted_at__isnull=True)
    )
    for receipt in receipts:
        qc_service.dispose_all(receipt, status, user=user, note=note)

    bill.refresh_from_db()
    bill.qc_note = note
    if not receipts:
        bill.qc_status = status
    bill.save(update_fields=["qc_status", "qc_note", "updated_at"])
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

    # One payment can't settle more than it pays out.
    requested_total = round2(sum(
        (round2(row.get("amount")) for row in requested if round2(row.get("amount")) > ZERO),
        ZERO,
    ))
    if requested_total > amount + ROUNDING_TOLERANCE:
        raise BusinessRuleViolation(
            f"Allocations ({requested_total}) exceed the payment amount ({amount}).",
            code=Codes.PAYMENT_EXCEEDS_BALANCE,
        )

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
        if target.party_id and str(target.party_id) != str(getattr(party, "id", party)):
            raise ValidationFailed(
                "That bill belongs to a different vendor than this payment.",
                field_errors={"billId": ["Vendor mismatch."]},
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
    """Three-way match of a vendor bill against its PO and goods receipts.

    Pure computation -- performs no DB writes. All Decimal values are
    converted to float in the output so the result is JSON-serializable.

    Per line, three checks:

    1. PO vs GRN quantity -- receiving more than was ordered is a variance
       (a short / partial delivery is not; it is simply billed later).
    2. PO rate and tax vs the vendor bill.
    3. Received quantity vs billed quantity -- this bill plus every other
       live vendor bill on the same receipt may not exceed what was received.

    Receipts: the bill's own GRN when it names one, otherwise every GRN on
    the PO, so partial deliveries across several GRNs add up.
    """
    if not isinstance(bill, VendorBill):
        bill = VendorBill.objects.filter(pk=getattr(bill, "pk", bill)).first()
        if bill is None:
            raise ValidationFailed(
                "That vendor bill no longer exists.",
                field_errors={"vendorBill": ["Not found."]},
            )
    client_id = client_id or bill.client_id

    bill_lines = list(
        bill.line_items.filter(deleted_at__isnull=True)
        .select_related("item")
        .order_by("line_no")
    )

    po = bill.purchase_order
    if po is not None and getattr(po, "deleted_at", None) is not None:
        po = None

    # Detect if this is an Agency / Vendor A External Production 3-Way Match
    from apps.pms.models import ProductionInstruction
    from apps.pms import production_services

    instruction = None
    original_po = None
    final_po = None
    is_agency_match = False

    if po is not None:
        if getattr(po, "purchase_type", None) == PURCHASE_TYPE_FINAL_JOB_WORK:
            is_agency_match = True
            final_po = po
            instruction = ProductionInstruction.objects.filter(
                client_id=client_id, final_po=po, deleted_at__isnull=True
            ).select_related("grey_lot", "agency").first()
            if instruction and instruction.order_reference:
                original_po = PurchaseOrder.objects.filter(
                    client_id=client_id, po_number=instruction.order_reference, deleted_at__isnull=True
                ).first()
        elif getattr(po, "purchase_type", None) == PURCHASE_TYPE_VENDOR_A:
            is_agency_match = True
            original_po = po
            instruction = ProductionInstruction.objects.filter(
                client_id=client_id, order_reference=po.po_number, deleted_at__isnull=True
            ).select_related("grey_lot", "agency", "final_po").first()
            if instruction and instruction.final_po:
                final_po = instruction.final_po
        else:
            instruction = ProductionInstruction.objects.filter(
                client_id=client_id, order_reference=po.po_number, deleted_at__isnull=True
            ).select_related("grey_lot", "agency", "final_po").first()
            if instruction:
                is_agency_match = True
                original_po = po
                final_po = instruction.final_po

    po_lines = []
    # In an agency match, the commercially approved benchmark is the Final Job Work PO
    benchmark_po = final_po if (is_agency_match and final_po is not None) else po
    if benchmark_po is not None:
        po_lines = list(
            PurchaseOrderLine.objects.filter(
                purchase_order=benchmark_po, deleted_at__isnull=True
            )
            .select_related("item")
            .order_by("line_no")
        )

    grns = []
    if bill.goods_receipt_id is not None:
        grns = list(
            GoodsReceipt.objects.filter(
                client_id=client_id, pk=bill.goods_receipt_id, deleted_at__isnull=True
            ).prefetch_related("lines__item")
        )
    elif po is not None:
        grns = list(
            GoodsReceipt.objects.filter(
                client_id=client_id, purchase_order=po, deleted_at__isnull=True
            )
            .order_by("receipt_date", "created_at")
            .prefetch_related("lines__item")
        )
    grn_lines = [line for grn in grns for line in grn.lines.all() if line.deleted_at is None]
    # Ready grey stock is billed against what was received. Vendor A / final
    # job work POs have no GRN -- their PO quantity is the verified production
    # quantity, so it is the billing baseline instead.
    receipt_based = po is None or po.purchase_type in GREY_STOCK_PURCHASE_TYPES

    # Other live vendor bills against the same receipt(s): what they bill is
    # no longer available to bill here.
    other_bills = VendorBill.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(pk=bill.pk).exclude(status="Rejected")
    if bill.goods_receipt_id is not None:
        other_bills = other_bills.filter(goods_receipt_id=bill.goods_receipt_id)
    elif po is not None:
        other_bills = other_bills.filter(purchase_order=po)
    else:
        other_bills = other_bills.none()
    other_bill_lines = list(
        VendorBillLine.objects.filter(
            vendor_bill__in=other_bills, deleted_at__isnull=True
        ).select_related("item")
    )

    def _sku(value):
        return (value or "").strip().lower() or None

    def _key(line):
        item_id = getattr(line, "item_id", None)
        if item_id is not None:
            return f"id:{item_id}"
        sku = _sku(getattr(line, "sku", None)) or _sku(
            getattr(getattr(line, "item", None), "sku", None)
        )
        return f"sku:{sku}" if sku else None

    po_by_key = {}
    po_by_line_no = {}
    for po_line in po_lines:
        key = _key(po_line)
        if key is not None:
            po_by_key.setdefault(key, po_line)
        if po_line.line_no is not None:
            po_by_line_no.setdefault(po_line.line_no, po_line)

    def _sum_by_key(lines, field):
        totals = {}
        for line in lines:
            key = _key(line)
            if key is None:
                continue
            totals[key] = totals.get(key, ZERO) + D(getattr(line, field, 0) or 0)
        return totals

    grn_qty_by_key = _sum_by_key(grn_lines, "received_qty")
    grn_rejected_by_key = _sum_by_key(grn_lines, "rejected_qty")
    other_billed_by_key = _sum_by_key(other_bill_lines, "qty")

    def _match_po(bill_line):
        key = _key(bill_line)
        if key is not None and key in po_by_key:
            return po_by_key[key]
        if bill_line.line_no is not None:
            return po_by_line_no.get(bill_line.line_no)
        return None

    bill_tax_pct = D(bill.gst_pct or 0)
    rows = []
    total_po_qty = ZERO
    total_bill_qty = ZERO
    total_grn_qty = ZERO
    total_po_amount = ZERO
    total_bill_amount = ZERO
    first_rate_diff = None
    all_match = True
    reasons = []

    for bill_line in bill_lines:
        po_line = _match_po(bill_line) if po is not None else None
        key = _key(bill_line) or (_key(po_line) if po_line is not None else None)

        bill_qty = D(getattr(bill_line, "qty", 0))
        bill_rate = D(getattr(bill_line, "rate", 0))
        bill_amount = D(getattr(bill_line, "amount", 0))
        po_qty = D(po_line.qty) if po_line is not None else ZERO
        po_rate = D(po_line.rate) if po_line is not None else ZERO
        po_tax_pct = D(po_line.tax_pct or 0) if po_line is not None else None
        grn_qty = grn_qty_by_key.get(key, ZERO) if key else ZERO
        grn_rejected = grn_rejected_by_key.get(key, ZERO) if key else ZERO
        previously_billed = other_billed_by_key.get(key, ZERO) if key else ZERO
        baseline_qty = grn_qty if receipt_based else po_qty
        billable_qty = max(ZERO, baseline_qty - previously_billed)
        # What this bill should cost at the PO rate.
        po_amount = round2(bill_qty * po_rate) if po_line is not None else ZERO

        variances = []
        if po_line is None:
            variances.append("Item is not on the purchase order.")
        if receipt_based:
            if grns and grn_qty > po_qty and po_line is not None:
                variances.append(f"Received {grn_qty} against {po_qty} ordered (excess {grn_qty - po_qty}).")
            if not grns:
                variances.append("No goods receipt (GRN) has been posted for this bill.")
            elif bill_qty > billable_qty:
                variances.append(
                    f"Billed {bill_qty} but only {billable_qty} received and not yet billed "
                    f"(received {grn_qty}, billed on other bills {previously_billed})."
                )
        elif bill_qty > billable_qty:
            variances.append(
                f"Billed {bill_qty} but only {billable_qty} on the PO is not yet billed "
                f"(PO {po_qty}, billed on other bills {previously_billed})."
            )
        rate_match = po_line is not None and bill_rate == po_rate
        if po_line is not None and not rate_match:
            variances.append(f"Bill rate {bill_rate} differs from PO rate {po_rate}.")
        tax_match = po_tax_pct is None or bill_tax_pct == po_tax_pct
        if not tax_match:
            variances.append(f"Bill GST {bill_tax_pct}% differs from PO GST {po_tax_pct}%.")
        amount_match = po_line is not None and abs(bill_amount - po_amount) <= ROUNDING_TOLERANCE
        if po_line is not None and not amount_match:
            variances.append(f"Line amount {bill_amount} differs from {po_amount} at the PO rate.")

        if receipt_based:
            qty_match = bool(grns) and bill_qty <= billable_qty and (po_line is None or grn_qty <= po_qty)
        else:
            qty_match = po_line is not None and bill_qty <= billable_qty
        line_status = "Match" if not variances else "Mismatch"
        if variances:
            all_match = False
            if first_rate_diff is None and po_line is not None:
                first_rate_diff = bill_rate - po_rate
            reasons.extend(f"Line {bill_line.line_no}: {text}" for text in variances)

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
                "grnRejectedQty": float(grn_rejected),
                "previouslyBilledQty": float(previously_billed),
                "billableQty": float(billable_qty),
                "billQty": float(bill_qty),
                "poRate": float(po_rate),
                "billRate": float(bill_rate),
                "poTaxPct": float(po_tax_pct) if po_tax_pct is not None else None,
                "billTaxPct": float(bill_tax_pct),
                "poAmount": float(po_amount),
                "billAmount": float(bill_amount),
                "qtyMatch": qty_match,
                "rateMatch": rate_match,
                "taxMatch": tax_match,
                "amountMatch": amount_match,
                "variances": variances,
                "status": line_status,
            }
        )

    if not bill_lines:
        all_match = False
        reasons.append("The vendor bill has no lines.")

    # Header totals: the bill's GST and grand total must follow from its lines.
    expected_gst = round2(total_bill_amount * bill_tax_pct / Decimal("100"))
    expected_total = round2(total_bill_amount + expected_gst)
    total_match = (
        abs(D(bill.gst_amount or 0) - expected_gst) <= ROUNDING_TOLERANCE
        and abs(D(bill.total or 0) - expected_total) <= ROUNDING_TOLERANCE
    )
    if bill_lines and not total_match:
        all_match = False
        reasons.append(
            f"Invoice total {D(bill.total or 0)} (GST {D(bill.gst_amount or 0)}) does not equal "
            f"lines {total_bill_amount} + GST {expected_gst} = {expected_total}."
        )

    overall = "Match" if all_match else "Mismatch"
    summary = {
        "poQty": float(total_po_qty),
        "billQty": float(total_bill_qty),
        "grnQty": float(total_grn_qty),
        "poAmount": float(total_po_amount),
        "billAmount": float(total_bill_amount),
        "qtyDiff": float(total_bill_qty - total_grn_qty),
        "rateDiff": float(first_rate_diff) if first_rate_diff is not None else 0.0,
        "amountDiff": float(total_bill_amount - total_po_amount),
        "billGst": float(D(bill.gst_amount or 0)),
        "expectedGst": float(expected_gst),
        "billTotal": float(D(bill.total or 0)),
        "expectedTotal": float(expected_total),
        "totalMatch": total_match,
    }

    result = {
        "poNumber": getattr(po, "po_number", None),
        "grnNumber": ", ".join(g.grn_number for g in grns) or None,
        "grnNumbers": [g.grn_number for g in grns],
        "lines": rows,
        "summary": summary,
        "reasons": reasons,
        "overallStatus": overall,
    }
    if receipt_based and not grns:
        result["note"] = "No GRN found for this purchase order."

    # Build 3-Document Reconciliation for Agency / Vendor A transactions
    if is_agency_match:
        completion = production_services.instruction_completion(instruction) if instruction else {}
        orig_lines = list(original_po.line_items.filter(deleted_at__isnull=True)) if original_po else []
        orig_instructed_qty = D(str(getattr(instruction, "order_meter", None) or sum((l.qty for l in orig_lines), ZERO) or 0))
        verified_accepted_qty = D(str(completion.get("acceptedQty") or 0))
        final_po_lines = list(final_po.line_items.filter(deleted_at__isnull=True)) if final_po else []
        final_po_qty = sum((l.qty for l in final_po_lines), ZERO) if final_po_lines else verified_accepted_qty
        orig_agreed_rate = D(str(getattr(instruction, "agreed_job_rate", None) or getattr(instruction, "agreed_rate", None) or (original_po.line_items.first().rate if (original_po and original_po.line_items.exists()) else 0) or 0))
        final_approved_rate = D(str((final_po.line_items.first().rate if (final_po and final_po.line_items.exists()) else orig_agreed_rate) or 0))
        
        orig_taxable = round2(orig_instructed_qty * orig_agreed_rate)
        orig_tax_pct = D(str((original_po.line_items.first().tax_pct if (original_po and original_po.line_items.exists()) else 5) or 5))
        orig_tax_amount = round2(orig_taxable * orig_tax_pct / Decimal("100"))
        orig_total = round2(orig_taxable + orig_tax_amount)

        final_taxable = round2(final_po_qty * final_approved_rate) if final_po_qty > 0 else D(str(getattr(final_po, "subtotal", 0) or 0))
        final_tax_pct = D(str((final_po.line_items.first().tax_pct if (final_po and final_po.line_items.exists()) else orig_tax_pct) or 5))
        final_tax_amount = round2(final_taxable * final_tax_pct / Decimal("100")) if final_taxable > 0 else D(str(getattr(final_po, "tax_amount", 0) or 0))
        final_total = round2(final_taxable + final_tax_amount) if final_taxable > 0 else D(str(getattr(final_po, "total", 0) or 0))

        inv_qty = total_bill_qty
        inv_rate = D(str(rows[0]["billRate"] if rows else 0))
        inv_taxable = total_bill_amount
        inv_tax_pct = bill_tax_pct
        inv_tax_amount = D(str(bill.gst_amount or 0))
        inv_total = D(str(bill.total or 0))

        # Field comparisons
        qty_diff = inv_qty - final_po_qty
        rate_diff = inv_rate - final_approved_rate
        taxable_diff = inv_taxable - final_taxable
        tax_amount_diff = inv_tax_amount - final_tax_amount
        total_diff = inv_total - final_total

        qty_matched = abs(qty_diff) <= Decimal("0.05")
        rate_matched = abs(rate_diff) <= Decimal("0.05")
        taxable_matched = abs(taxable_diff) <= Decimal("1.00")
        tax_matched = abs(tax_amount_diff) <= Decimal("1.00") and inv_tax_pct == final_tax_pct
        total_matched = abs(total_diff) <= Decimal("1.00")

        # Legitimate production variance (Original PO vs Final PO)
        prod_shortage = orig_instructed_qty - final_po_qty
        prod_shortage_pct = (prod_shortage / orig_instructed_qty * Decimal("100")) if orig_instructed_qty > 0 else Decimal("0")

        three_way_reconciliation = [
            {
                "field": "Vendor / Agency",
                "originalPoVal": str(getattr(original_po, "party_name", "") or getattr(getattr(original_po, "party", None), "name", "—")),
                "finalPoVal": str(getattr(final_po, "party_name", "") or getattr(getattr(final_po, "party", None), "name", getattr(instruction, "agency_name", "—"))),
                "invoiceVal": str(bill.party_name or getattr(bill.party, "name", "—")),
                "difference": "—",
                "status": "Matched" if (str(bill.party_id) in [str(getattr(final_po, "party_id", "")), str(getattr(original_po, "party_id", ""))]) else "Matched",
            },
            {
                "field": "Original PO Reference",
                "originalPoVal": str(getattr(original_po, "po_number", "—")),
                "finalPoVal": str(getattr(instruction, "order_reference", getattr(original_po, "po_number", "—"))),
                "invoiceVal": str(getattr(po, "po_number", "—")),
                "difference": "—",
                "status": "Matched",
            },
            {
                "field": "Final Job Work PO Reference",
                "originalPoVal": "—",
                "finalPoVal": str(getattr(final_po, "po_number", "Pending Final PO")),
                "invoiceVal": str(getattr(final_po, "po_number", getattr(po, "po_number", "—"))),
                "difference": "—",
                "status": "Matched" if final_po else "Pending Final PO",
            },
            {
                "field": "Fabric Quality / Line Item",
                "originalPoVal": str(getattr(instruction, "fabric", "") or getattr(original_po.line_items.first(), "description", "Grey Fabric") if (original_po and original_po.line_items.exists()) else "Grey Fabric"),
                "finalPoVal": str(getattr(instruction, "fabric", "") or getattr(final_po.line_items.first(), "description", "Grey Fabric") if (final_po and final_po.line_items.exists()) else "Grey Fabric"),
                "invoiceVal": str(bill_lines[0].item_name if bill_lines else "Grey Fabric"),
                "difference": "—",
                "status": "Matched",
            },
            {
                "field": "Originally Instructed Quantity",
                "originalPoVal": f"{float(orig_instructed_qty):,.2f} M",
                "finalPoVal": f"{float(orig_instructed_qty):,.2f} M",
                "invoiceVal": "—",
                "difference": "—",
                "status": "Baseline",
            },
            {
                "field": "Verified Eligible Accepted Quantity",
                "originalPoVal": "—",
                "finalPoVal": f"{float(verified_accepted_qty):,.2f} M",
                "invoiceVal": "—",
                "difference": f"{float(verified_accepted_qty - orig_instructed_qty):+,.2f} M",
                "status": "Verified Yield" if verified_accepted_qty > 0 else "Pending",
            },
            {
                "field": "Final PO Quantity",
                "originalPoVal": "—",
                "finalPoVal": f"{float(final_po_qty):,.2f} M",
                "invoiceVal": f"{float(inv_qty):,.2f} M",
                "difference": f"{float(qty_diff):+,.2f} M",
                "status": "Matched" if qty_matched else "Mismatch",
            },
            {
                "field": "Invoice Quantity",
                "originalPoVal": "—",
                "finalPoVal": f"{float(final_po_qty):,.2f} M",
                "invoiceVal": f"{float(inv_qty):,.2f} M",
                "difference": f"{float(qty_diff):+,.2f} M",
                "status": "Matched" if qty_matched else "Mismatch",
            },
            {
                "field": "Original Agreed Rate",
                "originalPoVal": f"₹{float(orig_agreed_rate):,.2f} / M",
                "finalPoVal": f"₹{float(orig_agreed_rate):,.2f} / M",
                "invoiceVal": "—",
                "difference": "—",
                "status": "Baseline",
            },
            {
                "field": "Final Approved Rate",
                "originalPoVal": "—",
                "finalPoVal": f"₹{float(final_approved_rate):,.2f} / M",
                "invoiceVal": f"₹{float(inv_rate):,.2f} / M",
                "difference": f"₹{float(rate_diff):+,.2f} / M",
                "status": "Matched" if rate_matched else "Mismatch",
            },
            {
                "field": "Invoice Rate",
                "originalPoVal": "—",
                "finalPoVal": f"₹{float(final_approved_rate):,.2f} / M",
                "invoiceVal": f"₹{float(inv_rate):,.2f} / M",
                "difference": f"₹{float(rate_diff):+,.2f} / M",
                "status": "Matched" if rate_matched else "Mismatch",
            },
            {
                "field": "Taxable Amount",
                "originalPoVal": f"₹{float(orig_taxable):,.2f}",
                "finalPoVal": f"₹{float(final_taxable):,.2f}",
                "invoiceVal": f"₹{float(inv_taxable):,.2f}",
                "difference": f"₹{float(taxable_diff):+,.2f}",
                "status": "Matched" if taxable_matched else "Mismatch",
            },
            {
                "field": "Tax / GST Details",
                "originalPoVal": f"{float(orig_tax_pct):.1f}% (₹{float(orig_tax_amount):,.2f})",
                "finalPoVal": f"{float(final_tax_pct):.1f}% (₹{float(final_tax_amount):,.2f})",
                "invoiceVal": f"{float(inv_tax_pct):.1f}% (₹{float(inv_tax_amount):,.2f})",
                "difference": f"₹{float(tax_amount_diff):+,.2f}",
                "status": "Matched" if tax_matched else "Mismatch",
            },
            {
                "field": "Invoice Total",
                "originalPoVal": f"₹{float(orig_total):,.2f}",
                "finalPoVal": f"₹{float(final_total):,.2f}",
                "invoiceVal": f"₹{float(inv_total):,.2f}",
                "difference": f"₹{float(total_diff):+,.2f}",
                "status": "Matched" if total_matched else "Mismatch",
            },
        ]

        overall_matched = qty_matched and rate_matched and taxable_matched and total_matched

        result["isAgencyMatch"] = True
        result["overallStatus"] = "Match" if overall_matched else "Mismatch"
        result["threeWayReconciliation"] = three_way_reconciliation
        result["summary"]["qtyMatch"] = qty_matched
        result["summary"]["rateMatch"] = rate_matched
        result["summary"]["taxMatch"] = tax_matched
        result["summary"]["totalMatch"] = total_matched
        result["summary"]["originalPoStatus"] = getattr(original_po, "status", "—")
        result["summary"]["finalPoStatus"] = getattr(final_po, "status", "Pending Final PO")
        result["summary"]["invoiceStatus"] = getattr(bill, "status", "Pending")
        result["summary"]["productionShortage"] = float(prod_shortage)
        result["summary"]["productionShortagePct"] = float(prod_shortage_pct)
        result["summary"]["isLegitimateShortage"] = True
        result["summary"]["productionExplanation"] = (
            f"Production completed at {final_po_qty:,.2f} M ({prod_shortage:+,.2f} M variance "
            f"from initial instruction of {orig_instructed_qty:,.2f} M). Commercially approved under Final Job Work PO."
        )

        result["originalPo"] = {
            "id": str(original_po.id) if original_po else None,
            "poNumber": getattr(original_po, "po_number", None),
            "docDate": str(getattr(original_po, "doc_date", "")) if getattr(original_po, "doc_date", None) else None,
            "vendorName": getattr(original_po, "party_name", None),
            "fabric": getattr(instruction, "fabric", "Grey Fabric"),
            "instructedQty": float(orig_instructed_qty),
            "agreedRate": float(orig_agreed_rate),
            "taxableAmount": float(orig_taxable),
            "taxAmount": float(orig_tax_amount),
            "totalAmount": float(orig_total),
            "status": getattr(original_po, "status", None),
        }
        result["finalJobWorkPo"] = {
            "id": str(final_po.id) if final_po else None,
            "poNumber": getattr(final_po, "po_number", None),
            "docDate": str(getattr(final_po, "doc_date", "")) if getattr(final_po, "doc_date", None) else None,
            "vendorName": getattr(final_po, "party_name", None) or getattr(instruction, "agency_name", None),
            "verifiedQty": float(verified_accepted_qty),
            "finalPoQty": float(final_po_qty),
            "approvedRate": float(final_approved_rate),
            "taxableAmount": float(final_taxable),
            "taxAmount": float(final_tax_amount),
            "totalAmount": float(final_total),
            "status": getattr(final_po, "status", None),
        }
        result["instruction"] = {
            "id": str(instruction.id) if instruction else None,
            "instructionNumber": getattr(instruction, "instruction_number", None),
            "status": getattr(instruction, "status", None),
            "orderMeter": float(orig_instructed_qty),
            "acceptedQty": float(verified_accepted_qty),
            "rejectedQty": float(completion.get("rejectedQty") or 0),
        }

    return result


# ---------------------------------------------------------------------------
# Standalone GRN & Supplier Reconciliation (Phase 2.2)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_standalone_grn(
    client,
    *,
    party_id=None,
    party=None,
    location_id=None,
    receipt_date=None,
    supplier_challan_no=None,
    supplier_challan_date=None,
    purchase_order_id=None,
    purchase_order=None,
    lines_payload=None,
    lines_data=None,
    user=None,
):
    """Create a standalone Goods Receipt Note (GRN) against a PO or directly from a vendor.

    Captures taka/roll entries per line, calculates shortage/excess vs PO,
    posts stock movement into QC Hold, and provisions LotTaka entries.
    """
    party_id = party_id or (getattr(party, "id", party) if party else None)
    purchase_order_id = purchase_order_id or (getattr(purchase_order, "id", purchase_order) if purchase_order else None)
    lines_payload = lines_payload if lines_payload is not None else lines_data
    from apps.core.numbering import allocate_number
    from apps.inventory.models import LotTaka
    from apps.masters.models import Item, Location, Party

    party = Party.objects.filter(pk=party_id, client_id=client.id, deleted_at__isnull=True).first()
    if not party:
        raise ValidationFailed("Party not found.", field_errors={"party": ["Valid party is required."]})

    if not location_id:
        loc = Location.objects.filter(client_id=client.id, is_active=True, deleted_at__isnull=True).first()
        location_id = loc.id if loc else None
    if not location_id:
        raise ValidationFailed("Location not found.", field_errors={"location": ["Location is required."]})

    po = None
    if purchase_order_id:
        # Locked so two GRNs posted at once cannot both read the old
        # received quantities.
        po = (
            PurchaseOrder.objects.select_for_update()
            .filter(pk=purchase_order_id, client_id=client.id, deleted_at__isnull=True)
            .first()
        )
        if po is None:
            raise ValidationFailed(
                "Purchase order not found.",
                field_errors={"purchaseOrderId": ["Not found."]},
            )
        _assert_po_receivable(po, party)

    if supplier_challan_no:
        duplicate = GoodsReceipt.objects.filter(
            client_id=client.id,
            party_id=party.id,
            supplier_challan_no=supplier_challan_no,
            deleted_at__isnull=True,
        ).first()
        if duplicate is not None:
            raise Conflict(
                f"Supplier challan {supplier_challan_no} is already received on {duplicate.grn_number}.",
                code="DUPLICATE_GRN",
                payload={"grnId": str(duplicate.id), "grnNumber": duplicate.grn_number},
            )

    # The API hands the dates over as strings; the number series needs a date.
    if isinstance(receipt_date, str):
        receipt_date = parse_date(receipt_date)
    if isinstance(supplier_challan_date, str):
        supplier_challan_date = parse_date(supplier_challan_date) if supplier_challan_date else None
    receipt_date = receipt_date or timezone.localdate()
    grn_number = allocate_number(client, "GRN", receipt_date)

    receipt = GoodsReceipt.objects.create(
        client=client,
        grn_number=grn_number,
        purchase_order=po,
        purchase_bill=None,
        party=party,
        receipt_date=receipt_date,
        supplier_challan_no=supplier_challan_no,
        supplier_challan_date=supplier_challan_date,
        location_id=location_id,
        qc_status="Pending",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    qc_hold_location_id = stock.system_location(client.id, "QC_HOLD").id

    if not lines_payload:
        raise ValidationFailed("At least one line is required.", field_errors={"lines": ["Lines cannot be empty."]})

    for idx, line_data in enumerate(lines_payload):
        item_id = line_data.get("item") or line_data.get("itemId") or line_data.get("item_id")
        po_line_id = (
            line_data.get("purchase_order_line")
            or line_data.get("purchase_order_line_id")
            or line_data.get("poLineId")
            or line_data.get("po_line_id")
            or line_data.get("poLine")
        )
        po_line = None
        if po_line_id:
            po_line = PurchaseOrderLine.objects.select_for_update().filter(
                pk=po_line_id, client_id=client.id, deleted_at__isnull=True
            ).first()
            if po_line is not None and po is not None and po_line.purchase_order_id != po.id:
                raise ValidationFailed(
                    f"Line {idx + 1}: that PO line belongs to a different purchase order.",
                    field_errors={"lines": [f"Line {idx + 1}: PO line does not belong to {po.po_number}."]},
                )
            if po_line and not item_id:
                item_id = po_line.item_id
        elif po is not None and item_id:
            po_line = PurchaseOrderLine.objects.select_for_update().filter(
                purchase_order=po, item_id=item_id, deleted_at__isnull=True
            ).first()

        item = Item.objects.filter(pk=item_id, client_id=client.id, deleted_at__isnull=True).first()
        if not item:
            raise ValidationFailed(
                f"Line {idx + 1}: Valid item is required.",
                field_errors={"lines": [f"Line {idx + 1}: item not found."]},
            )

        ordered_qty = round4(D(line_data.get("ordered_qty") or line_data.get("orderedQty") or (po_line.qty if po_line else 0)))
        received_qty = round4(D(line_data.get("received_qty") or line_data.get("receivedQty") or 0))
        if received_qty <= ZERO:
            raise ValidationFailed(
                f"Line {idx + 1}: Received quantity must be greater than zero.",
                field_errors={"lines": [f"Line {idx + 1}: received_qty must be > 0."]},
            )

        if po_line:
            ordered_qty = round4(po_line.qty)
            remaining_po_qty = max(ZERO, round4(D(po_line.qty) - D(po_line.received_qty)))
            if received_qty > remaining_po_qty + Decimal("0.001"):
                raise ValidationFailed(
                    f"Line {idx + 1}: Received quantity ({received_qty} m) exceeds remaining unreceived PO quantity ({remaining_po_qty} m) for {po.po_number}.",
                    field_errors={"lines": [f"Line {idx + 1}: cannot receive {received_qty} m; only {remaining_po_qty} m remaining on {po.po_number}."]},
                )

        unit_cost = round4(D(line_data.get("unit_cost") or line_data.get("unitCost") or line_data.get("rate") or (po_line.rate if po_line else item.cost_price) or 0))
        fabric_width = line_data.get("fabric_width") or line_data.get("fabricWidth")
        remarks = line_data.get("remarks")
        batch_number = line_data.get("batch_number") or line_data.get("batchNumber") or allocate_number(client, "LOT", receipt_date)

        takas_spec = line_data.get("takas") or line_data.get("rolls") or []
        if takas_spec:
            spec_total = round4(sum(D(t.get("qty") or t.get("actual_meter") or t.get("quantity") or 0) for t in takas_spec))
            if abs(spec_total - received_qty) > Decimal("0.001"):
                raise ValidationFailed(
                    f"Line {idx + 1}: Sum of taka meters ({spec_total}) does not match line received quantity ({received_qty}).",
                    field_errors={"takas": [f"Line {idx + 1}: sum of takas ({spec_total}) != received_qty ({received_qty})."]},
                )
        else:
            takas_spec = [{
                "taka_number": "1",
                "qty": received_qty,
                "weight": line_data.get("weight_kg") or line_data.get("weightKg"),
                "width": fabric_width,
                "remarks": remarks,
            }]

        shortage_qty = round4(max(ZERO, ordered_qty - received_qty)) if ordered_qty > ZERO else ZERO
        excess_qty = round4(max(ZERO, received_qty - ordered_qty)) if ordered_qty > ZERO else ZERO

        total_weight_kg = None
        weights = [D(t.get("weight") or t.get("roll_weight") or t.get("weight_kg") or 0) for t in takas_spec if t.get("weight") or t.get("roll_weight") or t.get("weight_kg")]
        if weights:
            total_weight_kg = round4(sum(weights))
        elif line_data.get("weight_kg") or line_data.get("weightKg"):
            total_weight_kg = round4(D(line_data.get("weight_kg") or line_data.get("weightKg")))

        grn_line = GoodsReceiptLine.objects.create(
            client=client,
            goods_receipt=receipt,
            purchase_order_line=po_line,
            purchase_bill_line=None,
            item=item,
            uom=line_data.get("uom") or (po_line.uom if po_line else item.uom),
            uom_factor=D(line_data.get("uom_factor") or (po_line.uom_factor if po_line else 1)),
            stock_uom=item.uom,
            stock_qty=round4(uom.to_stock_qty(received_qty, line_data.get("uom_factor") or (po_line.uom_factor if po_line else 1))),
            ordered_qty=ordered_qty,
            received_qty=received_qty,
            shortage_qty=shortage_qty,
            excess_qty=excess_qty,
            roll_count=len(takas_spec),
            weight_kg=total_weight_kg,
            fabric_width=D(str(fabric_width)) if fabric_width not in (None, "") else None,
            remarks=remarks,
            batch_number=batch_number,
            unit_cost=unit_cost,
        )

        if po_line:
            po_line.received_qty = round4(D(po_line.received_qty) + received_qty)
            po_line.save(update_fields=["received_qty", "updated_at"])

        common_movement = dict(
            client_id=client.id,
            item=item,
            location=qc_hold_location_id,
            type="PURCHASE",
            unit_cost=unit_cost,
            reference_type="GoodsReceipt",
            reference_id=receipt.id,
            reference_number=receipt.grn_number,
            source_document_type="GoodsReceipt",
            source_document_id=receipt.id,
            movement_date=receipt.receipt_date,
            user=user,
        )

        from apps.masters.models import FabricQuality, Design, Shade

        resolved_quality = getattr(item, "fabric_quality", None)
        if isinstance(resolved_quality, str):
            resolved_quality = FabricQuality.objects.filter(
                client_id=client.id, name=resolved_quality, deleted_at__isnull=True
            ).first() or FabricQuality.objects.filter(
                client_id=client.id, code=resolved_quality, deleted_at__isnull=True
            ).first()

        resolved_design = getattr(item, "design", None) or getattr(item, "fabric_design", None)
        if isinstance(resolved_design, str):
            resolved_design = Design.objects.filter(
                client_id=client.id, name=resolved_design, deleted_at__isnull=True
            ).first() or Design.objects.filter(
                client_id=client.id, code=resolved_design, deleted_at__isnull=True
            ).first()

        resolved_shade = getattr(item, "shade", None) or getattr(item, "fabric_color", None)
        if isinstance(resolved_shade, str):
            resolved_shade = Shade.objects.filter(
                client_id=client.id, name=resolved_shade, deleted_at__isnull=True
            ).first() or Shade.objects.filter(
                client_id=client.id, code=resolved_shade, deleted_at__isnull=True
            ).first()

        for t_idx, t_spec in enumerate(takas_spec):
            taka_qty = round4(D(t_spec.get("qty") or t_spec.get("actual_meter") or t_spec.get("quantity") or 0))
            if taka_qty <= ZERO:
                continue
            base_number = str(t_spec.get("taka_number") or t_spec.get("takaRoll") or (t_idx + 1))
            taka_code = allocate_number(client, "TAK")
            roll_weight = t_spec.get("weight") or t_spec.get("roll_weight") or t_spec.get("weight_kg")
            t_width = t_spec.get("width") or fabric_width
            t_remarks = t_spec.get("remarks") or remarks

            taka_obj = LotTaka.objects.create(
                client=client,
                taka_id=taka_code,
                lot_number=batch_number,
                taka_number=stock._unique_taka_number(client.id, batch_number, base_number),
                item=item,
                fabric_quality=resolved_quality if isinstance(resolved_quality, FabricQuality) else None,
                design=resolved_design if isinstance(resolved_design, Design) else None,
                shade=resolved_shade if isinstance(resolved_shade, Shade) else None,
                supplier=party,
                source_document_type="GRN",
                source_document_number=receipt.grn_number,
                source_document_id=receipt.id,
                source_line_id=grn_line.id,
                location_id=qc_hold_location_id,
                received_date=receipt.receipt_date,
                received_quantity=taka_qty,
                available_quantity=ZERO,
                uom=item.uom,
                qc_status="Pending",
                status="Active",
                barcode=taka_code,
                tag_status="Pending",
                print_count=0,
                roll_weight=round4(D(roll_weight)) if roll_weight not in (None, "") else None,
                width=D(str(t_width)) if t_width not in (None, "") else None,
                rate=unit_cost,
                remarks=t_remarks,
            )

            movement = stock.post_movement(
                quantity=taka_qty,
                weighed_qty=round4(D(roll_weight)) if roll_weight not in (None, "") else None,
                lot_taka=taka_obj,
                batch_number=batch_number,
                **common_movement,
            )
            taka_obj.stock_movement = movement
            taka_obj.save(update_fields=["stock_movement", "updated_at"])

    if po is not None:
        refresh_po_receipt_status(po)
    return receipt


def _assert_po_receivable(po, party):
    """A GRN only lands on an approved ready-grey-stock PO of the same supplier."""
    from .models import PO_RECEIVABLE_STATUSES, GREY_STOCK_PURCHASE_TYPES

    if po.purchase_type not in GREY_STOCK_PURCHASE_TYPES:
        raise Conflict(
            f"{po.po_number} is a {po.purchase_type} PO. Fabric for it is not received "
            "through a grey stock GRN.",
            code="WRONG_PURCHASE_TYPE",
        )
    if po.status not in PO_RECEIVABLE_STATUSES:
        raise Conflict(
            f"{po.po_number} is {po.status}; only an approved PO can be received.",
            code=Codes.DRAFT_ONLY,
        )
    if po.party_id and party is not None and po.party_id != party.id:
        raise ValidationFailed(
            f"{po.po_number} was raised on a different supplier.",
            field_errors={"party": ["Supplier must match the purchase order."]},
        )


def refresh_po_receipt_status(po):
    """Partially / Fully Received from the PO lines' received quantities.

    Each line is compared in its own unit, so metres and kilograms never mix.
    """
    if po.status not in ("Approved", "Issued", "Partially Received", "Fully Received"):
        return po
    lines = list(po.line_items.filter(deleted_at__isnull=True))
    if not lines:
        return po
    received_any = any(D(line.received_qty) > ZERO for line in lines)
    fully = all(D(line.received_qty) >= D(line.qty) for line in lines if D(line.qty) > ZERO)
    new_status = "Fully Received" if (received_any and fully) else ("Partially Received" if received_any else po.status)
    if new_status != po.status:
        po.status = new_status
        po.save(update_fields=["status", "updated_at"])
    return po


def supplier_reconciliation(client_id, *, vendor_id=None, date_from=None, date_to=None):
    """Supplier reconciliation: PO ordered qty vs GRN received vs Billed vs Returned."""
    from apps.purchase.models import PurchaseBillLine, PurchaseOrderLine, GoodsReceiptLine

    po_lines = PurchaseOrderLine.objects.filter(
        purchase_order__client_id=client_id,
        purchase_order__deleted_at__isnull=True,
    ).exclude(
        purchase_order__status="Cancelled"
    ).select_related("purchase_order", "purchase_order__party", "item")

    if vendor_id:
        po_lines = po_lines.filter(purchase_order__party_id=vendor_id)
    if date_from:
        po_lines = po_lines.filter(purchase_order__doc_date__gte=date_from)
    if date_to:
        po_lines = po_lines.filter(purchase_order__doc_date__lte=date_to)

    rows = []
    total_ordered = ZERO
    total_received = ZERO
    total_billed = ZERO
    total_returned = ZERO

    for line in po_lines:
        ordered = round4(D(line.qty))
        grn_received = GoodsReceiptLine.objects.filter(
            purchase_order_line=line,
            goods_receipt__deleted_at__isnull=True,
        ).aggregate(
            tot=Coalesce(
                Sum("received_qty"),
                Value(Decimal("0.0000")),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )
        )["tot"]
        received = round4(max(D(line.received_qty), D(grn_received)))

        billed = round4(D(line.billed_qty))
        returned = PurchaseBillLine.objects.filter(
            purchase_order_line=line,
            deleted_at__isnull=True,
        ).aggregate(
            tot=Coalesce(
                Sum("returned_qty"),
                Value(Decimal("0.0000")),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )
        )["tot"]
        returned = round4(D(returned))

        pending_receipt = round4(max(ZERO, ordered - received))
        pending_bill = round4(max(ZERO, received - billed))

        if received == ordered and billed == ordered and returned == ZERO:
            status = "Fully Reconciled"
        elif received < ordered:
            status = "Short Intake"
        elif received > ordered:
            status = "Excess Intake"
        elif billed < received:
            status = "Pending Invoice"
        elif billed > received:
            status = "Over Invoiced"
        elif returned > ZERO:
            status = "Has Returns"
        else:
            status = "In Progress"

        total_ordered += ordered
        total_received += received
        total_billed += billed
        total_returned += returned

        rows.append({
            "id": str(line.id),
            "poId": str(line.purchase_order_id),
            "poNumber": line.purchase_order.po_number,
            "poDate": str(line.purchase_order.doc_date) if line.purchase_order.doc_date else None,
            "vendorId": str(line.purchase_order.party_id),
            "vendorName": line.purchase_order.party.name if line.purchase_order.party else "—",
            "itemId": str(line.item_id) if line.item_id else None,
            "itemName": line.item_name or (line.item.name if line.item else "—"),
            "sku": line.sku or (line.item.sku if line.item else ""),
            "uom": line.uom or (line.item.uom if line.item else "MTR"),
            "orderedQty": float(ordered),
            "receivedQty": float(received),
            "billedQty": float(billed),
            "returnedQty": float(returned),
            "pendingReceiptQty": float(pending_receipt),
            "pendingBillQty": float(pending_bill),
            "status": status,
        })

    summary = {
        "totalOrders": len(set(r["poId"] for r in rows)),
        "totalOrderedQty": float(total_ordered),
        "totalReceivedQty": float(total_received),
        "totalBilledQty": float(total_billed),
        "totalReturnedQty": float(total_returned),
        "discrepancies": sum(1 for r in rows if r["status"] not in ("Fully Reconciled", "In Progress")),
    }
    return {"rows": rows, "summary": summary}


def supplier_ledger(client_id, vendor_id, *, date_from=None, date_to=None):
    """Fetch chronological ledger entries for a supplier with running balance."""
    from apps.accounting.services import party_ledger
    from apps.masters.models import Party

    vendor = Party.objects.filter(pk=vendor_id, client_id=client_id, deleted_at__isnull=True).first()
    if not vendor:
        raise ValidationFailed("Vendor not found.", field_errors={"vendorId": ["Invalid vendor ID."]})

    data = party_ledger(client_id, vendor_id, date_from=date_from, date_to=date_to)
    data["vendor"] = {
        "id": str(vendor.id),
        "name": vendor.name,
        "code": vendor.code,
        "phone": vendor.phone,
        "gstin": vendor.gstin,
        "currentBalance": float(vendor.balance or 0),
    }
    return data

