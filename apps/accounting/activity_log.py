"""Accounting log: the ledger entries raised by payroll, incentive, brokerage,
expenses and payments, each tied back to the document that caused it.

Read-only. It reports what ``post_entry`` already wrote -- nothing here posts.
"""
from collections import defaultdict

from django.db.models import Count, Q, Sum

from apps.core.money import ZERO, round2

from .models import JournalEntry, JournalLine

#: module key -> (label, source_document_type values it covers)
MODULES = {
    "payroll": ("Payroll & Advances", ["Payslip", "SalaryAdvance"]),
    "incentive": (
        "Salesperson Incentive",
        ["IncentiveCalc", "IncentivePayout", "SalespersonIncentiveAccrual", "SalespersonIncentivePayout"],
    ),
    "broker": ("Broker Commission", ["BrokerCommission"]),
    "expense": ("Expenses", ["Expense"]),
    "payment_in": ("Payment In", ["PaymentIn"]),
    "payment_out": ("Payment Out", ["PaymentOut"]),
}
SOURCE_MODULE = {src: key for key, (_, sources) in MODULES.items() for src in sources}
ALL_SOURCES = list(SOURCE_MODULE)


# ---------------------------------------------------------------------------
# Document labels -- one query per document type on the page
# ---------------------------------------------------------------------------
def _payslips(ids):
    from apps.hrms.models import Payslip

    return {
        p.id: f"Payslip {p.employee.employee_code} {p.period_month:%b %Y} - {p.employee.name}"
        for p in Payslip.objects.filter(pk__in=ids).select_related("employee")
    }


def _advances(ids):
    from apps.hrms.models import SalaryAdvance

    return {
        a.id: f"Salary advance - {a.employee.name}" + (" (deleted)" if a.deleted_at else "")
        for a in SalaryAdvance.objects.filter(pk__in=ids).select_related("employee")
    }


def _incentive_calcs(ids):
    from apps.pms.models import IncentiveCalculation

    return {
        c.id: f"Incentive {c.calc_number or ''} {c.period_label} - {c.employee.name}".replace("  ", " ")
        for c in IncentiveCalculation.objects.filter(pk__in=ids).select_related("employee")
    }


def _legacy_accruals(ids):
    from apps.hrms.models import SalespersonIncentiveAccrual

    return {
        a.id: f"Incentive {a.period_name} - {a.employee.name}"
        for a in SalespersonIncentiveAccrual.objects.filter(pk__in=ids).select_related("employee")
    }


def _broker_commissions(ids):
    from apps.sales.models import BrokerCommission

    return {
        c.id: f"Brokerage {c.commission_number or ''} - {c.broker.name if c.broker_id else ''}".strip(" -")
        for c in BrokerCommission.objects.filter(pk__in=ids).select_related("broker")
    }


def _expenses(ids):
    from apps.purchase.models import Expense

    return {
        e.id: f"Expense {e.expense_number}" + (" (deleted)" if e.deleted_at else "")
        for e in Expense.objects.filter(pk__in=ids)
    }


def _payments_in(ids):
    from apps.sales.models import PaymentIn

    return {
        p.id: f"Receipt {p.payment_number} - {p.party.name if p.party_id else ''}".strip(" -")
        for p in PaymentIn.objects.filter(pk__in=ids).select_related("party")
    }


def _payments_out(ids):
    from apps.purchase.models import PaymentOut

    return {
        p.id: f"Payment {p.payment_number} - {p.party.name if p.party_id else ''}".strip(" -")
        for p in PaymentOut.objects.filter(pk__in=ids).select_related("party")
    }


LABELLERS = {
    "Payslip": _payslips,
    "SalaryAdvance": _advances,
    "IncentiveCalc": _incentive_calcs,
    "IncentivePayout": _incentive_calcs,
    "SalespersonIncentiveAccrual": _legacy_accruals,
    "SalespersonIncentivePayout": _legacy_accruals,
    "BrokerCommission": _broker_commissions,
    "Expense": _expenses,
    "PaymentIn": _payments_in,
    "PaymentOut": _payments_out,
}


