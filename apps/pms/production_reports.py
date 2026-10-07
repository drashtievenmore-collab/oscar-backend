"""
Employee production reports (HRMS_ERP_CHANGES.md §15 dashboards).

Joins HRMS to production and sales. No new tables -- every view here is a
query over ``Employee``, ``ProductionInstruction``, ``DailyProductionEntry``,
``SalesInvoice``/``SalesReturn`` and ``IncentiveCalculation``.
"""
from django.db.models import Count, Sum

from apps.core.money import ZERO, D, round2

from .production_services import sales_totals_for_period


def _entry_totals(queryset):
    row = queryset.aggregate(entries=Count("id"), meters=Sum("meters"))
    return {
        "entries": row["entries"] or 0,
        "meters": row["meters"] or ZERO,
    }


def agency_production_view(client_id, *, day=None):
    """Agency-wise: agencies handled, orders monitored, entries, meters, pending."""
    from .models import DailyProductionEntry, ProductionInstruction

    instructions = ProductionInstruction.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    )
    rows = []
    for agency in (
        instructions.values_list("agency_name", flat=True).distinct().order_by("agency_name")
    ):
        scoped = instructions.filter(agency_name=agency)
        day_entries = DailyProductionEntry.objects.filter(
            client_id=client_id, instruction__in=scoped, deleted_at__isnull=True
        )
        if day is not None:
            day_entries = day_entries.filter(entry_date=day)
        totals = _entry_totals(day_entries)
        rows.append(
            {
                "agency": agency,
                "ordersMonitored": scoped.values("order_reference").distinct().count(),
                "instructions": scoped.count(),
                "dailyEntries": totals["entries"],
                "metersMonitored": totals["meters"],
                "pendingReports": scoped.exclude(status__in=["Verified", "Closed"]).count(),
            }
        )
    return rows


def employee_production_view(client_id, *, employee_id=None, day=None):
    """Employee-wise: entries joined on ``entered_by_employee_id`` plus workload."""
    from apps.hrms.models import Employee

    from .models import DailyProductionEntry, ProductionInstruction

    employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if employee_id is not None:
        employees = employees.filter(pk=employee_id)
    rows = []
    for employee in employees.order_by("name"):
        entries = DailyProductionEntry.objects.filter(
            client_id=client_id,
            entered_by_employee=employee,
            deleted_at__isnull=True,
        )
        if day is not None:
            entries = entries.filter(entry_date=day)
        totals = _entry_totals(entries)
        rows.append(
            {
                "employeeId": str(employee.id),
                "employeeCode": employee.employee_code,
                "name": employee.name,
                "status": employee.status,
                "agenciesHandled": entries.values(
                    "instruction__agency_name"
                ).distinct().count(),
                "ordersMonitored": ProductionInstruction.objects.filter(
                    client_id=client_id, employee=employee, deleted_at__isnull=True
                ).count(),
                "dailyEntries": totals["entries"],
                "metersMonitored": totals["meters"],
            }
        )
    return rows


def salesperson_view(client_id, *, employee_id, period_start, period_end):
    """Salesperson view for staff: period sales totals plus their incentive calcs.

    ``SALES_ORDERS``/``SALES_INVOICES`` carry no salesperson FK in this schema,
    so invoice-side numbers are period totals; the staff link is the incentive
    calculation's employee.
    """
    from apps.hrms.models import Employee

    from .models import IncentiveCalculation

    employee = Employee.objects.filter(
        pk=employee_id, client_id=client_id, deleted_at__isnull=True
    ).first()
    if employee is None:
        return None
    totals = sales_totals_for_period(client_id, period_start, period_end)
    calcs = IncentiveCalculation.objects.filter(
        client_id=client_id,
        employee=employee,
        period_start__gte=period_start,
        period_end__lte=period_end,
        deleted_at__isnull=True,
    ).exclude(status="Reversed")
    incentive = calcs.aggregate(value=Sum("incentive_amount"))["value"] or ZERO
    net_sales = D(totals["net_sales"])
    contribution = round2(incentive * 100 / net_sales) if net_sales > ZERO else ZERO
    return {
        "employeeId": str(employee.id),
        "employeeCode": employee.employee_code,
        "name": employee.name,
        "netSales": totals["net_sales"],
        "collection": totals["collection"],
        "discount": totals["discount"],
        "returns": totals["returns"],
        "incentive": round2(incentive),
        "contributionPct": contribution,
        "calculations": [
            {
                "id": str(calc.id),
                "calcNumber": calc.calc_number,
                "period": calc.period_label,
                "amount": calc.incentive_amount,
                "status": calc.status,
            }
            for calc in calcs.order_by("period_start")
        ],
    }
