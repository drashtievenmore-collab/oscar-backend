"""Job work business rules — per-row child management for orders and plans.

Child rows (materials / outwards / inwards / charges / reprocesses /
documents on a Job Work Order, charges on a Process Plan) are never
replaced wholesale by a header PATCH. Each row is created, edited or
deleted individually through these functions so every change is
validated, numbered by the server and audited.
"""
from apps.core.audit import record_audit, request_ip, snapshot
from apps.core.exceptions import Conflict, ValidationFailed
from apps.core.money import D, ZERO, round2
from apps.core.numbering import allocate_number

from . import costing as jobwork_costing

from .models import (
    JobWorkOrder,
    JWOCharge,
    JWODocument,
    JWOInward,
    JWOInwardTaka,
    JWOLossEntry,
    JWOMaterial,
    JWOOutward,
    JWOOutwardTaka,
    JWOReprocess,
    PlanCharge,
)
from .serializers import (
    JWOChargeSerializer,
    JWODocumentSerializer,
    JWOInwardSerializer,
    JWOLossEntrySerializer,
    JWOMaterialSerializer,
    JWOOutwardSerializer,
    JWOReprocessSerializer,
    PlanChargeSerializer,
)

#: kind -> (model, serializer, number series or None, human label)
ORDER_ROW_KINDS = {
    "materials": (JWOMaterial, JWOMaterialSerializer, None, "Material"),
    "outwards": (JWOOutward, JWOOutwardSerializer, "JWOUT", "Outward challan"),
    "inwards": (JWOInward, JWOInwardSerializer, "JWIN", "Inward receipt"),
    "charges": (JWOCharge, JWOChargeSerializer, None, "Charge"),
    "reprocesses": (JWOReprocess, JWOReprocessSerializer, "JWRP", "Reprocess"),
    "documents": (JWODocument, JWODocumentSerializer, None, "Document"),
    "losses": (JWOLossEntry, JWOLossEntrySerializer, None, "Approved loss"),
}
ORDER_ROW_KIND_PATTERN = "|".join(ORDER_ROW_KINDS)

#: Nested keys a header PATCH may not carry.
ORDER_NESTED_KEYS = tuple(ORDER_ROW_KINDS)
PLAN_NESTED_KEYS = ("charges",)


def live(manager):
    """Rows of a related manager that are not soft-deleted (prefetch-safe)."""
    return [row for row in manager.all() if getattr(row, "deleted_at", None) is None]


def reject_nested_on_update(data, keys):
    """Header updates never touch child rows; say so instead of ignoring them."""
    if not isinstance(data, dict):
        return
    present = [k for k in keys if k in data]
    if present:
        raise ValidationFailed(
            "Child rows cannot be changed through the header. "
            "Use the per-row endpoints instead.",
            field_errors={
                k: [f"Use /{k}/ (POST) or /{k}/{{rowId}}/ (PATCH / DELETE)."]
                for k in present
            },
        )


def recompute_order_totals(order):
    """planned_qty / total_amount are always the sum of live material rows."""
    mats = list(order.materials.filter(deleted_at__isnull=True))
    order.planned_qty = sum((m.qty or ZERO for m in mats), ZERO)
    order.total_amount = round2(sum((m.amount or ZERO for m in mats), ZERO))
    order.save(update_fields=["planned_qty", "total_amount", "updated_at"])
    return order


def line_amount(values):
    """amount = qty × rate when a rate is given, else the amount supplied."""
    qty = D(values.get("qty") or 0)
    rate = D(values.get("rate") or 0)
    if rate > ZERO:
        return round2(qty * rate)
    return round2(D(values.get("amount") or 0))


def _sum(rows, field="qty", exclude_id=None):
    return sum(
        (getattr(r, field) or ZERO for r in rows if exclude_id is None or str(r.id) != str(exclude_id)),
        ZERO,
    )


