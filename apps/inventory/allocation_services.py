"""General grey stock -> project allocation (reservation, not a stock move).

Accepted grey fabric from a purchase without a project sits in general
warehouse stock. An authorised user later earmarks metres of a lot/taka for
a project. The fabric does not move and the movement ledger is untouched;
the allocation row is the record, and unallocated stock is
``available_quantity - active allocations``.

Every allocation locks its lot row first, so concurrent or repeated requests
can never allocate the same metres twice.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.audit import record_audit, snapshot
from apps.core.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
from apps.core.money import D, round4

from .models import GreyStockAllocation, LotTaka
from .services import BLOCKED_LOCATION_CODES

ZERO = Decimal("0")

#: A lot can be allocated only once QC has passed it into a godown.
ALLOCATABLE_QC_STATUSES = ("Passed", "Pass")
NON_ALLOCATABLE_STATUSES = (
    LotTaka.STATUS_EXTERNAL_WIP, LotTaka.STATUS_QC_HOLD, LotTaka.STATUS_REPROCESS,
    LotTaka.STATUS_SECOND_QUALITY, LotTaka.STATUS_REJECT, LotTaka.STATUS_SAMPLE,
    LotTaka.STATUS_DISPATCHED,
)


def allocated_quantities(client_id, lot_ids):
    """``{lot_id: active allocated qty}`` for the given lots."""
    rows = (
        GreyStockAllocation.objects.filter(
            client_id=client_id,
            lot_taka_id__in=list(lot_ids),
            status=GreyStockAllocation.STATUS_ACTIVE,
            deleted_at__isnull=True,
        )
        .values("lot_taka_id")
        .annotate(
            qty=Coalesce(
                Sum("quantity"), Value(ZERO),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )
        )
    )
    return {row["lot_taka_id"]: D(row["qty"]) for row in rows}


def allocatable_lots(client_id, *, item_id=None, fabric_quality_id=None):
    """Lots whose QC-passed stock can be allocated, with unallocated qty."""
    qs = (
        LotTaka.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            available_quantity__gt=0,
            qc_status__in=ALLOCATABLE_QC_STATUSES,
        )
        .exclude(status__in=NON_ALLOCATABLE_STATUSES)
        .exclude(location__code__in=BLOCKED_LOCATION_CODES)
        .select_related("item", "location", "fabric_quality", "supplier")
        .order_by("received_date", "lot_number", "taka_number")
    )
    if item_id:
        qs = qs.filter(item_id=item_id)
    if fabric_quality_id:
        qs = qs.filter(fabric_quality_id=fabric_quality_id)
    lots = list(qs)
    allocated = allocated_quantities(client_id, [lot.id for lot in lots])
    result = []
    for lot in lots:
        taken = allocated.get(lot.id, ZERO)
        result.append((lot, taken, round4(max(ZERO, D(lot.available_quantity) - taken))))
    return result


def fabric_stock_summary(client_id, *, project=None, item_id=None, fabric_quality_id=None):
    """Fabric-wise summary and drill-down takas for Available Grey Stock.

    Groups grey fabric stock by fabric identity (item, quality, design, shade).
    Computes reconciled physical, QC-passed, QC-hold, rejected, reserved,
    usable, project-allocated, and balance quantities. Excludes QC-hold and
    rejected stock from usable allocations and avoids double-counting reservations.
    """
    qs = (
        LotTaka.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            available_quantity__gt=0,
        )
        .select_related("item", "location", "fabric_quality", "design", "shade", "supplier")
        .order_by("item__name", "received_date", "lot_number", "taka_number")
    )
    if item_id:
        qs = qs.filter(item_id=item_id)
    if fabric_quality_id:
        qs = qs.filter(fabric_quality_id=fabric_quality_id)
    lots = list(qs)

    mine_by_lot = {}
    customer_by_lot = {}
    own_by_lot = {}
    if project:
        active_allocations = project_allocations(client_id, project)
        for a in active_allocations:
            mine_by_lot[a.lot_taka_id] = mine_by_lot.get(a.lot_taka_id, ZERO) + a.quantity
            alloc_type = getattr(a, "allocation_type", GreyStockAllocation.ALLOCATION_CUSTOMER) or GreyStockAllocation.ALLOCATION_CUSTOMER
            if alloc_type == GreyStockAllocation.ALLOCATION_OWN:
                own_by_lot[a.lot_taka_id] = own_by_lot.get(a.lot_taka_id, ZERO) + a.quantity
            else:
                customer_by_lot[a.lot_taka_id] = customer_by_lot.get(a.lot_taka_id, ZERO) + a.quantity

        existing_lot_ids = {lot.id for lot in lots}
        missing_ids = [a.lot_taka_id for a in active_allocations if a.lot_taka_id not in existing_lot_ids]
        if missing_ids:
            extra_lots = list(
                LotTaka.objects.filter(client_id=client_id, id__in=missing_ids)
                .select_related("item", "location", "fabric_quality", "design", "shade", "supplier")
            )
            lots.extend(extra_lots)

    allocated_map = allocated_quantities(client_id, [lot.id for lot in lots])
    fabrics_map = {}
    lots_data = []

    for lot in lots:
        quality = (
            (lot.fabric_quality.name if lot.fabric_quality else None)
            or (lot.item.fabric_quality if lot.item else None)
            or "Standard"
        )
        design = (
            (lot.design.name if lot.design else None)
            or (lot.item.fabric_design if lot.item else None)
            or "—"
        )
        shade = (
            (lot.shade.name if lot.shade else None)
            or (lot.item.fabric_color if lot.item else None)
            or "—"
        )
        uom = lot.uom or (lot.item.uom if lot.item else "Meters")
        fabric_name = lot.item.name if lot.item else "Fabric"
        fabric_key = f"{lot.item_id}_{quality}_{design}_{shade}"

        is_passed = (
            (lot.qc_status in ALLOCATABLE_QC_STATUSES)
            and (lot.status not in NON_ALLOCATABLE_STATUSES)
            and (not (lot.location and lot.location.code in BLOCKED_LOCATION_CODES))
        )
        is_rejected = (
            (lot.qc_status in ("Rejected", "Reject"))
            or (lot.status == LotTaka.STATUS_REJECT)
        )
        is_hold = (not is_passed) and (not is_rejected)
        is_allocatable = is_passed

        physical = D(lot.available_quantity)
        total_taken = allocated_map.get(lot.id, ZERO)
        mine = mine_by_lot.get(lot.id, ZERO)
        reserved_others = max(ZERO, total_taken - mine)

        if is_allocatable:
            usable = round4(max(ZERO, physical - reserved_others))
            balance = round4(max(ZERO, usable - mine))
        else:
            usable = ZERO
            balance = ZERO

        if mine > ZERO:
            status_label = "Selected"
        elif not is_allocatable:
            status_label = "QC Hold" if is_hold else ("Rejected" if is_rejected else "Unavailable")
        else:
            status_label = "Available"

        c_alloc = customer_by_lot.get(lot.id, ZERO)
        o_alloc = own_by_lot.get(lot.id, ZERO)
        alloc_destination = "Customer Project"
        if o_alloc > ZERO and c_alloc > ZERO:
            alloc_destination = "Mixed"
        elif o_alloc > ZERO:
            alloc_destination = "Own Project"

        lot_dict = {
            "id": str(lot.id),
            "lotTakaId": str(lot.id),
            "fabricKey": fabric_key,
            "fabricQuality": quality,
            "fabricName": fabric_name,
            "design": design,
            "shade": shade,
            "takaId": lot.taka_id,
            "lotNumber": lot.lot_number,
            "takaNumber": lot.taka_number,
            "location": lot.location.name if lot.location else None,
            "uom": uom,
            "totalStock": float(physical),
            "reserved": float(reserved_others),
            "usable": float(usable),
            "allocateQty": float(mine),
            "customerAllocateQty": float(c_alloc),
            "ownAllocateQty": float(o_alloc),
            "allocationType": alloc_destination,
            "weightKg": float(lot.roll_weight) if lot.roll_weight else None,
            "sourcePoNumber": lot.source_document_number or None,
            "balance": float(balance),
            "status": status_label,
            "qcStatus": lot.qc_status or ("Passed" if is_passed else "Pending"),
            "isAllocatable": is_allocatable,
            "supplier": lot.supplier.name if lot.supplier else None,
            "receivedDate": str(lot.received_date) if lot.received_date else None,
        }
        lots_data.append(lot_dict)

        if fabric_key not in fabrics_map:
            fabrics_map[fabric_key] = {
                "fabricKey": fabric_key,
                "itemId": str(lot.item_id),
                "fabricName": fabric_name,
                "fabricQuality": quality,
                "design": design,
                "shade": shade,
                "uom": uom,
                "_physical": ZERO,
                "_passed": ZERO,
                "_hold": ZERO,
                "_rejected": ZERO,
                "_reserved": ZERO,
                "_usable": ZERO,
                "_allocated": ZERO,
                "_customer_allocated": ZERO,
                "_own_allocated": ZERO,
                "_balance": ZERO,
                "takasCount": 0,
                "_lot_numbers": set(),
            }

        fg = fabrics_map[fabric_key]
        fg["_physical"] += physical
        fg["takasCount"] += 1
        fg["_lot_numbers"].add(lot.lot_number)

        if is_passed:
            fg["_passed"] += physical
            fg["_reserved"] += reserved_others
            fg["_usable"] += usable
            fg["_allocated"] += mine
            fg["_customer_allocated"] += c_alloc
            fg["_own_allocated"] += o_alloc
            fg["_balance"] += balance
        elif is_rejected:
            fg["_rejected"] += physical
        else:
            fg["_hold"] += physical

    fabrics_list = []
    for fg in fabrics_map.values():
        fabrics_list.append({
            "fabricKey": fg["fabricKey"],
            "itemId": fg["itemId"],
            "fabricName": fg["fabricName"],
            "fabricQuality": fg["fabricQuality"],
            "design": fg["design"],
            "shade": fg["shade"],
            "uom": fg["uom"],
            "physicalStock": float(fg["_physical"]),
            "qcPassedStock": float(fg["_passed"]),
            "qcHoldStock": float(fg["_hold"]),
            "rejectedStock": float(fg["_rejected"]),
            "reservedStock": float(fg["_reserved"]),
            "usableStock": float(fg["_usable"]),
            "allocatedToProject": float(fg["_allocated"]),
            "customerAllocated": float(fg["_customer_allocated"]),
            "ownAllocated": float(fg["_own_allocated"]),
            "balanceStock": float(fg["_balance"]),
            "takasCount": fg["takasCount"],
            "lotsCount": len(fg["_lot_numbers"]),
        })

    return fabrics_list, lots_data


def _assert_allocatable(lot):
    if lot.qc_status not in ALLOCATABLE_QC_STATUSES:
        raise BusinessRuleViolation(
            f"Taka {lot.taka_id or lot.taka_number} is not QC approved "
            f"(QC status: {lot.qc_status}); only QC-passed stock can be allocated.",
            code="LOT_NOT_QC_PASSED",
        )
    if lot.status in NON_ALLOCATABLE_STATUSES:
        raise BusinessRuleViolation(
            f"Taka {lot.taka_id or lot.taka_number} is in {lot.status} and cannot be allocated.",
            code="LOT_NOT_ALLOCATABLE",
        )
    if lot.location and lot.location.code in BLOCKED_LOCATION_CODES:
        raise BusinessRuleViolation(
            f"Taka {lot.taka_id or lot.taka_number} is at {lot.location.name} and cannot be allocated.",
            code="LOT_NOT_ALLOCATABLE",
        )


@transaction.atomic
def allocate_to_project(*, client_id, project, lot_taka_id, quantity, allocation_type=GreyStockAllocation.ALLOCATION_CUSTOMER, user=None, remarks=None):
    quantity = round4(D(quantity))
    if quantity <= ZERO:
        raise ValidationFailed(
            "Allocation quantity must be greater than zero.",
            field_errors={"quantity": ["Must be greater than zero."]},
        )
    lot = (
        LotTaka.objects.select_for_update()
        .select_related("location")
        .filter(client_id=client_id, pk=lot_taka_id, deleted_at__isnull=True)
        .first()
    )
    if lot is None:
        raise NotFound("That lot/taka does not exist.")
    _assert_allocatable(lot)

    already = allocated_quantities(client_id, [lot.id]).get(lot.id, ZERO)
    unallocated = round4(max(ZERO, D(lot.available_quantity) - already))
    if quantity > unallocated:
        raise Conflict(
            f"Taka {lot.taka_id or lot.taka_number} has only {unallocated} {lot.uom or ''} "
            f"unallocated; {quantity} was requested.".replace("  ", " "),
            code="INSUFFICIENT_STOCK",
            payload={"lotTakaId": str(lot.id), "unallocated": str(unallocated)},
        )

    allocation = GreyStockAllocation.objects.create(
        client_id=client_id,
        project=project,
        lot_taka=lot,
        item_id=lot.item_id,
        quantity=quantity,
        allocation_type=allocation_type or GreyStockAllocation.ALLOCATION_CUSTOMER,
        uom=lot.uom,
        remarks=remarks or None,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    if quantity == unallocated and lot.status in (LotTaka.STATUS_AVAILABLE, "Active"):
        lot.status = LotTaka.STATUS_RESERVED
        lot.save(update_fields=["status", "updated_at"])

    record_audit(
        client=client_id,
        actor=user,
        action="allocate",
        entity_type="GreyStockAllocation",
        entity_id=allocation.id,
        entity_label=f"{lot.lot_number}/{lot.taka_number} -> {project.code}",
        description=f"{quantity} {lot.uom or ''} of {lot.taka_id or lot.lot_number} allocated [{allocation.allocation_type}] to {project.code}",
        after=snapshot(allocation),
    )
    return allocation


@transaction.atomic
def release_allocation(*, client_id, allocation_id, user=None, remarks=None):
    allocation = (
        GreyStockAllocation.objects.select_for_update()
        .select_related("lot_taka", "project")
        .filter(client_id=client_id, pk=allocation_id, deleted_at__isnull=True)
        .first()
    )
    if allocation is None:
        raise NotFound("That allocation does not exist.")
    if allocation.status != GreyStockAllocation.STATUS_ACTIVE:
        raise Conflict("This allocation is already released.", code="ALREADY_DONE")

    before = snapshot(allocation)
    allocation.status = GreyStockAllocation.STATUS_RELEASED
    allocation.released_at = timezone.now()
    allocation.released_by = user if getattr(user, "is_authenticated", False) else None
    if remarks:
        allocation.remarks = remarks
    allocation.save(update_fields=["status", "released_at", "released_by", "remarks", "updated_at"])

    lot = LotTaka.objects.select_for_update().get(pk=allocation.lot_taka_id)
    if lot.status == LotTaka.STATUS_RESERVED:
        lot.status = LotTaka.STATUS_AVAILABLE
        lot.save(update_fields=["status", "updated_at"])

    record_audit(
        client=client_id,
        actor=user,
        action="release",
        entity_type="GreyStockAllocation",
        entity_id=allocation.id,
        entity_label=f"{lot.lot_number}/{lot.taka_number} -> {allocation.project.code}",
        description=remarks or "Allocation released",
        before=before,
        after=snapshot(allocation),
    )
    return allocation


def serialize_allocation(allocation):
    lot = allocation.lot_taka
    return {
        "id": str(allocation.id),
        "projectId": str(allocation.project_id),
        "projectCode": getattr(allocation.project, "code", None),
        "lotTakaId": str(allocation.lot_taka_id),
        "takaId": lot.taka_id,
        "lotNumber": lot.lot_number,
        "takaNumber": lot.taka_number,
        "itemId": str(allocation.item_id),
        "itemName": getattr(lot.item, "name", None),
        "quantity": float(allocation.quantity),
        "allocationType": getattr(allocation, "allocation_type", GreyStockAllocation.ALLOCATION_CUSTOMER),
        "uom": allocation.uom,
        "status": allocation.status,
        "remarks": allocation.remarks,
        "allocatedAt": allocation.created_at.isoformat() if allocation.created_at else None,
        "releasedAt": allocation.released_at.isoformat() if allocation.released_at else None,
    }


def serialize_lot(lot, allocated, unallocated):
    return {
        "id": str(lot.id),
        "takaId": lot.taka_id,
        "lotNumber": lot.lot_number,
        "takaNumber": lot.taka_number,
        "itemId": str(lot.item_id),
        "itemName": getattr(lot.item, "name", None),
        "fabricQuality": getattr(lot.fabric_quality, "name", None),
        "location": getattr(lot.location, "name", None),
        "supplier": getattr(lot.supplier, "name", None),
        "sourceDocument": lot.source_document_number,
        "receivedDate": str(lot.received_date) if lot.received_date else None,
        "uom": lot.uom,
        "available": float(lot.available_quantity),
        "allocated": float(allocated),
        "unallocated": float(unallocated),
        "status": lot.status,
        "qcStatus": lot.qc_status,
    }


@transaction.atomic
def set_project_lot_allocation(*, client_id, project, lot_taka_id, target_qty, allocation_type=GreyStockAllocation.ALLOCATION_CUSTOMER, user=None, remarks=None):
    """Bring a project's active allocation on one lot to ``target_qty``.

    Used by screens that submit the desired quantity per lot rather than a
    delta. Increases allocate the difference; decreases release whole rows
    and re-allocate whatever must stay.
    """
    target_qty = round4(max(ZERO, D(target_qty)))
    LotTaka.objects.select_for_update().filter(client_id=client_id, pk=lot_taka_id).first()
    current_rows = list(
        GreyStockAllocation.objects.select_for_update().filter(
            client_id=client_id,
            project=project,
            lot_taka_id=lot_taka_id,
            status=GreyStockAllocation.STATUS_ACTIVE,
            deleted_at__isnull=True,
        )
    )
    current = round4(sum((D(row.quantity) for row in current_rows), ZERO))
    current_type = current_rows[0].allocation_type if current_rows else None
    if target_qty == current and (not current_rows or current_type == allocation_type):
        return current
    # Consolidate: release older active rows and allocate exact target_qty in a single clean row
    for row in current_rows:
        release_allocation(client_id=client_id, allocation_id=row.id, user=user, remarks=remarks or "Allocation updated")
    if target_qty > ZERO:
        allocate_to_project(
            client_id=client_id, project=project, lot_taka_id=lot_taka_id,
            quantity=target_qty, allocation_type=allocation_type, user=user, remarks=remarks,
        )
    return target_qty


def project_allocations(client_id, project, *, include_released=False):
    qs = GreyStockAllocation.objects.filter(
        client_id=client_id, project=project, deleted_at__isnull=True
    ).select_related("lot_taka__item", "project", "created_by", "released_by")
    if not include_released:
        qs = qs.filter(status=GreyStockAllocation.STATUS_ACTIVE)
    return list(qs.order_by("-created_at"))
