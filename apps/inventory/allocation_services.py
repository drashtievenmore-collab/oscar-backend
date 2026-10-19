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
ALLOCATABLE_QC_STATUSES = ("Passed",)
NON_ALLOCATABLE_STATUSES = (
    LotTaka.STATUS_EXTERNAL_WIP, LotTaka.STATUS_QC_HOLD, LotTaka.STATUS_REPROCESS,
    LotTaka.STATUS_SECOND_QUALITY, LotTaka.STATUS_REJECT, LotTaka.STATUS_SAMPLE,
    LotTaka.STATUS_DISPATCHED, LotTaka.STATUS_FINISHED,
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
def allocate_to_project(*, client_id, project, lot_taka_id, quantity, user=None, remarks=None):
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
        description=f"{quantity} {lot.uom or ''} of {lot.taka_id or lot.lot_number} allocated to {project.code}",
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
def set_project_lot_allocation(*, client_id, project, lot_taka_id, target_qty, user=None, remarks=None):
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
    if target_qty == current:
        return current
    if target_qty > current:
        allocate_to_project(
            client_id=client_id, project=project, lot_taka_id=lot_taka_id,
            quantity=target_qty - current, user=user, remarks=remarks,
        )
        return target_qty
    for row in current_rows:
        release_allocation(client_id=client_id, allocation_id=row.id, user=user, remarks=remarks or "Allocation reduced")
    if target_qty > ZERO:
        allocate_to_project(
            client_id=client_id, project=project, lot_taka_id=lot_taka_id,
            quantity=target_qty, user=user, remarks=remarks,
        )
    return target_qty


def project_allocations(client_id, project, *, include_released=False):
    qs = GreyStockAllocation.objects.filter(
        client_id=client_id, project=project, deleted_at__isnull=True
    ).select_related("lot_taka__item", "project", "created_by", "released_by")
    if not include_released:
        qs = qs.filter(status=GreyStockAllocation.STATUS_ACTIVE)
    return list(qs.order_by("-created_at"))