def assert_order_open(order):
    if order.status in JobWorkOrder.LOCKED_STATUSES:
        raise Conflict(
            f"Job work order {order.jwo_no} is {order.status}; its rows can no longer change.",
            code="JOBWORK_ORDER_CLOSED",
        )


def _merged(instance, data, fields):
    """Field values after applying ``data`` on top of ``instance``."""
    out = {}
    for f in fields:
        if f in data:
            out[f] = data[f]
        elif instance is not None:
            out[f] = getattr(instance, f)
        else:
            out[f] = None
    return out


def validate_order_row(order, kind, data, instance=None):
    """Kind-specific rules. ``data`` is serializer.validated_data (mutated)."""
    if kind in ("materials", "charges"):
        values = _merged(instance, data, ["qty", "rate", "amount"])
        if D(values["qty"] or 0) < ZERO:
            raise ValidationFailed("Quantity cannot be negative.", field_errors={"qty": ["Must be ≥ 0."]})
        if D(values["rate"] or 0) < ZERO:
            raise ValidationFailed("Rate cannot be negative.", field_errors={"rate": ["Must be ≥ 0."]})
        data["amount"] = line_amount(values)
        return

    if kind == "outwards":
        values = _merged(instance, data, ["qty", "date"])
        qty = D(values["qty"] or 0)
        if qty <= ZERO:
            raise ValidationFailed("Outward quantity must be more than zero.", field_errors={"qty": ["Must be > 0."]})
        if not values["date"]:
            raise ValidationFailed("Outward date is required.", field_errors={"date": ["Required."]})
        if instance is not None:
            sent_after = _sum(live(order.outwards), exclude_id=instance.id) + qty
            received = _sum(live(order.inwards))
            if sent_after < received:
                raise ValidationFailed(
                    f"Total sent ({sent_after}) would fall below total received ({received}).",
                    field_errors={"qty": ["Cannot be less than what has already been received."]},
                )
        return

    if kind == "inwards":
        values = _merged(instance, data, ["qty", "accepted", "rejected", "date"])
        qty = D(values["qty"] or 0)
        if qty <= ZERO:
            raise ValidationFailed("Received quantity must be more than zero.", field_errors={"qty": ["Must be > 0."]})
        if not values["date"]:
            raise ValidationFailed("Inward date is required.", field_errors={"date": ["Required."]})
        rejected = D(values["rejected"] or 0)
        if rejected < ZERO:
            raise ValidationFailed("Rejected quantity cannot be negative.", field_errors={"rejected": ["Must be ≥ 0."]})
        if "accepted" not in data and instance is None:
            data["accepted"] = qty - rejected
            values["accepted"] = data["accepted"]
        accepted = D(values["accepted"] or 0)
        if accepted < ZERO:
            raise ValidationFailed("Accepted quantity cannot be negative.", field_errors={"accepted": ["Must be ≥ 0."]})
        if accepted + rejected > qty:
            raise ValidationFailed(
                "Accepted + rejected cannot exceed the received quantity.",
                field_errors={"accepted": ["Accepted + rejected must be ≤ received qty."]},
            )
        sent = _sum(live(order.outwards))
        received_other = _sum(live(order.inwards), exclude_id=getattr(instance, "id", None))
        outstanding = sent - received_other
        if qty > outstanding:
            raise ValidationFailed(
                f"Received quantity ({qty}) exceeds the quantity still at the vendor ({max(outstanding, ZERO)}).",
                field_errors={"qty": [f"Max {max(outstanding, ZERO)}."]},
            )
        return

    if kind == "reprocesses":
        values = _merged(instance, data, ["qty", "inward_no", "date"])
        qty = D(values["qty"] or 0)
        if qty <= ZERO:
            raise ValidationFailed("Reprocess quantity must be more than zero.", field_errors={"qty": ["Must be > 0."]})
        if not values["date"]:
            raise ValidationFailed("Reprocess date is required.", field_errors={"date": ["Required."]})
        inward_no = values["inward_no"] or ""
        if inward_no:
            inward = next((i for i in live(order.inwards) if i.no == inward_no), None)
            if inward is None:
                raise ValidationFailed(
                    f"Inward receipt {inward_no} does not belong to this order.",
                    field_errors={"inwardNo": ["Unknown inward receipt for this order."]},
                )
            already = _sum(
                [r for r in live(order.reprocesses) if r.inward_no == inward_no],
                exclude_id=getattr(instance, "id", None),
            )
            if already + qty > (inward.qty or ZERO):
                raise ValidationFailed(
                    f"Reprocess quantity exceeds what was received on {inward_no} "
                    f"({inward.qty}, {already} already sent for reprocess).",
                    field_errors={"qty": [f"Max {max((inward.qty or ZERO) - already, ZERO)}."]},
                )
        return

    if kind == "documents":
        values = _merged(instance, data, ["file_name", "file"])
        if not values["file_name"] and not values["file"]:
            raise ValidationFailed(
                "Attach a file or give a file name.",
                field_errors={"fileName": ["Required."]},
            )
        if not data.get("type") and instance is None:
            data["type"] = "Other"
        return

    if kind == "losses":
        values = _merged(instance, data, ["qty", "loss_reason"])
        qty = D(values.get("qty") or 0)
        if qty <= ZERO:
            raise ValidationFailed("Loss quantity must be more than zero.", field_errors={"qty": ["Must be > 0."]})
        return


