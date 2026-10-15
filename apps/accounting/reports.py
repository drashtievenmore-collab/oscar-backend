"""
Financial reports (api.md §8, §12).

Every figure here is a query over ``journal_lines`` -- there is no reporting
table, because two sources of truth for money is where reconciliation bugs come
from (db.md §8).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.core.money import ZERO, D, ageing_bucket, round2

from .models import Account, JournalEntry, JournalLine

MONEY = DecimalField(max_digits=18, decimal_places=2)


def _sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


def _live_lines(client_id, date_from=None, date_to=None):
    queryset = JournalLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(journal_entry__status="Reversed")
    if date_from:
        queryset = queryset.filter(journal_entry__entry_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(journal_entry__entry_date__lte=date_to)
    return queryset


def _by_type(client_id, account_type, date_from=None, date_to=None):
    rows = (
        _live_lines(client_id, date_from, date_to)
        .filter(account__type=account_type)
        .values("account_id", "account__code", "account__name", "account__system_key")
        .annotate(debit=_sum("debit"), credit=_sum("credit"))
        .order_by("account__code")
    )
    return list(rows)


def profit_and_loss(client_id, date_from=None, date_to=None):
    """Sales revenue, COGS, gross profit, operating expenses, EBITDA, margin %.

    api.md §12 names these five figures explicitly as what the frontend
    computes today.
    """
    income = _by_type(client_id, "Income", date_from, date_to)
    expense = _by_type(client_id, "Expense", date_from, date_to)

    revenue = round2(sum((row["credit"] - row["debit"] for row in income), ZERO))
    cogs = round2(
        sum(
            (
                row["debit"] - row["credit"]
                for row in expense
                if row["account__system_key"] in ("cogs", "purchases")
            ),
            ZERO,
        )
    )
    operating = round2(
        sum(
            (
                row["debit"] - row["credit"]
                for row in expense
                if row["account__system_key"] not in ("cogs", "purchases")
            ),
            ZERO,
        )
    )
    gross_profit = round2(revenue - cogs)
    net_operating = round2(gross_profit - operating)
    margin = (
        round2(gross_profit / revenue * Decimal("100")) if revenue > ZERO else ZERO
    )

    return {
        "periodFrom": date_from,
        "periodTo": date_to,
        "revenue": revenue,
        "cogs": cogs,
        "grossProfit": gross_profit,
        "operatingExpenses": operating,
        "netOperatingProfit": net_operating,
        "grossMarginPct": margin,
        "incomeAccounts": [
            {
                "accountId": str(row["account_id"]),
                "code": row["account__code"],
                "name": row["account__name"],
                "amount": round2(row["credit"] - row["debit"]),
            }
            for row in income
        ],
        "expenseAccounts": [
            {
                "accountId": str(row["account_id"]),
                "code": row["account__code"],
                "name": row["account__name"],
                "amount": round2(row["debit"] - row["credit"]),
            }
            for row in expense
        ],
    }


def balance_sheet(client_id, as_of=None):
    as_of = as_of or timezone.localdate()

    def side(account_type, natural_debit):
        rows = _by_type(client_id, account_type, None, as_of)
        entries = []
        total = ZERO
        for row in rows:
            amount = (
                round2(row["debit"] - row["credit"])
                if natural_debit
                else round2(row["credit"] - row["debit"])
            )
            total += amount
            entries.append(
                {
                    "accountId": str(row["account_id"]),
                    "code": row["account__code"],
                    "name": row["account__name"],
                    "amount": amount,
                }
            )
        return entries, round2(total)

    assets, total_assets = side("Asset", True)
    liabilities, total_liabilities = side("Liability", False)
    equity, total_equity = side("Equity", False)

    pnl = profit_and_loss(client_id, None, as_of)
    retained = pnl["netOperatingProfit"]

    return {
        "asOf": as_of,
        "assets": assets,
        "totalAssets": total_assets,
        "liabilities": liabilities,
        "totalLiabilities": total_liabilities,
        "equity": equity,
        "totalEquity": round2(total_equity + retained),
        "retainedEarnings": retained,
        # A non-zero difference means an unbalanced posting slipped through and
        # is worth surfacing rather than hiding.
        "difference": round2(
            total_assets - (total_liabilities + total_equity + retained)
        ),
    }


def cash_flow(client_id, date_from=None, date_to=None):
    """Period movement across bank and cash accounts."""
    rows = (
        _live_lines(client_id, date_from, date_to)
        .filter(account__subtype="Bank")
        .annotate(month=TruncMonth("journal_entry__entry_date"))
        .values("month")
        .annotate(inflow=_sum("debit"), outflow=_sum("credit"))
        .order_by("month")
    )
    periods = [
        {
            "period": row["month"],
            "inflow": round2(row["inflow"]),
            "outflow": round2(row["outflow"]),
            "net": round2(row["inflow"] - row["outflow"]),
        }
        for row in rows
    ]
    return {
        "periods": periods,
        "totalInflow": round2(sum((p["inflow"] for p in periods), ZERO)),
        "totalOutflow": round2(sum((p["outflow"] for p in periods), ZERO)),
        "netCashFlow": round2(sum((p["net"] for p in periods), ZERO)),
    }


def gst_summary(client_id, date_from=None, date_to=None):
    """GSTR-1 / GSTR-3B style output (api.md §8)."""
    from apps.purchase.models import PurchaseBill
    from apps.sales.models import SalesInvoice

    outward = SalesInvoice.objects.filter(
        client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
    ).exclude(status="Cancelled")
    inward = PurchaseBill.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(status__in=["Cancelled", "Draft"])

    if date_from:
        outward = outward.filter(doc_date__gte=date_from)
        inward = inward.filter(doc_date__gte=date_from)
    if date_to:
        outward = outward.filter(doc_date__lte=date_to)
        inward = inward.filter(doc_date__lte=date_to)

    out_rows = outward.aggregate(
        taxable=_sum("taxable_value"), cgst=_sum("cgst"), sgst=_sum("sgst"),
        igst=_sum("igst"), cess=_sum("cess"), count=Count("id"),
    )
    in_rows = inward.aggregate(
        taxable=_sum("taxable_value"), cgst=_sum("cgst"), sgst=_sum("sgst"),
        igst=_sum("igst"), cess=_sum("cess"), count=Count("id"),
    )

    output_tax = round2(out_rows["cgst"] + out_rows["sgst"] + out_rows["igst"] + out_rows["cess"])
    input_tax = round2(in_rows["cgst"] + in_rows["sgst"] + in_rows["igst"] + in_rows["cess"])

    by_hsn = (
        outward.values("line_items__hsn_code")
        .annotate(
            taxable=_sum("line_items__amount"),
            tax=_sum("line_items__tax_amount"),
            count=Count("line_items__id"),
        )
        .order_by("-taxable")
    )

    return {
        "periodFrom": date_from,
        "periodTo": date_to,
        "outward": {**{k: round2(v) for k, v in out_rows.items() if k != "count"},
                    "count": out_rows["count"], "totalTax": output_tax},
        "inward": {**{k: round2(v) for k, v in in_rows.items() if k != "count"},
                   "count": in_rows["count"], "totalTax": input_tax},
        "netPayable": round2(output_tax - input_tax),
        "byHsn": [
            {
                "hsnCode": row["line_items__hsn_code"],
                "taxableValue": round2(row["taxable"]),
                "tax": round2(row["tax"]),
                "lineCount": row["count"],
            }
            for row in by_hsn
            if row["line_items__hsn_code"]
        ],
    }


def ageing(client_id, kind="receivable", as_of=None):
    """``?type=receivable|payable``, bucketed Current / 0-30 / 31-60 / 61-90 / 91-120 / 120+."""
    from datetime import timedelta
    from apps.purchase.models import PurchaseBill
    from apps.sales.models import SalesInvoice

    as_of = as_of or timezone.localdate()

    if kind == "payable":
        documents = PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft", "Paid"]).select_related("party")
        number_field = "bill_number"
    else:
        documents = SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
        ).exclude(status__in=["Cancelled", "Draft", "Paid"]).select_related("party")
        number_field = "invoice_number"

    buckets = {"Current": ZERO, "0-30": ZERO, "31-60": ZERO, "61-90": ZERO, "91-120": ZERO, "120+": ZERO}
    rows = []
    for document in documents:
        outstanding = round2(D(document.total) - D(document.amount_paid))
        if outstanding <= ZERO:
            continue
        due_date = document.due_date
        if not due_date and document.doc_date:
            credit_days = getattr(document, "credit_days", 0) or 0
            due_date = document.doc_date + timedelta(days=int(credit_days))

        days = (as_of - due_date).days if due_date else 0
        if days <= 0:
            bucket = "Current"
        elif days <= 30:
            bucket = "0-30"
        elif days <= 60:
            bucket = "31-60"
        elif days <= 90:
            bucket = "61-90"
        elif days <= 120:
            bucket = "91-120"
        else:
            bucket = "120+"

        buckets[bucket] = round2(buckets[bucket] + outstanding)
        rows.append(
            {
                "documentId": str(document.id),
                "documentNumber": getattr(document, number_field),
                "partyId": str(document.party_id),
                "partyName": document.party_name or (document.party.name if document.party else ""),
                "date": document.doc_date,
                "dueDate": due_date,
                "total": round2(document.total),
                "outstanding": outstanding,
                "daysOverdue": max(days, 0),
                "bucket": bucket,
            }
        )

    rows.sort(key=lambda row: row["daysOverdue"], reverse=True)
    return {
        "type": kind,
        "asOf": as_of,
        "buckets": buckets,
        "total": round2(sum(buckets.values(), ZERO)),
        "rows": rows,
    }
