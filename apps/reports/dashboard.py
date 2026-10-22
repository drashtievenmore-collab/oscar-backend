"""
Business dashboard (``GET /reports/sales-dashboard/``).

Every figure on the main business-position screen is computed here, from the
database, so the browser only renders. Nothing is estimated: a figure with no
source rows is zero (or ``None`` where a value cannot be determined, e.g. a
WIP value when the job-work materials carry no rate).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounting import reports as financial
from apps.core.money import ZERO, D, effective_due_date, round2, round4

MONEY = DecimalField(max_digits=18, decimal_places=2)
QTY = DecimalField(max_digits=18, decimal_places=4)


def _money(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


def _qty(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0")), output_field=QTY)


def _parse_day(value, default):
    if not value:
        return default
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# ---------------------------------------------------------------------------
# Daily business position
# ---------------------------------------------------------------------------
def _posted_invoices(client_id):
    from apps.sales.models import SalesInvoice

    return SalesInvoice.objects.filter(
        client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
    ).exclude(status__in=["Cancelled", "Draft"])


def _sales_by_day(client_id, days):
    rows = (
        _posted_invoices(client_id)
        .filter(doc_date__in=days)
        .values("doc_date")
        .annotate(value=_money("total"))
    )
    return {row["doc_date"]: round2(row["value"]) for row in rows}


def _collection_by_day(client_id, days):
    from apps.sales.models import PaymentIn

    rows = (
        PaymentIn.objects.filter(
            client_id=client_id, deleted_at__isnull=True, payment_date__in=days
        )
        .exclude(status="Cancelled")
        .values("payment_date")
        .annotate(value=_money("amount"))
    )
    return {row["payment_date"]: round2(row["value"]) for row in rows}


def _purchase_by_day(client_id, days):
    from apps.purchase.models import PurchaseBill

    rows = (
        PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True, doc_date__in=days
        )
        .exclude(status__in=["Cancelled", "Draft"])
        .values("doc_date")
        .annotate(value=_money("total"))
    )
    return {row["doc_date"]: round2(row["value"]) for row in rows}


def _jobwork_expense_by_day(client_id, days):
    """Job-work processing cost: accepted inward meters x the order's job rate,
    on the inward date (the day the processed fabric came back)."""
    from apps.jobwork.models import JWOInward

    rows = (
        JWOInward.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            date__in=days,
            job_work_order__deleted_at__isnull=True,
        )
        .values("date")
        .annotate(
            value=_money(F("accepted") * F("job_work_order__rate"), output_field=MONEY)
        )
    )
    return {row["date"]: round2(row["value"]) for row in rows}


def _journal_by_day(client_id, days, *, system_keys=None, exclude_keys=None, account_type=None):
    from apps.accounting.models import JournalLine

    queryset = JournalLine.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        journal_entry__entry_date__in=days,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    if system_keys is not None:
        queryset = queryset.filter(account__system_key__in=system_keys)
    if exclude_keys:
        queryset = queryset.exclude(account__system_key__in=exclude_keys)
    if account_type:
        queryset = queryset.filter(account__type=account_type)
    rows = queryset.values("journal_entry__entry_date").annotate(
        debit=_money("debit"), credit=_money("credit")
    )
    return {
        row["journal_entry__entry_date"]: (D(row["debit"]), D(row["credit"])) for row in rows
    }


def _transport_by_day(client_id, days):
    """Net debits to the Freight and Carriage account (5300)."""
    rows = _journal_by_day(client_id, days, system_keys=["freight"])
    return {day: round2(debit - credit) for day, (debit, credit) in rows.items()}


def _other_expense_by_day(client_id, days):
    """Posted expense vouchers (net of tax), excluding freight-coded ones that
    already appear under Transport."""
    from apps.purchase.models import Expense

    rows = (
        Expense.objects.filter(
            client_id=client_id, deleted_at__isnull=True, expense_date__in=days
        )
        .exclude(status__in=["Cancelled", "Draft"])
        .exclude(account__system_key="freight")
        .values("expense_date")
        .annotate(value=_money("amount"))
    )
    return {row["expense_date"]: round2(row["value"]) for row in rows}


def _gross_profit_by_day(client_id, days):
    """Sales revenue (net of returns) minus COGS, straight from the ledger."""
    revenue = _journal_by_day(client_id, days, system_keys=["sales", "sales_return"])
    cogs = _journal_by_day(client_id, days, system_keys=["cogs"])
    result = {}
    for day in days:
        r_debit, r_credit = revenue.get(day, (ZERO, ZERO))
        c_debit, c_credit = cogs.get(day, (ZERO, ZERO))
        result[day] = round2((r_credit - r_debit) - (c_debit - c_credit))
    return result


# ---------------------------------------------------------------------------
# Funds / stock / working capital
# ---------------------------------------------------------------------------
def stock_valuation(client_id):
    """On-hand qty x weighted average cost, per item, plus the per-stage split
    (Grey / Dyed / Printed / Finished) from the fabric-type master."""
    from apps.inventory.models import StockBalance
    from apps.masters.models import FabricQuality

    stage_by_quality = {}
    for name, code, stage in FabricQuality.objects.filter(
        client_id=client_id, deleted_at__isnull=True, fabric_type__isnull=False
    ).values_list("name", "code", "fabric_type__process_stage"):
        for key in (name, code):
            if key:
                stage_by_quality[str(key).strip().lower()] = stage

    per_item = {}
    for row in (
        StockBalance.objects.filter(client_id=client_id, item__deleted_at__isnull=True)
        .exclude(item__item_kind="Service")
        .values("item_id", "item__fabric_quality", "item__cost_price")
        .annotate(
            total_on_hand=_qty("on_hand"),
            inward_qty=_qty("inward_qty"),
            inward_value=_money("inward_value"),
        )
    ):
        on_hand = D(row["total_on_hand"])
        if on_hand <= ZERO:
            continue
        if D(row["inward_qty"]) > ZERO:
            unit = D(row["inward_value"]) / D(row["inward_qty"])
        else:
            unit = D(row["item__cost_price"])
        quality = str(row["item__fabric_quality"] or "").strip().lower()
        per_item[row["item_id"]] = {
            "qty": on_hand,
            "value": on_hand * unit,
            "stage": stage_by_quality.get(quality) or "Unclassified",
        }

    stages = {}
    for entry in per_item.values():
        bucket = stages.setdefault(entry["stage"], {"qty": ZERO, "value": ZERO})
        bucket["qty"] += entry["qty"]
        bucket["value"] += entry["value"]
    total = round2(sum((entry["value"] for entry in per_item.values()), ZERO))
    return total, {
        stage: {"qty": round4(v["qty"]), "value": round2(v["value"])}
        for stage, v in stages.items()
    }


def _lot_state_totals(client_id):
    """QC Hold / Reprocess / Rejected lots, valued at the lot's own rate."""
    from apps.inventory.models import LotTaka

    states = {
        "qcHold": ["Pending", "On Hold", "Hold", "QC Hold"],
        "reprocess": ["Reprocess"],
        "rejected": ["Rejected"],
    }
    result = {}
    base = LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, available_quantity__gt=0
    )
    for key, statuses in states.items():
        row = base.filter(qc_status__in=statuses).aggregate(
            qty=_qty("available_quantity"),
            value=_money(F("available_quantity") * F("rate"), output_field=MONEY),
            lots=Count("id"),
        )
        result[key] = {
            "qty": round4(row["qty"]),
            "value": round2(row["value"]),
            "lots": row["lots"],
        }
    return result