def validate_order_row_delete(order, kind, row):
    if kind == "outwards":
        sent_after = _sum(live(order.outwards), exclude_id=row.id)
        received = _sum(live(order.inwards))
        if sent_after < received:
            raise Conflict(
                f"Cannot delete outward {row.no}: received quantity ({received}) would exceed "
                f"what was sent ({sent_after}).",
                code="JOBWORK_ROW_IN_USE",
            )
    if kind == "inwards":
        if any(r.inward_no and r.inward_no == row.no for r in live(order.reprocesses)):
            raise Conflict(
                f"Cannot delete inward {row.no}: reprocess rows reference it. Delete those first.",
                code="JOBWORK_ROW_IN_USE",
            )


def _audit(request, order, entity_type, action, description, before=None, after=None):
    record_audit(
        client=getattr(request, "client_id", None),
        actor=getattr(request, "user", None),
        action=action,
        entity_type=entity_type,
        entity_id=order.id,
        entity_label=getattr(order, "jwo_no", None) or getattr(order, "plan_no", None),
        description=description,
        before=before,
        after=after,
        ip=request_ip(request),
    )


def _touch(header, user):
    fields = ["updated_at"]
    if hasattr(header, "updated_by_id") and getattr(user, "is_authenticated", False):
        header.updated_by = user
        fields.append("updated_by")
    header.save(update_fields=fields)




