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

from apps.core.money import AGEING_BUCKETS, ZERO, D, ageing_bucket, round2

from .models import Account, JournalEntry, JournalLine

MONEY = DecimalField(max_digits=18, decimal_places=2)


def _sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


def _live_lines(client_id, date_from=None, date_to=None):
    queryset = JournalLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
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
    """``?type=receivable|payable`` -- the single ageing report.

    Buckets come from ``apps.core.money.AGEING_BUCKETS`` (0-30 / 31-60 / 61-90 /
    91-120 / 120+), computed on the **outstanding** amount (total - payments -
    posted credit/debit notes). A document without a due date ages from its
    document date + credit days.
    """
    from apps.core.money import days_overdue, effective_due_date, empty_ageing_buckets
    from apps.purchase.models import PurchaseBill
    from apps.sales.models import SalesInvoice

    if isinstance(as_of, str) and as_of:
        as_of = date.fromisoformat(as_of[:10])
    as_of = as_of or timezone.localdate()

    returns_sum = Coalesce(
        Sum("returns__total", filter=Q(returns__deleted_at__isnull=True)
            & ~Q(returns__status="Cancelled")),
        Value(Decimal("0.00")),
        output_field=MONEY,
    )
    if kind == "payable":
        documents = PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft"])
        number_field = "bill_number"
    else:
        kind = "receivable"
        documents = SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
        ).exclude(status__in=["Cancelled", "Draft"])
        number_field = "invoice_number"
    documents = documents.filter(doc_date__lte=as_of).select_related("party").annotate(
        returned_total=returns_sum
    )

    buckets = empty_ageing_buckets()
    rows = []
    for document in documents:
        outstanding = round2(
            D(document.total) - D(document.amount_paid) - D(document.returned_total)
        )
        if outstanding <= ZERO:
            continue
        credit_days = getattr(document, "credit_days", None)
        due_date = effective_due_date(document.doc_date, document.due_date, credit_days)
        days = days_overdue(as_of, document.doc_date, document.due_date, credit_days)
        bucket = ageing_bucket(days)

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
                "daysOverdue": days,
                "bucket": bucket,
            }
        )

    rows.sort(key=lambda row: row["daysOverdue"], reverse=True)
    return {
        "type": kind,
        "asOf": as_of,
        "buckets": buckets,
        "bucketOrder": list(AGEING_BUCKETS),
        "total": round2(sum(buckets.values(), ZERO)),
        "rows": rows,
    }