def _external_wip(client_id):
    """Meters out at job-work vendors (outward - inward) on open orders."""
    from apps.jobwork.models import JobWorkOrder

    orders = (
        JobWorkOrder.objects.filter(
            client_id=client_id, deleted_at__isnull=True, status="In-Process"
        )
        .annotate(
            out_qty=_qty("outwards__qty", filter=Q(outwards__deleted_at__isnull=True)),
        )
        .values("id", "process", "out_qty")
    )
    from apps.jobwork.models import JWOInward, JWOMaterial

    inward = {
        row["job_work_order_id"]: D(row["qty"])
        for row in JWOInward.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .values("job_work_order_id")
        .annotate(qty=_qty("qty"))
    }
    material_rate = {}
    for row in (
        JWOMaterial.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .values("job_work_order_id")
        .annotate(qty=_qty("qty"), value=_money("amount"))
    ):
        if D(row["qty"]) > ZERO and D(row["value"]) > ZERO:
            material_rate[row["job_work_order_id"]] = D(row["value"]) / D(row["qty"])

    by_process = {}
    total_qty = ZERO
    total_value = ZERO
    valued = True
    for order in orders:
        pending = D(order["out_qty"]) - inward.get(order["id"], ZERO)
        if pending <= ZERO:
            continue
        process = order["process"] or "Unspecified"
        by_process[process] = by_process.get(process, ZERO) + pending
        total_qty += pending
        rate = material_rate.get(order["id"])
        if rate is None:
            valued = False
        else:
            total_value += pending * rate
    return {
        "qty": round4(total_qty),
        # None when some pending fabric has no material rate to value it at.
        "value": round2(total_value) if valued else None,
        "byProcess": [
            {"process": process, "qty": round4(qty)}
            for process, qty in sorted(by_process.items(), key=lambda kv: -kv[1])
        ],
    }


