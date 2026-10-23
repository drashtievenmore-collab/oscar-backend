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
    """Agency-wise: today produced, cumulative, balance, delayed, missing today's update."""
    from datetime import date
    from django.utils import timezone
    from .models import DailyProductionEntry, ProductionInstruction
    from apps.masters.models import Agency

    if day is None:
        today = timezone.localdate()
    elif isinstance(day, str):
        try:
            today = date.fromisoformat(day)
        except (ValueError, TypeError):
            today = timezone.localdate()
    else:
        today = day

    instructions = ProductionInstruction.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    )
    agency_names = list(
        instructions.values_list("agency_name", flat=True).distinct().order_by("agency_name")
    )
    agencies_master = {a.name: a for a in Agency.objects.filter(client_id=client_id, deleted_at__isnull=True)}
    for name in agencies_master:
        if name not in agency_names:
            agency_names.append(name)

    rows = []
    for agency_name in agency_names:
        if not agency_name:
            continue
        scoped = instructions.filter(agency_name=agency_name)
        master = agencies_master.get(agency_name)

        all_entries = DailyProductionEntry.objects.filter(
            client_id=client_id, instruction__in=scoped, deleted_at__isnull=True
        )
        today_entries = all_entries.filter(entry_date=today)

        today_produced = today_entries.aggregate(m=Sum("meters"))["m"] or ZERO
        today_takas = today_entries.aggregate(t=Sum("produced_takas"))["t"] or 0
        cumulative_produced = all_entries.aggregate(m=Sum("meters"))["m"] or ZERO
        order_meters = scoped.aggregate(m=Sum("order_meter"))["m"] or ZERO
        balance_meters = max(ZERO, order_meters - cumulative_produced)

        active_instructions = scoped.filter(status__in=["Running", "Partially Completed", "Not Started", "Approved"])
        running_instructions = scoped.filter(status__in=["Running", "Partially Completed"])
        instructions_with_today_entry = set(today_entries.values_list("instruction_id", flat=True))
        missing_today_count = sum(1 for inst in running_instructions if inst.id not in instructions_with_today_entry)

        delayed_count = scoped.filter(
            expected_start_date__lt=today,
            status__in=["Draft", "Approved", "Not Started"]
        ).count()

        totals = _entry_totals(all_entries)
        quality = _quality_split(all_entries)
        rows.append(
            {
                "agency": agency_name,
                "agencyCode": master.code if master else None,
                "agencyId": str(master.id) if master else None,
                "ordersMonitored": scoped.values("order_reference").distinct().count(),
                "instructions": scoped.count(),
                "activeInstructions": active_instructions.count(),
                "todayProduced": float(today_produced),
                "todayTakas": int(today_takas),
                "cumulativeProduced": float(cumulative_produced),
                "orderMeters": float(order_meters),
                "balanceMeters": float(balance_meters),
                "delayed": delayed_count,
                "missingTodayUpdate": missing_today_count,
                "dailyEntries": totals["entries"],
                "metersMonitored": float(totals["meters"]),
                "pendingReports": scoped.exclude(status__in=["Verified", "Closed", "PO Created"]).count(),
                **quality,
            }
        )
    return rows


def _quality_split(entries):
    """Good / reject / reprocess out of submitted production (PDF §15
    "QC / Wastage: pass %, reject %, reprocess %" by agency).

    Same rule as the completion screen: drafts do not count, and a row with
    no good-metre figure counts produced − reject − reprocess as good.
    """
    produced = rejected = reprocess = accepted = ZERO
    for entry in entries.exclude(status="Draft"):
        meters = D(entry.meters or 0)
        reject = D(entry.reject_meter or 0)
        rework = D(entry.reprocess_meter or 0)
        produced += meters
        rejected += reject
        reprocess += rework
        accepted += D(entry.good_meter) if entry.good_meter else max(ZERO, meters - reject - rework)

    def pct(part):
        return float(round2(part / produced * 100)) if produced > ZERO else 0.0

    return {
        "acceptedMeters": float(accepted),
        "rejectedMeters": float(rejected),
        "reprocessMeters": float(reprocess),
        "acceptedPct": pct(accepted),
        "rejectPct": pct(rejected),
        "reprocessPct": pct(reprocess),
    }


def employee_production_view(client_id, *, employee_id=None, day=None):
    """Employee-wise: today produced, cumulative, balance, delayed, missing today's update."""
    from datetime import date
    from django.utils import timezone
    from apps.hrms.models import Employee
    from .models import DailyProductionEntry, ProductionInstruction

    if day is None:
        today = timezone.localdate()
    elif isinstance(day, str):
        try:
            today = date.fromisoformat(day)
        except (ValueError, TypeError):
            today = timezone.localdate()
    else:
        today = day

    employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if employee_id is not None:
        employees = employees.filter(pk=employee_id)
    rows = []
    for employee in employees.order_by("name"):
        all_entries = DailyProductionEntry.objects.filter(
            client_id=client_id,
            entered_by_employee=employee,
            deleted_at__isnull=True,
        )
        today_entries = all_entries.filter(entry_date=today)
        today_produced = today_entries.aggregate(m=Sum("meters"))["m"] or ZERO
        cumulative_produced = all_entries.aggregate(m=Sum("meters"))["m"] or ZERO

        assigned_instructions = ProductionInstruction.objects.filter(
            client_id=client_id, employee=employee, deleted_at__isnull=True
        )
        active_instructions = assigned_instructions.filter(
            status__in=["Running", "Partially Completed", "Not Started", "Approved"]
        )
        running_instructions = assigned_instructions.filter(status__in=["Running", "Partially Completed"])
        today_entered_inst_ids = set(
            DailyProductionEntry.objects.filter(
                client_id=client_id, instruction__in=running_instructions, entry_date=today, deleted_at__isnull=True
            ).values_list("instruction_id", flat=True)
        )
        missing_today_count = sum(1 for inst in running_instructions if inst.id not in today_entered_inst_ids)

        delayed_count = assigned_instructions.filter(
            expected_start_date__lt=today,
            status__in=["Draft", "Approved", "Not Started"]
        ).count()

        totals = _entry_totals(all_entries)
        rows.append(
            {
                "employeeId": str(employee.id),
                "employeeCode": employee.employee_code,
                "name": employee.name,
                "status": employee.status,
                "agenciesHandled": all_entries.values(
                    "instruction__agency_name"
                ).distinct().count(),
                "ordersMonitored": assigned_instructions.count(),
                "activeInstructions": active_instructions.count(),
                "todayProduced": float(today_produced),
                "cumulativeProduced": float(cumulative_produced),
                "delayed": delayed_count,
                "missingTodayUpdate": missing_today_count,
                "dailyEntries": totals["entries"],
                "metersMonitored": float(totals["meters"]),
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
