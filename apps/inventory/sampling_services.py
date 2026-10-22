"""
Sampling Management Service (PDF §10).

Sampling is controlled as stock movement and expense booking:
- Sample issue: deducts meters / moves taka to Sample stock bucket, records SAMPLE_ISSUE movement.
- Sample return: returns usable meters back to Finished inventory, records SAMPLE_RETURN movement.
- Consumed / free sample: books cost to Sample Expense ledger account.
- Overdue tracking for returnable samples.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounting import services as ledger
from apps.core.exceptions import BusinessRuleViolation, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.inventory import services as stock
from apps.inventory.models import LotTaka, Sample, StockMovement


@transaction.atomic
def issue_sample(
    *,
    client_id,
    party_id=None,
    salesperson_id=None,
    broker_id=None,
    source_lot_taka_id=None,
    fabric_quality_id=None,
    design_id=None,
    shade_id=None,
    sample_type="CUT_PIECE",
    quantity=1,
    uom="MTR",
    is_returnable=True,
    due_date=None,
    photo_file=None,
    remarks=None,
    user=None,
):
    """Issue a fabric sample with physical stock deduction."""
    quantity = round4(D(quantity))
    if quantity <= ZERO:
        raise ValidationFailed("Sample quantity must be greater than zero.")

    # A sample is cut from a real taka, so the stock always comes off it
    # (PDF §10: sampling is a stock movement, not only a photo record).
    if not source_lot_taka_id:
        raise ValidationFailed(
            "Choose the lot / taka the sample is cut from.",
            field_errors={"sourceLotTakaId": ["Required: the sample is deducted from this taka."]},
        )
    source_taka = LotTaka.objects.select_for_update().filter(
        client_id=client_id, pk=source_lot_taka_id, deleted_at__isnull=True
    ).first()
    if source_taka is None:
        raise NotFound("Source lot taka not found.")

    # The SAMPLE_ISSUE movement below takes the quantity off the taka; it
    # is not reduced here as well (that deducted the sample twice).
    if sample_type == "FULL_TAKA":
        if source_taka.available_quantity <= ZERO:
            raise BusinessRuleViolation("Taka has zero available quantity.")
        quantity = source_taka.available_quantity
    elif source_taka.available_quantity < quantity:
        raise BusinessRuleViolation(
            f"Taka only has {source_taka.available_quantity} available, {quantity} requested."
        )

    fabric_quality_id = fabric_quality_id or source_taka.fabric_quality_id
    design_id = design_id or source_taka.design_id
    shade_id = shade_id or source_taka.shade_id
    unit_rate = D(source_taka.rate) if source_taka.rate else ZERO
    estimated_cost = round2(quantity * unit_rate)

    sample_no = allocate_number(client_id, "SMP")
    sample = Sample.objects.create(
        client_id=client_id,
        sample_number=sample_no,
        party_id=party_id,
        salesperson_id=salesperson_id,
        broker_id=broker_id,
        source_lot_taka=source_taka,
        fabric_quality_id=fabric_quality_id,
        design_id=design_id,
        shade_id=shade_id,
        sample_type=sample_type,
        quantity=quantity,
        uom=uom,
        is_returnable=is_returnable,
        due_date=due_date,
        status="Issued",
        estimated_cost=estimated_cost,
        photo_file=photo_file,
        issued_by=user if getattr(user, "is_authenticated", False) else None,
        remarks=remarks,
    )

    stock.post_movement(
        client_id=client_id,
        item=source_taka.item,
        location=source_taka.location,
        type="SAMPLE_ISSUE",
        quantity=-quantity,
        uom=uom,
        unit_cost=source_taka.rate,
        reference_type="Sample",
        reference_id=sample.id,
        reference_number=sample.sample_number,
        movement_date=sample.sample_date,
        user=user,
        lot_taka=source_taka,
        notes=f"Sample issue {sample.sample_number}",
    )
    if sample_type == "FULL_TAKA":
        LotTaka.objects.filter(pk=source_taka.pk).update(status=LotTaka.STATUS_SAMPLE)

    return sample


@transaction.atomic
def return_sample(sample, return_qty, *, condition="Good", user=None, remarks=None):
    """Process return of an issued sample back to inventory."""
    sample = Sample.objects.select_for_update().get(pk=sample.pk)
    return_qty = round4(D(return_qty))

    if return_qty <= ZERO:
        raise ValidationFailed("Return quantity must be greater than zero.")

    remaining = round4(D(sample.quantity) - D(sample.returned_quantity) - D(sample.consumed_quantity))
    if return_qty > remaining:
        raise BusinessRuleViolation(
            f"Cannot return {return_qty}; only {remaining} pending return on this sample."
        )

    sample.returned_quantity = round4(sample.returned_quantity + return_qty)
    if sample.returned_quantity + sample.consumed_quantity >= sample.quantity:
        sample.status = "Returned"
    else:
        sample.status = "Partially Returned"

    sample.save(update_fields=["returned_quantity", "status", "updated_at"])

    # Restore inventory: the SAMPLE_RETURN movement puts the quantity back on
    # the taka; it is not added here as well (that returned it twice).
    source_taka = sample.source_lot_taka
    if source_taka:
        if sample.sample_type == "FULL_TAKA":
            source_taka.status = LotTaka.STATUS_FINISHED if condition == "Good" else LotTaka.STATUS_QC_HOLD
            source_taka.save(update_fields=["status", "updated_at"])

        mv_type = "SAMPLE_RETURN" if condition == "Good" else "SAMPLE_RETURN_HOLD"
        stock.post_movement(
            client_id=sample.client_id,
            item=source_taka.item,
            location=source_taka.location,
            type=mv_type,
            quantity=return_qty,
            uom=sample.uom,
            unit_cost=source_taka.rate,
            reference_type="Sample",
            reference_id=sample.id,
            reference_number=sample.sample_number,
            movement_date=timezone.localdate(),
            user=user,
            lot_taka=source_taka,
            notes=f"Sample return {sample.sample_number} ({condition})",
        )

    return sample


@transaction.atomic
def consume_sample(sample, consume_qty=None, *, user=None, remarks=None):
    """Mark unreturned sample as consumed and book to Sample Expense account."""
    sample = Sample.objects.select_for_update().get(pk=sample.pk)
    pending = round4(D(sample.quantity) - D(sample.returned_quantity) - D(sample.consumed_quantity))

    consume_qty = round4(D(consume_qty)) if consume_qty is not None else pending
    if consume_qty <= ZERO:
        raise ValidationFailed("Consumed quantity must be greater than zero.")
    if consume_qty > pending:
        raise BusinessRuleViolation(f"Only {pending} is unreturned on this sample.")

    sample.consumed_quantity = round4(sample.consumed_quantity + consume_qty)
    if sample.returned_quantity + sample.consumed_quantity >= sample.quantity:
        sample.status = "Consumed"
    else:
        sample.status = "Partially Returned"

    # Post accounting entry: Dr Sample Expense / Cr Stock
    unit_cost = ZERO
    if sample.source_lot_taka and sample.source_lot_taka.rate:
        unit_cost = D(sample.source_lot_taka.rate)
    expense_amount = round2(consume_qty * unit_cost)

    if expense_amount > ZERO:
        # A posting failure rolls back the consumption instead of leaving the
        # sample consumed with nothing in the books.
        entry = ledger.post_entry(
            client=sample.client_id,
            postings=[
                ledger.debit(
                    ledger.system_account(sample.client_id, "sample_expense"),
                    expense_amount,
                    description=f"Sample {sample.sample_number} consumed",
                ),
                ledger.credit(
                    ledger.system_account(sample.client_id, "stock"),
                    expense_amount,
                    description=f"Stock deduction for sample {sample.sample_number}",
                ),
            ],
            narration=f"Sample consumption: {sample.sample_number}",
            source_document_type="Sample",
            source_document_id=sample.id,
            user=user,
        )
        sample.journal_entry = entry

    sample.save(update_fields=["consumed_quantity", "status", "journal_entry", "updated_at"])
    return sample


def get_sample_expense_dashboard(client_id):
    """Return KPI tiles and overdue register for sampling."""
    today = timezone.localdate()
    qs = Sample.objects.filter(client_id=client_id, deleted_at__isnull=True)

    # Auto-flag overdue samples
    overdue_qs = qs.filter(
        is_returnable=True,
        due_date__lt=today,
        status__in=["Issued", "Partially Returned"],
    )
    overdue_qs.update(status="Overdue")

    total_issued = qs.count()
    returned_count = qs.filter(status="Returned").count()
    consumed_count = qs.filter(status="Consumed").count()
    overdue_count = qs.filter(status="Overdue").count()

    total_meters_issued = qs.aggregate(val=Sum("quantity"))["val"] or ZERO
    total_meters_returned = qs.aggregate(val=Sum("returned_quantity"))["val"] or ZERO
    total_meters_consumed = qs.aggregate(val=Sum("consumed_quantity"))["val"] or ZERO
    total_consumed_expense = qs.filter(status="Consumed").aggregate(val=Sum("estimated_cost"))["val"] or ZERO
    overdue_value = qs.filter(status="Overdue").aggregate(val=Sum("estimated_cost"))["val"] or ZERO

    return {
        "totalIssued": total_issued,
        "returnedCount": returned_count,
        "consumedCount": consumed_count,
        "overdueCount": overdue_count,
        "totalMetersIssued": str(round2(total_meters_issued)),
        "totalMetersReturned": str(round2(total_meters_returned)),
        "totalMetersConsumed": str(round2(total_meters_consumed)),
        "totalConsumedExpense": str(round2(total_consumed_expense)),
        "overdueValue": str(round2(overdue_value)),
    }