def validate_outward_stock(order, takas_data=None, qty=None):
    """Pre-flight validation for Stage 12 Job Work Outward dispatch."""
    from apps.inventory.models import LotTaka

    if order.status in JobWorkOrder.LOCKED_STATUSES:
        raise ValidationFailed(f"Job Work Order {order.jwo_no} is locked ({order.status}).")

    total_qty = D(str(qty or 0))
    dispatched_so_far = _sum([o for o in live(order.outwards) if o.status in ("Sent", "Confirmed")])
    remaining_planned = max(D(order.planned_qty or 0) - dispatched_so_far, ZERO)

    taka_errors = []
    validated_takas = []
    calculated_qty = ZERO

    if takas_data:
        for item in takas_data:
            taka_id = item.get("lot_taka_id") or item.get("lotTakaId") or item.get("lot_taka")
            if isinstance(taka_id, LotTaka):
                lot_taka = taka_id
            else:
                lot_taka = LotTaka.objects.filter(client_id=order.client_id, id=taka_id).first()

            if not lot_taka:
                taka_errors.append(f"Taka {taka_id} not found.")
                continue

            if lot_taka.status == "External WIP":
                taka_errors.append(f"Roll {lot_taka.taka_number or lot_taka.taka_id} is already at external processor WIP.")
            elif lot_taka.status in ("Dispatched", "QC Hold", "Scrapped", "Consumed"):
                taka_errors.append(f"Roll {lot_taka.taka_number or lot_taka.taka_id} has status '{lot_taka.status}' and cannot be dispatched.")

            taka_meters = D(str(item.get("meters") or lot_taka.available_quantity or 0))
            if taka_meters <= ZERO:
                taka_errors.append(f"Roll {lot_taka.taka_number or lot_taka.taka_id} has 0 available metres.")
            elif taka_meters > D(str(lot_taka.available_quantity or 0)):
                taka_errors.append(f"Roll {lot_taka.taka_number or lot_taka.taka_id}: requested {taka_meters}M exceeds available {lot_taka.available_quantity}M.")

            calculated_qty += taka_meters
            validated_takas.append({
                "id": str(lot_taka.id),
                "takaNumber": lot_taka.taka_number,
                "lotNumber": lot_taka.lot_number,
                "meters": float(taka_meters),
                "availableQuantity": float(lot_taka.available_quantity or 0),
            })

    if total_qty <= ZERO and calculated_qty > ZERO:
        total_qty = calculated_qty

    if taka_errors:
        raise ValidationFailed("; ".join(taka_errors), field_errors={"takas": taka_errors})

    return {
        "valid": True,
        "totalQty": float(total_qty),
        "remainingPlanned": float(remaining_planned),
        "dispatchedSoFar": float(dispatched_so_far),
        "validatedTakasCount": len(validated_takas),
        "validatedTakas": validated_takas,
    }