def day_book(client_id, as_of_date=None):
    """Daily Business Position / Day Book (PDF §14).

    Sales, collections, purchases, processing expenses, freight, cash/bank balances,
    receivables, payables, and current stock value. Zero dummy data.
    """
    from apps.inventory.models import StockBalance, Trip
    from apps.purchase.models import PaymentOut, PurchaseBill
    from apps.sales.models import PaymentIn, SalesInvoice

    if isinstance(as_of_date, str) and as_of_date:
        as_of = date.fromisoformat(as_of_date[:10])
    else:
        as_of = as_of_date or timezone.localdate()

    # 1. Sales on as_of
    sales_qs = SalesInvoice.objects.filter(
        client_id=client_id, doc_date=as_of, deleted_at__isnull=True
    ).exclude(status__in=["Cancelled", "Draft"]).select_related("party")
    sales_total = sales_qs.aggregate(val=_sum("total"))["val"]
    sales_taxable = sales_qs.aggregate(val=_sum("taxable_value"))["val"]
    sales_tax = sales_qs.aggregate(val=_sum("total_tax"))["val"]
    sales_vouchers = [
        {
            "id": str(s.id),
            "voucherNumber": s.invoice_number,
            "partyName": s.party_name or (s.party.name if s.party else ""),
            "amount": round2(s.total),
            "taxable": round2(s.taxable_value),
            "tax": round2(s.total_tax),
            "status": s.status,
        }
        for s in sales_qs.order_by("-created_at")
    ]

    # 2. Collections (Payments In)
    payments_in_qs = PaymentIn.objects.filter(
        client_id=client_id, payment_date=as_of, deleted_at__isnull=True, status="Active"
    ).select_related("party", "bank_account")
    collection_total = payments_in_qs.aggregate(val=_sum("amount"))["val"]
    collection_vouchers = [
        {
            "id": str(p.id),
            "voucherNumber": p.payment_number,
            "partyName": p.party.name if p.party else "",
            "amount": round2(p.amount),
            "mode": p.mode,
            "bank": p.bank_account.name if p.bank_account else None,
        }
        for p in payments_in_qs.order_by("-created_at")
    ]

    # 3. Purchases (Bills)
    bills_qs = PurchaseBill.objects.filter(
        client_id=client_id, doc_date=as_of, deleted_at__isnull=True
    ).exclude(status__in=["Cancelled", "Draft"]).select_related("party")
    purchase_total = bills_qs.aggregate(val=_sum("total"))["val"]
    purchase_vouchers = [
        {
            "id": str(b.id),
            "voucherNumber": b.bill_number,
            "partyName": b.party_name or (b.party.name if b.party else ""),
            "amount": round2(b.total),
            "taxable": round2(b.taxable_value),
            "status": b.status,
        }
        for b in bills_qs.order_by("-created_at")
    ]

    # 4. Disbursements (Payments Out)
    payments_out_qs = PaymentOut.objects.filter(
        client_id=client_id, payment_date=as_of, deleted_at__isnull=True, status="Active"
    ).select_related("party", "bank_account")
    disbursement_total = payments_out_qs.aggregate(val=_sum("amount"))["val"]
    disbursement_vouchers = [
        {
            "id": str(p.id),
            "voucherNumber": p.payment_number,
            "partyName": p.party.name if p.party else "",
            "amount": round2(p.amount),
            "mode": p.mode,
            "bank": p.bank_account.name if p.bank_account else None,
        }
        for p in payments_out_qs.order_by("-created_at")
    ]

    # 5. Freight & Transport trips on as_of
    trips_qs = Trip.objects.filter(client_id=client_id, trip_date=as_of, deleted_at__isnull=True)
    freight_total = trips_qs.aggregate(val=_sum("freight_amount"))["val"]
    trip_vouchers = [
        {
            "id": str(t.id),
            "voucherNumber": t.trip_number,
            "vehicleNumber": t.vehicle_number,
            "amount": round2(t.total_charges),
            "paidBy": t.paid_by,
        }
        for t in trips_qs.order_by("-created_at")
    ]

    # 6. Cash and Bank Balances
    bank_lines = JournalLine.objects.filter(
        client_id=client_id,
        account__type="Asset",
        account__subtype="Bank",
        journal_entry__entry_date__lte=as_of,
        deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    cash_bank_balance = round2(
        bank_lines.aggregate(val=Coalesce(Sum("debit") - Sum("credit"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    )

    # 7. Total Receivables and Payables as of as_of
    debtors_lines = JournalLine.objects.filter(
        client_id=client_id,
        account__system_key="debtors",
        journal_entry__entry_date__lte=as_of,
        deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    total_receivable = round2(
        debtors_lines.aggregate(val=Coalesce(Sum("debit") - Sum("credit"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    )

    creditors_lines = JournalLine.objects.filter(
        client_id=client_id,
        account__system_key="creditors",
        journal_entry__entry_date__lte=as_of,
        deleted_at__isnull=True,
    ).exclude(journal_entry__status="Reversed").exclude(journal_entry__reversal_of__isnull=False)
    total_payable = round2(
        creditors_lines.aggregate(val=Coalesce(Sum("credit") - Sum("debit"), Value(Decimal("0.00")), output_field=MONEY))["val"]
    )

    # 8. Stock Valuation as of as_of
    stock_balances = StockBalance.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(item__item_kind="Service")
    stock_value = round2(
        sum(
            (
                D(b.on_hand) * D(b.weighted_average_cost or b.item.cost_price or 0)
                for b in stock_balances.select_related("item")
            ),
            ZERO,
        )
    )

    return {
        "asOf": as_of.isoformat(),
        "summary": {
            "salesTotal": round2(sales_total),
            "collectionTotal": round2(collection_total),
            "purchaseTotal": round2(purchase_total),
            "disbursementTotal": round2(disbursement_total),
            "freightTotal": round2(freight_total),
            "cashBankBalance": cash_bank_balance,
            "totalReceivable": total_receivable,
            "totalPayable": total_payable,
            "stockValue": stock_value,
        },
        "salesVouchers": sales_vouchers,
        "collectionVouchers": collection_vouchers,
        "purchaseVouchers": purchase_vouchers,
        "disbursementVouchers": disbursement_vouchers,
        "tripVouchers": trip_vouchers,
    }


def salesperson_broker_overdue_view(client_id, as_of=None):
    """Overdue invoices grouped by Salesperson and Broker (PDF §14)."""
    from apps.core.money import days_overdue, effective_due_date
    from apps.sales.models import SalesInvoice

    if isinstance(as_of, str) and as_of:
        as_of = date.fromisoformat(as_of[:10])
    else:
        as_of = as_of or timezone.localdate()

    returns_sum = Coalesce(
        Sum("returns__total", filter=Q(returns__deleted_at__isnull=True) & ~Q(returns__status="Cancelled")),
        Value(Decimal("0.00")),
        output_field=MONEY,
    )
    invoices = (
        SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
        )
        .exclude(status__in=["Cancelled", "Draft"])
        .filter(doc_date__lte=as_of)
        .select_related("party", "salesperson_fk", "broker_fk")
        .annotate(returned_total=returns_sum)
    )

    by_salesperson = {}
    by_broker = {}

    for inv in invoices:
        outstanding = round2(D(inv.total) - D(inv.amount_paid) - D(inv.returned_total))
        if outstanding <= ZERO:
            continue
        credit_days = getattr(inv, "credit_days", None)
        days = days_overdue(as_of, inv.doc_date, inv.due_date, credit_days)
        if days <= 0:
            continue

        sp_key = str(inv.salesperson_fk_id) if inv.salesperson_fk_id else (inv.salesperson or "Unassigned")
        sp_name = inv.salesperson_fk.name if inv.salesperson_fk else (inv.salesperson or "Unassigned")
        sp_group = by_salesperson.setdefault(sp_key, {
            "id": sp_key,
            "name": sp_name,
            "totalOverdue": ZERO,
            "invoiceCount": 0,
            "maxDaysOverdue": 0,
            "invoices": [],
        })
        sp_group["totalOverdue"] += outstanding
        sp_group["invoiceCount"] += 1
        sp_group["maxDaysOverdue"] = max(sp_group["maxDaysOverdue"], days)
        sp_group["invoices"].append({
            "id": str(inv.id),
            "invoiceNumber": inv.invoice_number,
            "partyName": inv.party_name or (inv.party.name if inv.party else ""),
            "outstanding": float(outstanding),
            "daysOverdue": days,
        })

        br_key = str(inv.broker_fk_id) if inv.broker_fk_id else (inv.broker or "Direct / No Broker")
        br_name = inv.broker_fk.name if inv.broker_fk else (inv.broker or "Direct / No Broker")
        br_group = by_broker.setdefault(br_key, {
            "id": br_key,
            "name": br_name,
            "totalOverdue": ZERO,
            "invoiceCount": 0,
            "maxDaysOverdue": 0,
            "invoices": [],
        })
        br_group["totalOverdue"] += outstanding
        br_group["invoiceCount"] += 1
        br_group["maxDaysOverdue"] = max(br_group["maxDaysOverdue"], days)
        br_group["invoices"].append({
            "id": str(inv.id),
            "invoiceNumber": inv.invoice_number,
            "partyName": inv.party_name or (inv.party.name if inv.party else ""),
            "outstanding": float(outstanding),
            "daysOverdue": days,
        })

    sp_list = sorted(
        [
            {**v, "totalOverdue": float(round2(v["totalOverdue"]))}
            for v in by_salesperson.values()
        ],
        key=lambda x: x["totalOverdue"],
        reverse=True,
    )
    br_list = sorted(
        [
            {**v, "totalOverdue": float(round2(v["totalOverdue"]))}
            for v in by_broker.values()
        ],
        key=lambda x: x["totalOverdue"],
        reverse=True,
    )

    return {
        "asOf": as_of.isoformat(),
        "bySalesperson": sp_list,
        "byBroker": br_list,
        "totalOverdue": float(round2(sum((s["totalOverdue"] for s in sp_list), ZERO))),
    }


def sub_ledger_summary(client_id, ledger_type="jobwork", as_of=None):
    """Sub-ledgers for Job-work payable, Brokerage payable, and Incentive payable (PDF §14)."""
    if ledger_type == "brokerage":
        from apps.sales.models import BrokerCommission
        qs = BrokerCommission.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related("broker")
        by_broker = {}
        for bc in qs:
            br_name = bc.broker.name if bc.broker else "Direct / Unassigned"
            br_id = str(bc.broker_id) if bc.broker_id else "none"
            group = by_broker.setdefault(br_id, {
                "id": br_id,
                "name": br_name,
                "accrued": ZERO,
                "approved": ZERO,
                "payable": ZERO,
                "paid": ZERO,
                "balance": ZERO,
            })
            amt = D(bc.commission_amount)
            if bc.status == "Accrued":
                group["accrued"] += amt
            elif bc.status == "Approved":
                group["approved"] += amt
            elif bc.status == "Payable":
                group["payable"] += amt
                group["balance"] += amt
            elif bc.status == "Paid":
                group["paid"] += amt
        rows = [
            {
                "id": k,
                "name": v["name"],
                "accrued": float(round2(v["accrued"])),
                "approved": float(round2(v["approved"])),
                "payable": float(round2(v["payable"])),
                "paid": float(round2(v["paid"])),
                "balance": float(round2(v["balance"])),
            }
            for k, v in by_broker.items()
        ]
        return {"ledgerType": "brokerage", "rows": rows}

    elif ledger_type == "incentive":
        from apps.pms.models import IncentiveCalculation
        qs = IncentiveCalculation.objects.filter(client_id=client_id, deleted_at__isnull=True).select_related("employee")
        by_emp = {}
        for inc in qs:
            emp_name = inc.employee.name if inc.employee else "Unknown"
            emp_id = str(inc.employee_id)
            group = by_emp.setdefault(emp_id, {
                "id": emp_id,
                "name": emp_name,
                "calculated": ZERO,
                "approved": ZERO,
                "posted": ZERO,
                "hold": ZERO,
                "paid": ZERO,
                "balance": ZERO,
            })
            amt = D(inc.incentive_amount)
            hold = D(inc.hold_amount)
            paid = D(inc.paid_amount)
            group["calculated"] += amt
            group["hold"] += hold
            group["paid"] += paid
            if inc.status == "Approved":
                group["approved"] += amt
            elif inc.status == "Posted":
                group["posted"] += amt
                group["balance"] += max(ZERO, amt - hold - paid)
        rows = [
            {
                "id": k,
                "name": v["name"],
                "calculated": float(round2(v["calculated"])),
                "approved": float(round2(v["approved"])),
                "posted": float(round2(v["posted"])),
                "hold": float(round2(v["hold"])),
                "paid": float(round2(v["paid"])),
                "balance": float(round2(v["balance"])),
            }
            for k, v in by_emp.items()
        ]
        return {"ledgerType": "incentive", "rows": rows}

    else:  # jobwork
        from apps.jobwork.models import JobWorkOrder
        from apps.masters.models import Party
        processors = Party.objects.filter(client_id=client_id, type__in=["Vendor", "Both"], deleted_at__isnull=True)
        jwos = JobWorkOrder.objects.filter(client_id=client_id, deleted_at__isnull=True)
        by_processor = {}
        for proc in processors:
            proc_jwos = [j for j in jwos if str(j.processor_id) == str(proc.id)]
            total_orders = len(proc_jwos)
            if total_orders == 0:
                continue
            total_val = sum((D(j.rate) * D(j.sent_meter) for j in proc_jwos), ZERO)
            by_processor[str(proc.id)] = {
                "id": str(proc.id),
                "name": proc.name,
                "orderCount": total_orders,
                "totalValue": float(round2(total_val)),
                "balance": float(round2(total_val)),
            }
        return {"ledgerType": "jobwork", "rows": list(by_processor.values())}

