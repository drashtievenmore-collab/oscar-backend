"""Merge several lots into one processing batch (PDF §06 "Lot split / merge").

Multiple small lots of the same item, in the same godown, become one new lot.
Stock moves by document (LOT_MERGE_OUT from each source, LOT_MERGE_IN into
the merged taka), the merged taka keeps every source in ``parent_lots`` so a
finished lot traces back to all of them, and the sources' value is carried
onto the merged lot's cost ledger.
"""
import uuid

from django.db import transaction

from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number

MERGEABLE_STATUSES = ("Available", "Active", "Finished")


@transaction.atomic
def merge_lots(*, client_id, lot_taka_ids, lot_number=None, remarks=None, user=None):
    from apps.inventory import costing_services
    from apps.inventory import services as stock
    from apps.inventory.models import LotTaka

    ids = list(dict.fromkeys(str(i) for i in lot_taka_ids or []))
    if len(ids) < 2:
        raise ValidationFailed(
            "Choose at least two lots / takas to merge.",
            field_errors={"lotTakaIds": ["At least two."]},
        )
    sources = list(
        LotTaka.objects.select_for_update()
        .filter(client_id=client_id, id__in=ids, deleted_at__isnull=True)
        .select_related("item", "location")
        .order_by("lot_number", "taka_number")
    )
    if len(sources) != len(ids):
        raise ValidationFailed("One of those lots / takas does not exist.", field_errors={"lotTakaIds": ["Not found."]})

    first = sources[0]
    problems = []
    if any(s.item_id != first.item_id for s in sources):
        problems.append("All lots must be the same item / quality.")
    if any(s.location_id != first.location_id for s in sources):
        problems.append("All lots must be in the same godown; transfer them first.")
    if first.location and first.location.code in stock.BLOCKED_LOCATION_CODES:
        problems.append(f"{first.location.name} is not a good-stock location.")
    for s in sources:
        if s.status not in MERGEABLE_STATUSES:
            problems.append(f"Taka {s.taka_id or s.taka_number} is {s.status}; only available stock can be merged.")
        elif D(s.available_quantity) <= ZERO:
            problems.append(f"Taka {s.taka_id or s.taka_number} has nothing available.")
    if problems:
        raise BusinessRuleViolation(" ".join(problems), code="LOT_MERGE_NOT_ALLOWED", payload={"problems": problems})
    if lot_number and LotTaka.objects.filter(
        client_id=client_id, lot_number__iexact=lot_number, deleted_at__isnull=True
    ).exists():
        raise ValidationFailed(
            f"Lot {lot_number} already exists; give the merged batch a new lot number.",
            field_errors={"lotNumber": ["Already used."]},
        )

    merge_ref = allocate_number(client_id, "MRG")
    reference_id = uuid.uuid4()
    total_qty = sum((D(s.available_quantity) for s in sources), ZERO)
    total_value = sum((D(s.available_quantity) * D(s.rate or 0) for s in sources), ZERO)
    rate = round4(total_value / total_qty) if total_qty > ZERO else ZERO

    taka_code = allocate_number(client_id, "TAK")
    merged = LotTaka.objects.create(
        client_id=client_id,
        taka_id=taka_code,
        barcode=taka_code,
        lot_number=lot_number or merge_ref,
        taka_number="1",
        item_id=first.item_id,
        fabric_quality_id=first.fabric_quality_id,
        design_id=first.design_id,
        shade_id=first.shade_id,
        supplier_id=first.supplier_id if all(s.supplier_id == first.supplier_id for s in sources) else None,
        source_document_type="LotMerge",
        source_document_id=reference_id,
        source_document_number=merge_ref,
        location_id=first.location_id,
        received_date=min((s.received_date for s in sources if s.received_date), default=None) or first.received_date,
        received_quantity=round4(total_qty),
        available_quantity=ZERO,
        uom=first.uom,
        qc_status=first.qc_status,
        status=first.status if all(s.status == first.status for s in sources) else "Available",
        tag_status="Pending",
        width=first.width,
        gsm=first.gsm,
        rate=rate,
        remarks=remarks or f"Merged from {', '.join(sorted({s.lot_number for s in sources}))}",
    )
    merged.parent_lots.add(*sources)

    common = dict(
        client_id=client_id,
        item=first.item_id,
        location=first.location_id,
        reference_type="LotMerge",
        reference_id=reference_id,
        reference_number=merge_ref,
        user=user,
    )
    for s in sources:
        stock.post_movement(
            type="LOT_MERGE_OUT", quantity=-round4(D(s.available_quantity)), lot_taka=s,
            unit_cost=s.rate, notes=f"Merged into {merged.lot_number}", **common,
        )
    in_movement = stock.post_movement(
        type="LOT_MERGE_IN", quantity=round4(total_qty), lot_taka=merged, unit_cost=rate,
        notes=f"Merged from {len(sources)} takas", **common,
    )
    merged.stock_movement = in_movement
    merged.save(update_fields=["stock_movement", "updated_at"])

    # The merged lot carries the value of what went into it (PDF §09).
    if total_value > ZERO:
        costing_services.post_lot_cost(
            client_id=client_id,
            lot_number=merged.lot_number,
            cost_type="Grey purchase",
            amount=round2(total_value),
            quantity=round4(total_qty),
            source_document_type="LotMerge",
            source_document_id=reference_id,
            source_document_number=merge_ref,
            lot_taka=merged,
            user=user,
            is_approved=True,
            remarks=f"Carried from merged lots {', '.join(sorted({s.lot_number for s in sources}))}",
        )
    merged.refresh_from_db()
    return merged, sources