def save_order_row(request, order, kind, serializer, instance=None):
    """Create (instance=None) or update one child row of a JWO."""
    model, _ser, series, label = ORDER_ROW_KINDS[kind]
    assert_order_open(order)
    data = dict(serializer.validated_data)
    validate_order_row(order, kind, data, instance)
    user = getattr(request, "user", None)
    authed = getattr(user, "is_authenticated", False)

    takas_data = None
    if kind in ("outwards", "inwards"):
        takas_data = data.pop("takas", None)

    if instance is None:
        if series:
            data["no"] = allocate_number(request.user.client, series, data.get("date"))
        row = model.objects.create(
            client_id=order.client_id,
            job_work_order=order,
            created_by=user if authed else None,
            updated_by=user if authed else None,
            **data,
        )
        _audit(
            request, order, "JobWorkOrder", f"{kind}_create",
            f"{label} {getattr(row, 'no', '') or ''} added".replace("  ", " "),
            after=snapshot(row),
        )
    else:
        before = snapshot(instance)
        for field, value in data.items():
            setattr(instance, field, value)
        if authed:
            instance.updated_by = user
        instance.save()
        row = instance
        _audit(
            request, order, "JobWorkOrder", f"{kind}_update",
            f"{label} {getattr(row, 'no', '') or ''} updated".replace("  ", " "),
            before=before, after=snapshot(row),
        )

    # Post real stock movements and create roll-by-roll taka lines (Phase 2.5)
    if kind == "outwards":
        from apps.inventory import services as stock_services
        from apps.inventory.models import StockMovement, LotTaka

        # Save or update individual taka lines
        if takas_data:
            for t_entry in takas_data:
                lot_taka = t_entry.get("lot_taka")
                if not lot_taka:
                    t_id = t_entry.get("lot_taka_id") or t_entry.get("lotTakaId")
                    if t_id:
                        lot_taka = LotTaka.objects.filter(client_id=order.client_id, id=t_id).first()
                if not lot_taka:
                    continue
                meters = D(t_entry.get("meters") or lot_taka.available_quantity or 0)
                weight_kg = t_entry.get("weight_kg")
                outward_taka, created = JWOOutwardTaka.objects.get_or_create(
                    client_id=order.client_id,
                    outward=row,
                    lot_taka=lot_taka,
                    defaults={"meters": meters, "weight_kg": weight_kg},
                )
                if not created and (meters != outward_taka.meters or weight_kg != outward_taka.weight_kg):
                    outward_taka.meters = meters
                    outward_taka.weight_kg = weight_kg
                    outward_taka.save(update_fields=["meters", "weight_kg", "updated_at"])

        # Stock deduction only on confirmed outward, with strict idempotency guard
        is_confirmed = str(getattr(row, "status", "")).strip() in ("Sent", "Confirmed")
        if is_confirmed:
            vendor_party = order.vendor
            wip_loc = stock_services.wip_location_for_party(order.client_id, vendor_party or order.vendor_name)

            already_moved = StockMovement.objects.filter(
                client_id=order.client_id,
                reference_type="JobWorkOrder",
                reference_id=order.id,
                reference_number=row.no or order.jwo_no,
                type="JOBWORK_OUTWARD",
            ).exists()

            if not already_moved:
                for ot in row.takas.select_related("lot_taka").all():
                    lot_taka = ot.lot_taka
                    if lot_taka and lot_taka.status != "External WIP":
                        meters = ot.meters or lot_taka.available_quantity or 0
                        stock_services.relocate_lot(
                            client_id=order.client_id,
                            lot=lot_taka,
                            quantity=meters,
                            to_location_id=wip_loc.id,
                            out_type="JOBWORK_OUTWARD",
                            in_type="JOBWORK_OUTWARD",
                            reference_type="JobWorkOrder",
                            reference_id=order.id,
                            reference_number=row.no or order.jwo_no,
                            user=user,
                        )
                        lot_taka.status = "External WIP"
                        lot_taka.save(update_fields=["status", "updated_at"])

            if order.status in ("Draft", "Sent"):
                order.status = "In-Process"
                order.save(update_fields=["status", "updated_at"])

    elif kind == "inwards" and takas_data:
        from apps.inventory import services as stock_services
        from apps.masters.models import Location
        from apps.inventory.models import LotTaka
        qc_loc = Location.objects.filter(client_id=order.client_id, code="SYS-QC-HOLD").first()
        if qc_loc is None:
            qc_loc = Location.objects.create(
                client_id=order.client_id,
                code="SYS-QC-HOLD",
                name="QC Hold Stock",
                type="Godown",
                is_active=True,
            )
        vendor_party = order.vendor
        wip_loc = stock_services.wip_location_for_party(order.client_id, vendor_party or order.vendor_name)

        first_mat = live(order.materials)
        mat_item = None
        if first_mat and getattr(first_mat[0], "fabric_item", None):
            from apps.masters.models import Item
            mat_item = Item.objects.filter(client_id=order.client_id, name__iexact=first_mat[0].fabric_item).first()
            if not mat_item:
                mat_item = Item.objects.filter(client_id=order.client_id, sku__iexact=first_mat[0].fabric_item).first()

        for idx, t_entry in enumerate(takas_data):
            source_taka = t_entry.get("source_taka")
            meters = D(t_entry.get("meters") or 0)
            weight_kg = t_entry.get("weight_kg")
            width = t_entry.get("width")
            shade = t_entry.get("shade")
            target_item = (source_taka.item if source_taka else None) or mat_item

            rec_lot_num = t_entry.get("lot_number") or (
                f"{source_taka.lot_number}-P" if source_taka else f"JWO-{order.jwo_no}"
            )
            taka_num = str(t_entry.get("taka_number") or (idx + 1))
            taka_id = t_entry.get("taka_id") or f"TAK-{rec_lot_num}-{taka_num}"

            rec_taka = None
            if target_item:
                rec_taka = LotTaka.objects.create(
                    client_id=order.client_id,
                    item=target_item,
                    location=qc_loc,
                    lot_number=rec_lot_num,
                    taka_number=taka_num,
                    taka_id=taka_id,
                    received_quantity=meters,
                    available_quantity=ZERO,
                    roll_weight=weight_kg,
                    width=width,
                    status="QC Hold",
                    qc_status="Pending",
                    parent_taka=source_taka,
                    source_document_type="JobWorkInward",
                    source_document_id=order.id,
                    source_document_number=row.no,
                )
                if source_taka:
                    rec_taka.parent_lots.add(source_taka)

            JWOInwardTaka.objects.create(
                client_id=order.client_id,
                inward=row,
                source_taka=source_taka,
                received_taka=rec_taka,
                taka_number=taka_num,
                meters=meters,
                weight_kg=weight_kg,
                width=width,
                shade=shade,
            )

            if target_item:
                if source_taka and str(source_taka.location_id) == str(wip_loc.id):
                    consume_qty = min(meters, D(source_taka.available_quantity or 0))
                    if consume_qty > ZERO:
                        stock_services.post_movement(
                            client_id=order.client_id,
                            item=source_taka.item,
                            location=wip_loc,
                            type="JOBWORK_INWARD",
                            quantity=-consume_qty,
                            lot_taka=source_taka,
                            reference_type="JobWorkOrder",
                            reference_id=order.id,
                            reference_number=row.no or order.jwo_no,
                            user=user,
                        )
                stock_services.post_movement(
                    client_id=order.client_id,
                    item=target_item,
                    location=qc_loc,
                    type="JOBWORK_INWARD",
                    quantity=meters,
                    lot_taka=rec_taka,
                    reference_type="JobWorkOrder",
                    reference_id=order.id,
                    reference_number=row.no or order.jwo_no,
                    user=user,
                )

                # Processing charges are a direct cost of the received lot (PDF §09).
                proc_cost = round2(D(order.rate or 0) * meters)
                if proc_cost > ZERO:
                    from apps.inventory import costing_services

                    costing_services.post_lot_cost(
                        client_id=order.client_id,
                        lot_number=rec_taka.lot_number if rec_taka else rec_lot_num,
                        cost_type="Dyeing/Printing/Finishing",
                        amount=proc_cost,
                        quantity=meters,
                        source_document_type="JobWorkInward",
                        source_document_id=row.id,
                        source_document_number=row.no or order.jwo_no,
                        lot_taka=rec_taka,
                        user=user,
                        is_approved=True,
                        remarks=f"Processing charges for JWO {order.jwo_no}",
                    )

        # ... and owed to the processor: accrue everything posted for this
        # receipt so far in the books.
        jobwork_costing.sync_jobwork_accrual(
            order, source_type="JobWorkInward", source_id=row.id,
            amount=jobwork_costing.total_posted(order.client_id, "JobWorkInward", row.id),
            narration=f"Processing charges {row.no or ''}".strip(), user=user,
        )

    elif kind == "reprocesses":
        # The charge lands on the lots being reprocessed, next to their
        # original processing entry (PDF §08 costing rule).
        rate = serializer.initial_data.get("rate") if hasattr(serializer, "initial_data") else None
        jobwork_costing.sync_reprocess_cost(
            order, row, rate=D(rate) if rate not in (None, "") else None, user=user,
        )

    elif kind == "charges":
        jobwork_costing.sync_jobwork_accrual(
            order, source_type="JWOCharge", source_id=row.id,
            amount=ZERO if row.status == "Cancelled" else row.amount,
            narration=f"Job work charge: {row.type or row.description or ''}".strip(), user=user,
        )

    if kind == "materials":
        recompute_order_totals(order)
    else:
        _touch(order, user)
    return row