def _working_capital(client_id, funds, stock_value):
    """Funds + stock + other current assets (debtors, GST input) - current
    liabilities (creditors, GST payable, salaries payable), from the ledger."""
    from apps.accounting.models import JournalLine

    rows = (
        JournalLine.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
        .filter(
            Q(account__type="Asset", account__subtype__in=["Receivable", "Tax"])
            | Q(account__type="Liability", account__subtype__in=["Payable", "Tax"])
        )
        .values("account__type")
        .annotate(debit=_money("debit"), credit=_money("credit"))
    )
    assets = ZERO
    liabilities = ZERO
    for row in rows:
        if row["account__type"] == "Asset":
            assets += D(row["debit"]) - D(row["credit"])
        else:
            liabilities += D(row["credit"]) - D(row["debit"])
    current_assets = round2(D(funds) + D(stock_value) + assets)
    return {
        "currentAssets": current_assets,
        "currentLiabilities": round2(liabilities),
        "workingCapital": round2(current_assets - liabilities),
    }


# ---------------------------------------------------------------------------
# Production / QC
# ---------------------------------------------------------------------------
def _production(client_id, day):
    from apps.pms.models import DailyProductionEntry, ProductionInstruction

    active = ProductionInstruction.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status__in=["In Progress", "Completed"]
    )
    planned = D(active.aggregate(value=_qty("order_meter"))["value"])
    produced = D(
        DailyProductionEntry.objects.filter(
            client_id=client_id, deleted_at__isnull=True, instruction__in=active
        ).aggregate(value=_qty("meters"))["value"]
    )
    missing = (
        active.filter(status="In Progress")
        .exclude(
            id__in=DailyProductionEntry.objects.filter(
                client_id=client_id, deleted_at__isnull=True, entry_date=day
            ).values("instruction_id")
        )
        .count()
    )
    pending = planned - produced
    return {
        "instructions": active.count(),
        "plannedMeters": round4(planned),
        "producedMeters": round4(produced),
        "pendingMeters": round4(pending if pending > ZERO else ZERO),
        "completionPct": (
            round(float(min(produced / planned, Decimal("1")) * 100), 1) if planned > ZERO else None
        ),
        "missingUpdates": missing,
    }


