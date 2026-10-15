"""
Landed cost & Lot profitability service (PDF §09).

Maintains the LotCostEntry append-only ledger for direct expenses:
- Grey purchase
- Purchase freight / loading
- Dyeing / Printing / Finishing (Job work)
- Processing transport
- Reprocessing
- Other direct charges
- Scrap recovery (credit/negative)

Computes cost per good finished meter and lot contribution / profitability.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.inventory.models import LotCostEntry, LotTaka


@transaction.atomic
def post_lot_cost(
    *,
    client_id,
    lot_number,
    cost_type,
    amount,
    quantity=0,
    source_document_type="MANUAL",
    source_document_id=None,
    source_document_number=None,
    lot_taka=None,
    user=None,
    is_approved=True,
    remarks=None,
):
    """Post a cost entry to the lot cost ledger."""
    amount = round4(D(amount))
    quantity = round4(D(quantity or 0))
    cost_per_meter = round4(amount / quantity) if quantity > ZERO else ZERO

    entry = LotCostEntry.objects.create(
        client_id=client_id,
        lot_number=lot_number,
        lot_taka=lot_taka,
        cost_type=cost_type,
        amount=amount,
        quantity=quantity,
        cost_per_meter=cost_per_meter,
        source_document_type=source_document_type,
        source_document_id=source_document_id,
        source_document_number=source_document_number,
        posted_by=user if getattr(user, "is_authenticated", False) else None,
        is_approved=is_approved,
        approval_status="Approved" if is_approved else "Pending",
        remarks=remarks,
    )

    if is_approved:
        recompute_lot_rate(client_id, lot_number)

    return entry


def recompute_lot_rate(client_id, lot_number):
    """Recompute the landed rate per meter and propagate to lot takas."""
    approved_entries = LotCostEntry.objects.filter(
        client_id=client_id,
        lot_number=lot_number,
        is_approved=True,
        deleted_at__isnull=True,
    )
    total_cost = approved_entries.aggregate(val=Sum("amount"))["val"] or ZERO
    total_cost = D(total_cost)

    takas = LotTaka.objects.filter(
        client_id=client_id,
        lot_number=lot_number,
        deleted_at__isnull=True,
    )

    # Use good finished or received meters
    finished_meters = (
        takas.filter(status__in=[LotTaka.STATUS_FINISHED, LotTaka.STATUS_AVAILABLE])
        .aggregate(val=Sum("received_quantity"))["val"]
        or ZERO
    )
    if finished_meters <= ZERO:
        finished_meters = takas.aggregate(val=Sum("received_quantity"))["val"] or ZERO

    finished_meters = D(finished_meters)
    unit_rate = round4(total_cost / finished_meters) if finished_meters > ZERO else ZERO

    if unit_rate > ZERO:
        takas.update(rate=unit_rate)

    return unit_rate


def get_lot_landed_cost(client_id, lot_number):
    """Return detailed landed cost breakdown for a lot."""
    entries = LotCostEntry.objects.filter(
        client_id=client_id,
        lot_number=lot_number,
        deleted_at__isnull=True,
    ).order_by("-date", "-created_at")

    breakdown = {
        "grey_purchase": ZERO,
        "purchase_freight": ZERO,
        "processing": ZERO,
        "processing_transport": ZERO,
        "reprocess": ZERO,
        "other_direct": ZERO,
        "scrap_recovery": ZERO,
    }

    type_mapping = {
        "Grey purchase": "grey_purchase",
        "Purchase freight/loading": "purchase_freight",
        "Dyeing/Printing/Finishing": "processing",
        "Processing transport": "processing_transport",
        "Reprocess": "reprocess",
        "Other direct": "other_direct",
        "Scrap recovery": "scrap_recovery",
    }

    total_cost = ZERO
    entry_list = []
    for entry in entries:
        if entry.is_approved:
            slot = type_mapping.get(entry.cost_type, "other_direct")
            breakdown[slot] += D(entry.amount)
            total_cost += D(entry.amount)
        entry_list.append({
            "id": str(entry.id),
            "lotNumber": entry.lot_number,
            "costType": entry.cost_type,
            "amount": str(round2(entry.amount)),
            "quantity": str(round2(entry.quantity)),
            "costPerMeter": str(round4(entry.cost_per_meter)),
            "sourceDocumentType": entry.source_document_type,
            "sourceDocumentNumber": entry.source_document_number,
            "date": entry.date.isoformat() if entry.date else None,
            "isApproved": entry.is_approved,
            "approvalStatus": entry.approval_status,
            "remarks": entry.remarks,
        })

    takas = LotTaka.objects.filter(
        client_id=client_id,
        lot_number=lot_number,
        deleted_at__isnull=True,
    )
    total_received_meters = takas.aggregate(val=Sum("received_quantity"))["val"] or ZERO
    total_available_meters = takas.aggregate(val=Sum("available_quantity"))["val"] or ZERO
    good_finished_meters = (
        takas.filter(status__in=[LotTaka.STATUS_FINISHED, LotTaka.STATUS_AVAILABLE])
        .aggregate(val=Sum("received_quantity"))["val"]
        or ZERO
    )
    if good_finished_meters <= ZERO:
        good_finished_meters = total_received_meters

    good_finished_meters = D(good_finished_meters)
    cost_per_meter = (
        round4(total_cost / good_finished_meters) if good_finished_meters > ZERO else ZERO
    )

    return {
        "lotNumber": lot_number,
        "totalCost": str(round2(total_cost)),
        "costPerMeter": str(cost_per_meter),
        "goodFinishedMeters": str(round2(good_finished_meters)),
        "totalReceivedMeters": str(round2(total_received_meters)),
        "totalAvailableMeters": str(round2(total_available_meters)),
        "breakdown": {k: str(round2(v)) for k, v in breakdown.items()},
        "entries": entry_list,
    }


def check_item_margin(client_id, item_id, selling_rate, lot_number=None, min_margin_pct=5):
    """
    Check selling rate against lot landed cost or item rate.
    Returns (below_cost: bool, below_margin: bool, landed_cost: Decimal, margin_pct: Decimal).
    """
    selling_rate = D(selling_rate or 0)
    landed_cost = ZERO

    if lot_number:
        info = get_lot_landed_cost(client_id, lot_number)
        landed_cost = D(info["costPerMeter"])

    if landed_cost <= ZERO and item_id:
        taka = LotTaka.objects.filter(
            client_id=client_id, item_id=item_id, deleted_at__isnull=True, rate__gt=0
        ).first()
        if taka:
            landed_cost = D(taka.rate)

    if landed_cost <= ZERO:
        return False, False, ZERO, Decimal("100.00")

    below_cost = selling_rate < landed_cost
    if selling_rate > ZERO:
        margin_pct = round2(((selling_rate - landed_cost) / selling_rate) * 100)
    else:
        margin_pct = Decimal("-100.00")

    below_margin = margin_pct < D(min_margin_pct)
    return below_cost, below_margin, landed_cost, margin_pct


def get_lot_profitability_report(client_id, lot_number=None, date_from=None, date_to=None):
    """
    Produce Lot Profitability report:
    Lot Cost, Sold Qty, Sales Value, Discount, Brokerage, Incentive, Freight, Returns -> Contribution.
    """
    from apps.sales.models import BrokerCommission, SalesInvoiceLine, SalesReturnLine

    qs = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if lot_number:
        qs = qs.filter(lot_number=lot_number)

    lot_numbers = sorted(list(set(qs.values_list("lot_number", flat=True))))
    report_rows = []

    for lot_no in lot_numbers:
        cost_info = get_lot_landed_cost(client_id, lot_no)
        unit_cost = D(cost_info["costPerMeter"])
        total_lot_cost = D(cost_info["totalCost"])

        # Sales lines matching lot preference or lot number
        sales_lines = SalesInvoiceLine.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            sales_invoice__status__in=["Unpaid", "Partially Paid", "Paid"],
        ).filter(Q(lot_preference=lot_no) | Q(item__lot_takas__lot_number=lot_no)).distinct()

        if date_from:
            sales_lines = sales_lines.filter(sales_invoice__doc_date__gte=date_from)
        if date_to:
            sales_lines = sales_lines.filter(sales_invoice__doc_date__lte=date_to)

        sold_meters = sales_lines.aggregate(val=Sum("qty"))["val"] or ZERO
        sales_value = sales_lines.aggregate(val=Sum("line_total"))["val"] or ZERO
        discounts = sales_lines.aggregate(val=Sum("discount_amount"))["val"] or ZERO

        sold_meters = D(sold_meters)
        sales_value = D(sales_value)
        discounts = D(discounts)

        # Cost of Goods Sold
        cogs = round2(sold_meters * unit_cost)

        # Brokerage attributed to these sales invoices
        invoice_ids = list(sales_lines.values_list("sales_invoice_id", flat=True).distinct())
        brokerage = ZERO
        if invoice_ids:
            comm_val = BrokerCommission.objects.filter(
                client_id=client_id, invoice_id__in=invoice_ids, deleted_at__isnull=True
            ).aggregate(val=Sum("commission_amount"))["val"] or ZERO
            brokerage = D(comm_val)

        # Returns
        return_lines = SalesReturnLine.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            sales_return__status="Posted",
        ).filter(Q(lot_preference=lot_no) | Q(item__lot_takas__lot_number=lot_no)).distinct()
        returns_val = return_lines.aggregate(val=Sum("line_total"))["val"] or ZERO
        returns_val = D(returns_val)

        # Net Sales = Sales Value - Returns
        net_sales = sales_value - returns_val
        # Net Contribution = Net Sales - COGS - Brokerage
        net_contribution = round2(net_sales - cogs - brokerage)
        margin_pct = (
            round2((net_contribution / net_sales) * 100) if net_sales > ZERO else ZERO
        )

        report_rows.append({
            "lotNumber": lot_no,
            "totalLotCost": str(round2(total_lot_cost)),
            "unitCost": str(unit_cost),
            "soldMeters": str(round2(sold_meters)),
            "salesValue": str(round2(sales_value)),
            "discounts": str(round2(discounts)),
            "returns": str(round2(returns_val)),
            "netSales": str(round2(net_sales)),
            "cogs": str(cogs),
            "brokerage": str(round2(brokerage)),
            "netContribution": str(net_contribution),
            "marginPct": str(margin_pct),
        })

    return report_rows