def delete_order_row(request, order, kind, row):
    _model, _ser, _series, label = ORDER_ROW_KINDS[kind]
    assert_order_open(order)
    validate_order_row_delete(order, kind, row)
    user = getattr(request, "user", None)
    before = snapshot(row)
    row.soft_delete(user if getattr(user, "is_authenticated", False) else None)
    # A deleted charge or reprocess round no longer costs anything: its lot
    # cost and accrual are reversed by an equal and opposite posting.
    if kind == "charges":
        jobwork_costing.sync_jobwork_accrual(
            order, source_type="JWOCharge", source_id=row.id, amount=ZERO,
            narration=f"Job work charge deleted: {row.type or ''}".strip(), user=user,
        )
    elif kind == "reprocesses":
        row.status = "Cancelled"
        jobwork_costing.sync_reprocess_cost(order, row, user=user)
    _audit(
        request, order, "JobWorkOrder", f"{kind}_delete",
        f"{label} {getattr(row, 'no', '') or ''} deleted".replace("  ", " "),
        before=before,
    )
    if kind == "materials":
        recompute_order_totals(order)
    else:
        _touch(order, user)


def create_order_rows(order, rows_by_kind, client):
    """Rows posted together with a brand-new order (create only)."""
    for kind in ("materials", "outwards", "inwards", "charges", "reprocesses", "losses"):
        model, _ser, series, _label = ORDER_ROW_KINDS[kind]
        for data in rows_by_kind.get(kind) or []:
            data = dict(data)
            data.pop("id", None)
            data.pop("no", None)
            # Validate against what has been created so far (outwards before inwards).
            validate_order_row(order, kind, data)
            if series:
                data["no"] = allocate_number(client, series, data.get("date"))
            row = model.objects.create(client_id=order.client_id, job_work_order=order, **data)
            if kind == "charges":
                jobwork_costing.sync_jobwork_accrual(
                    order, source_type="JWOCharge", source_id=row.id,
                    amount=ZERO if row.status == "Cancelled" else row.amount,
                    narration=f"Job work charge: {row.type or row.description or ''}".strip(),
                )
    recompute_order_totals(order)


