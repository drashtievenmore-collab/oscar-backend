"""
Logistics / Transport / Tempo trip services (PDF §13).

Tracks transportation trips, vehicle/driver details, linked documents, and material lines.
Supports allocating transport freight & charges directly into related LotCostEntry ledgers.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import BusinessRuleViolation, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.inventory.costing_services import post_lot_cost
from apps.inventory.models import LotTaka, Trip, TripMaterialLine


@transaction.atomic
def create_trip(
    *,
    client_id,
    from_location,
    to_location,
    vehicle_number=None,
    vehicle_id=None,
    driver_id=None,
    trip_date=None,
    transporter_id=None,
    transporter_party_id=None,
    driver_name=None,
    driver_mobile=None,
    lr_number=None,
    lr_date=None,
    trip_type="OUTWARD",
    freight_amount=0,
    loading_charges=0,
    unloading_charges=0,
    other_charges=0,
    paid_by="Company",
    cost_allocation_target="LOT",
    material_lines=None,
    remarks=None,
    user=None,
):
    """Create a new transport trip with material lines."""
    trip_number = allocate_number(client_id, "TRP")
    freight = D(freight_amount or 0)
    loading = D(loading_charges or 0)
    unloading = D(unloading_charges or 0)
    other = D(other_charges or 0)
    total_charges = round2(freight + loading + unloading + other)

    if vehicle_id and not vehicle_number:
        from apps.masters.models import Vehicle
        v_obj = Vehicle.objects.filter(client_id=client_id, pk=vehicle_id).first()
        if v_obj:
            vehicle_number = v_obj.vehicle_number
            if not transporter_id and v_obj.transporter_id:
                transporter_id = v_obj.transporter_id

    if driver_id and (not driver_name or not driver_mobile):
        from apps.masters.models import Driver
        d_obj = Driver.objects.filter(client_id=client_id, pk=driver_id).first()
        if d_obj:
            if not driver_name:
                driver_name = d_obj.name
            if not driver_mobile:
                driver_mobile = d_obj.mobile

    vehicle_number = vehicle_number or "VEHICLE"

    trip = Trip.objects.create(
        client_id=client_id,
        trip_number=trip_number,
        trip_date=trip_date or timezone.localdate(),
        from_location=from_location,
        to_location=to_location,
        transporter_id=transporter_id,
        transporter_party_id=transporter_party_id,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        vehicle_number=vehicle_number,
        driver_name=driver_name,
        driver_mobile=driver_mobile,
        lr_number=lr_number,
        lr_date=lr_date,
        trip_type=trip_type,
        status="Draft",
        freight_amount=freight,
        loading_charges=loading,
        unloading_charges=unloading,
        other_charges=other,
        total_charges=total_charges,
        paid_by=paid_by,
        cost_allocation_target=cost_allocation_target,
        remarks=remarks,
    )

    if material_lines:
        for line_data in material_lines:
            taka_id = line_data.get("lotTakaId") or line_data.get("lot_taka_id")
            lot_taka = None
            if taka_id:
                lot_taka = LotTaka.objects.filter(client_id=client_id, pk=taka_id).first()

            TripMaterialLine.objects.create(
                client_id=client_id,
                trip=trip,
                lot_taka=lot_taka,
                lot_number=line_data.get("lotNumber") or (lot_taka.lot_number if lot_taka else ""),
                taka_number=line_data.get("takaNumber") or (lot_taka.taka_number if lot_taka else ""),
                item_id=line_data.get("itemId") or (lot_taka.item_id if lot_taka else None),
                quantity=D(line_data.get("quantity") or line_data.get("qty") or 0),
                roll_weight=D(line_data.get("rollWeight") or line_data.get("roll_weight") or 0) if line_data.get("rollWeight") or line_data.get("roll_weight") else None,
                document_reference_type=line_data.get("documentReferenceType") or line_data.get("document_reference_type"),
                document_reference_id=line_data.get("documentReferenceId") or line_data.get("document_reference_id"),
                document_reference_number=line_data.get("documentReferenceNumber") or line_data.get("document_reference_number"),
            )

    return trip


@transaction.atomic
def allocate_trip_freight(trip, *, user=None):
    """
    Apportion trip charges across material lines and post directly into the lot cost ledger (PDF §09, §13).
    """
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    if trip.is_cost_allocated:
        raise BusinessRuleViolation("Cost for this trip has already been allocated.")

    total_charges = D(trip.total_charges)
    if total_charges <= ZERO:
        trip.is_cost_allocated = True
        trip.save(update_fields=["is_cost_allocated", "updated_at"])
        return trip

    lines = list(trip.materials.all())
    if not lines:
        raise ValidationFailed("Cannot allocate cost: trip has no material lines.")

    total_meters = sum([D(line.quantity) for line in lines])
    total_weight = sum([D(line.roll_weight or 0) for line in lines])

    use_weight = total_weight > ZERO and total_meters <= ZERO
    divisor = total_weight if use_weight else total_meters

    cost_type = (
        "Processing transport"
        if trip.trip_type in ("JOBWORK_OUT", "JOBWORK_IN")
        else "Purchase freight/loading"
    )

    allocated_so_far = ZERO
    for idx, line in enumerate(lines, start=1):
        basis_qty = D(line.roll_weight or 0) if use_weight else D(line.quantity)
        if idx == len(lines):
            allocated = round4(total_charges - allocated_so_far)
        else:
            allocated = round4((basis_qty / divisor) * total_charges) if divisor > ZERO else ZERO

        allocated_so_far += allocated
        line.allocated_freight = allocated
        line.save(update_fields=["allocated_freight", "updated_at"])

        lot_num = line.lot_number or (line.lot_taka.lot_number if line.lot_taka else None)
        if lot_num and trip.cost_allocation_target == "LOT":
            post_lot_cost(
                client_id=trip.client_id,
                lot_number=lot_num,
                lot_taka=line.lot_taka,
                cost_type=cost_type,
                amount=allocated,
                quantity=line.quantity,
                source_document_type="TRIP",
                source_document_id=trip.id,
                source_document_number=trip.trip_number,
                user=user,
                is_approved=True,
                remarks=f"Freight allocation from Trip {trip.trip_number} ({trip.vehicle_number})",
            )

        if trip.cost_allocation_target == "SALES_ORDER":
            from apps.sales.models import SalesOrder
            so = None
            if line.document_reference_id:
                so = SalesOrder.objects.filter(client_id=trip.client_id, pk=line.document_reference_id).first()
            if not so and line.document_reference_number:
                so = SalesOrder.objects.filter(client_id=trip.client_id, order_number=line.document_reference_number).first()
            if so:
                so.allocated_freight_cost = F("allocated_freight_cost") + round2(allocated)
                so.save(update_fields=["allocated_freight_cost", "updated_at"])

    # Post GL journal entry for EXPENSE target OR when paid_by is Company (freight payable)
    if (trip.paid_by == "Company" or trip.cost_allocation_target == "EXPENSE") and total_charges > ZERO:
        freight_acc = ledger.system_account(trip.client_id, "freight")
        creditors_acc = ledger.system_account(trip.client_id, "creditors")
        transporter_party = trip.transporter_party
        if not transporter_party and trip.transporter:
            from apps.masters.models import Party
            transporter_party = Party.objects.filter(
                client_id=trip.client_id,
                name__iexact=trip.transporter.name,
                deleted_at__isnull=True,
            ).first()

        ledger.post_entry(
            client=trip.client_id,
            entry_date=trip.trip_date or timezone.localdate(),
            narration=f"Freight allocation for Trip {trip.trip_number} ({trip.vehicle_number})",
            source_document_type="TRIP",
            source_document_id=trip.id,
            postings=[
                ledger.debit(
                    freight_acc,
                    total_charges,
                    description=f"Freight charge Trip {trip.trip_number}",
                ),
                ledger.credit(
                    creditors_acc,
                    total_charges,
                    party=transporter_party,
                    description=f"Freight payable Trip {trip.trip_number}",
                ),
            ],
            user=user,
        )

    trip.is_cost_allocated = True
    trip.save(update_fields=["is_cost_allocated", "updated_at"])
    return trip


@transaction.atomic
def confirm_trip_delivery(trip, *, delivery_time=None, proof_file_id=None, remarks=None, user=None):
    """
    Confirm delivery for a transport trip (PDF §13).
    Sets status to 'Delivered', and optionally attaches proof file and remarks.
    """
    trip = Trip.objects.select_for_update().get(pk=trip.pk)
    trip.status = "Delivered"
    trip.delivery_time = delivery_time or timezone.now()
    if proof_file_id:
        trip.proof_file_id = proof_file_id
    if remarks:
        trip.remarks = f"{trip.remarks}\n{remarks}" if trip.remarks else remarks
    trip.save(update_fields=["status", "delivery_time", "proof_file", "remarks", "updated_at"])
    return trip