def _qc_wastage(client_id, since):
    """Job-work inwards since ``since``: received / accepted / rejected,
    reprocess sent, and process loss on finished orders."""
    from apps.jobwork.models import JobWorkOrder, JWOInward, JWOOutward, JWOReprocess

    inwards = JWOInward.objects.filter(
        client_id=client_id, deleted_at__isnull=True, date__gte=since,
        job_work_order__deleted_at__isnull=True,
    )
    totals = inwards.aggregate(
        received=_qty("qty"), good=_qty("accepted"), reject=_qty("rejected")
    )
    reprocess = D(
        JWOReprocess.objects.filter(
            client_id=client_id, deleted_at__isnull=True, date__gte=since
        ).aggregate(value=_qty("qty"))["value"]
    )
    finished_ids = list(
        JobWorkOrder.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            status__in=["Completed", "Closed"],
            inwards__date__gte=since,
        ).values_list("id", flat=True).distinct()
    )
    out_total = D(
        JWOOutward.objects.filter(
            client_id=client_id, deleted_at__isnull=True, job_work_order_id__in=finished_ids
        ).aggregate(value=_qty("qty"))["value"]
    )
    in_total = D(
        JWOInward.objects.filter(
            client_id=client_id, deleted_at__isnull=True, job_work_order_id__in=finished_ids
        ).aggregate(value=_qty("qty"))["value"]
    )
    loss = out_total - in_total
    received = D(totals["received"])
    good = D(totals["good"])
    return {
        "since": since,
        "received": round4(received),
        "good": round4(good),
        "reprocess": round4(reprocess),
        "reject": round4(D(totals["reject"])),
        "loss": round4(loss if loss > ZERO else ZERO),
        "yieldPct": round(float(good / received * 100), 1) if received > ZERO else None,
        "lossPct": (
            round(float(max(loss, ZERO) / out_total * 100), 1) if out_total > ZERO else None
        ),
        "rejectedInwards": inwards.filter(rejected__gt=0).count(),
        "pendingReprocess": JWOReprocess.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Received", "Closed", "Cancelled", "Completed"]).count(),
    }