# ---------------------------------------------------------------------------
# Process plan charges
# ---------------------------------------------------------------------------
PLAN_ROW_KINDS = {"charges": (PlanCharge, PlanChargeSerializer, None, "Charge")}


def assert_plan_open(plan):
    if plan.status == "Completed":
        raise Conflict(
            f"Process plan {plan.plan_no} is Completed; its charges can no longer change.",
            code="JOBWORK_PLAN_COMPLETED",
        )


def save_plan_charge(request, plan, serializer, instance=None):
    assert_plan_open(plan)
    data = serializer.validated_data
    values = _merged(instance, data, ["qty", "rate", "amount"])
    if D(values["qty"] or 0) < ZERO or D(values["rate"] or 0) < ZERO:
        raise ValidationFailed("Quantity and rate cannot be negative.", field_errors={"qty": ["Must be ≥ 0."]})
    data["amount"] = line_amount(values)
    user = getattr(request, "user", None)
    authed = getattr(user, "is_authenticated", False)
    if instance is None:
        row = PlanCharge.objects.create(
            client_id=plan.client_id,
            process_plan=plan,
            created_by=user if authed else None,
            updated_by=user if authed else None,
            **data,
        )
        _audit(request, plan, "ProcessPlan", "charges_create", f"Charge {row.type} added", after=snapshot(row))
    else:
        before = snapshot(instance)
        for field, value in data.items():
            setattr(instance, field, value)
        if authed:
            instance.updated_by = user
        instance.save()
        row = instance
        _audit(request, plan, "ProcessPlan", "charges_update", f"Charge {row.type} updated",
               before=before, after=snapshot(row))
    _touch(plan, user)
    return row


def delete_plan_charge(request, plan, row):
    assert_plan_open(plan)
    user = getattr(request, "user", None)
    before = snapshot(row)
    row.soft_delete(user if getattr(user, "is_authenticated", False) else None)
    _audit(request, plan, "ProcessPlan", "charges_delete", f"Charge {row.type} deleted", before=before)
    _touch(plan, user)