def _document_labels(entries):
    ids_by_type = defaultdict(set)
    for entry in entries:
        if entry.source_document_id:
            ids_by_type[entry.source_document_type].add(entry.source_document_id)
    labels = {}
    for source_type, ids in ids_by_type.items():
        labeller = LABELLERS.get(source_type)
        if labeller is None:
            continue
        for doc_id, label in labeller(ids).items():
            labels[(source_type, doc_id)] = label
    return labels


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
def _base_queryset(client_id, *, date_from=None, date_to=None, search=None):
    queryset = JournalEntry.objects.filter(
        client_id=client_id, deleted_at__isnull=True, source_document_type__in=ALL_SOURCES
    )
    if date_from:
        queryset = queryset.filter(entry_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(entry_date__lte=date_to)
    if search:
        queryset = queryset.filter(
            Q(entry_number__icontains=search) | Q(narration__icontains=search)
        )
    return queryset


def module_summary(client_id, *, date_from=None, date_to=None, search=None):
    """Per-module entry count and net amount still on the books.

    A reversed entry and its mirror cancel out, so both are left out of
    ``netAmount`` but still counted as activity.
    """
    queryset = _base_queryset(client_id, date_from=date_from, date_to=date_to, search=search)
    counts = defaultdict(int)
    for row in queryset.values("source_document_type").annotate(n=Count("id")):
        counts[SOURCE_MODULE[row["source_document_type"]]] += row["n"]

    amounts = defaultdict(lambda: ZERO)
    live_lines = JournalLine.objects.filter(
        journal_entry__in=queryset.filter(reversal_of__isnull=True).exclude(status="Reversed")
    )
    for row in live_lines.values("journal_entry__source_document_type").annotate(total=Sum("debit")):
        amounts[SOURCE_MODULE[row["journal_entry__source_document_type"]]] += row["total"] or ZERO

    return [
        {
            "module": key,
            "label": label,
            "entries": counts.get(key, 0),
            "netAmount": round2(amounts.get(key, ZERO)),
        }
        for key, (label, _) in MODULES.items()
    ]


def accounting_log(client_id, *, module=None, date_from=None, date_to=None, search=None,
                   page=1, page_size=50):
    queryset = _base_queryset(client_id, date_from=date_from, date_to=date_to, search=search)
    if module in MODULES:
        queryset = queryset.filter(source_document_type__in=MODULES[module][1])

    total = queryset.count()
    start = (page - 1) * page_size
    entries = list(
        queryset.select_related("posted_by", "reversal_of")
        .prefetch_related("lines__account", "lines__party")
        .order_by("-entry_date", "-created_at")[start:start + page_size]
    )
    labels = _document_labels(entries)

    rows = []
    for entry in entries:
        lines = sorted(entry.lines.all(), key=lambda line: line.line_no)
        module_key = SOURCE_MODULE.get(entry.source_document_type)
        rows.append({
            "id": str(entry.id),
            "entryNumber": entry.entry_number,
            "entryDate": entry.entry_date,
            "module": module_key,
            "moduleLabel": MODULES[module_key][0] if module_key else entry.source_document_type,
            "sourceDocumentType": entry.source_document_type,
            "sourceDocumentId": str(entry.source_document_id) if entry.source_document_id else None,
            "documentLabel": labels.get(
                (entry.source_document_type, entry.source_document_id),
                entry.source_document_type,
            ),
            "narration": entry.narration,
            "status": entry.status,
            "isReversal": entry.reversal_of_id is not None,
            "reversalOf": entry.reversal_of.entry_number if entry.reversal_of_id else None,
            "postedBy": getattr(entry.posted_by, "name", None) or getattr(entry.posted_by, "email", None),
            "postedAt": entry.posted_at,
            "amount": round2(sum((line.debit for line in lines), ZERO)),
            "lines": [
                {
                    "accountCode": line.account.code,
                    "accountName": line.account.name,
                    "party": line.party.name if line.party_id else None,
                    "description": line.description,
                    "debit": round2(line.debit),
                    "credit": round2(line.credit),
                }
                for line in lines
            ],
        })
    return rows, total