# ---------------------------------------------------------------------------
# Attention counts
# ---------------------------------------------------------------------------
def _attention(client_id, day, production, qc):
    from apps.inventory import services as stock
    from apps.jobwork.models import JobWorkOrder
    from apps.inventory.models import LotTaka
    from apps.masters.models import Item, Party
    from apps.purchase.models import VendorBill

    overdue = 0
    for invoice in _posted_invoices(client_id).only(
        "doc_date", "due_date", "credit_days", "total", "amount_paid"
    ):
        if D(invoice.total) - D(invoice.amount_paid) <= ZERO:
            continue
        due = effective_due_date(invoice.doc_date, invoice.due_date, invoice.credit_days)
        if due and due < day:
            overdue += 1

    credit_exceeded = Party.objects.filter(
        client_id=client_id, deleted_at__isnull=True, credit_limit__gt=0,
        balance__gt=F("credit_limit"),
    ).count()

    items = list(
        Item.objects.filter(client_id=client_id, deleted_at__isnull=True, reorder_level__gt=0)
        .exclude(item_kind="Service")
        .only("id", "reorder_level")
    )
    stock.annotate_items_with_stock(client_id, items)
    below_reorder = sum(
        1 for item in items if D(getattr(item, "available_qty", 0)) <= D(item.reorder_level)
    )

    delayed_jobwork = JobWorkOrder.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="In-Process",
        expected_completion__lt=day,
    ).count()

    reprocess_lots = LotTaka.objects.filter(
        client_id=client_id, deleted_at__isnull=True, qc_status="Reprocess",
        available_quantity__gt=0,
    ).count()

    unmatched_vendor_bills = VendorBill.objects.filter(
        client_id=client_id, deleted_at__isnull=True, match_status="Pending Matching"
    ).exclude(status="Rejected").count()

    return [
        {"key": "productionUpdatesMissing", "label": "Production updates missing today",
         "count": production["missingUpdates"], "link": "/pms/daily-production"},
        {"key": "jobWorkDelayed", "label": "Job works past expected completion",
         "count": delayed_jobwork, "link": "/job-work/orders"},
        {"key": "rejectedInwards", "label": "Job-work inwards with rejections (30 days)",
         "count": qc["rejectedInwards"], "link": "/job-work/orders"},
        {"key": "reprocessLots", "label": "Lots marked for reprocess",
         "count": reprocess_lots + qc["pendingReprocess"], "link": "/inventory/lot-register"},
        {"key": "creditExceeded", "label": "Customers over credit limit",
         "count": credit_exceeded, "link": "/crm/customers"},
        {"key": "overdueReceivables", "label": "Overdue receivables",
         "count": overdue, "link": "/sales/invoices"},
        {"key": "vendorBillsUnmatched", "label": "Vendor bills pending matching",
         "count": unmatched_vendor_bills, "link": "/purchase/vendor-bills"},
        {"key": "belowReorder", "label": "Items at or below reorder level",
         "count": below_reorder, "link": "/inventory/stock-position"},
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def business_dashboard(client_id, *, day=None):
    from apps.accounting.services import account_balances

    today = timezone.localdate()
    day = _parse_day(day, today)
    prev = day - timedelta(days=1)
    week = [day - timedelta(days=offset) for offset in range(6, -1, -1)]
    days = sorted(set(week) | {prev})

    sales = _sales_by_day(client_id, days)
    collection = _collection_by_day(client_id, days)
    purchase = _purchase_by_day(client_id, days)
    jobwork = _jobwork_expense_by_day(client_id, days)
    transport = _transport_by_day(client_id, days)
    other = _other_expense_by_day(client_id, days)
    gross = _gross_profit_by_day(client_id, [day, prev])

    def pair(source):
        return {"current": source.get(day, ZERO), "previous": source.get(prev, ZERO)}

    banks = account_balances(client_id)
    funds = round2(sum((D(row["balance"]) for row in banks), ZERO))
    stock_value, stages = stock_valuation(client_id)
    lot_states = _lot_state_totals(client_id)
    wip = _external_wip(client_id)
    production = _production(client_id, day)
    qc = _qc_wastage(client_id, day - timedelta(days=30))
    ageing = financial.ageing(client_id, "receivable", day)

    finished_stages = ("Dyed", "Printed", "Finished")
    finished = {
        "qty": round4(sum((stages[s]["qty"] for s in finished_stages if s in stages), ZERO)),
        "value": round2(sum((stages[s]["value"] for s in finished_stages if s in stages), ZERO)),
    }

    return {
        "date": day,
        "previousDate": prev,
        "kpis": {
            "sales": pair(sales),
            "collection": pair(collection),
            "purchase": pair(purchase),
            "jobWorkExpense": pair(jobwork),
            "transport": pair(transport),
            "otherExpense": pair(other),
            "grossProfit": pair(gross),
        },
        "funds": {"total": funds, "accounts": banks},
        "stockValue": stock_value,
        "workingCapital": _working_capital(client_id, funds, stock_value),
        "inventoryPosition": {
            "grey": stages.get("Grey", {"qty": ZERO, "value": ZERO}),
            "finished": finished,
            "unclassified": stages.get("Unclassified", {"qty": ZERO, "value": ZERO}),
            "externalWip": {"qty": wip["qty"], "value": wip["value"]},
            "qcHold": lot_states["qcHold"],
            "reprocess": lot_states["reprocess"],
            "rejected": lot_states["rejected"],
        },
        "production": production,
        "jobWorkInProcess": wip["byProcess"],
        "qc": qc,
        "salesTrend": [
            {"date": d, "sales": sales.get(d, ZERO), "collection": collection.get(d, ZERO)}
            for d in week
        ],
        "receivableAgeing": {
            "buckets": ageing["buckets"],
            "bucketOrder": ageing["bucketOrder"],
            "total": ageing["total"],
        },
        "attention": _attention(client_id, day, production, qc),
    }
