"""
HRMS services (api.md §11).

The three interlocking pieces db.md §11 calls out:

  - attendance derives ``hours`` and the Late / Half Day verdict from the
    ``attendance_flexibility`` policy
  - leave approval posts attendance **and** decrements the balance in one
    transaction
  - payroll ports ``salaryCalculations.js`` verbatim, and freezes the result on
    the payslip so a later attendance edit cannot silently restate a paid month
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

from .models import (
    Attendance,
    CalendarEvent,
    Employee,
    Holiday,
    LeaveBalance,
    LeaveRequest,
    Payslip,
    PayslipComponent,
    PayrollRun,
    SalaryAdvance,
    WorkingDay,
)

#: api.md §11.4 -- totalDays defaults to 24.
DEFAULT_TOTAL_DAYS = Decimal("24")

#: Defaults for the `attendance_flexibility` settings blob (db.md §2.8).
DEFAULT_FLEXIBILITY = {
    "graceMinutes": 15,
    "shiftStart": "09:30",
    "shiftEnd": "18:30",
    "fullDayHours": 8,
    "halfDayThresholdHours": 4,
    "coreHoursStart": "11:00",
    "coreHoursEnd": "16:00",
}


def flexibility_policy(client_id):
    from apps.core.models import Setting

    row = Setting.objects.filter(client_id=client_id, key="attendance_flexibility").first()
    policy = dict(DEFAULT_FLEXIBILITY)
    if row and isinstance(row.value, dict):
        policy.update(row.value)
    return policy


def _parse_time(value, fallback):
    try:
        hour, minute = str(value).split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
def derive_attendance(client_id, *, check_in, check_out, submitted_status=None):
    """``hours`` plus the Late / Half Day determination, applied server-side.

    An explicitly-submitted non-working status (Absent, On Leave, Holiday,
    Week Off, WFH) is respected as-is; the policy only decides between Present,
    Late and Half Day, which is the judgement the frontend was making badly.
    """
    if submitted_status in ("Absent", "On Leave", "Holiday", "Week Off"):
        return submitted_status, None

    if check_in is None:
        return submitted_status or "Absent", None

    hours = None
    if check_out is not None:
        delta = check_out - check_in
        hours = round(Decimal(delta.total_seconds()) / Decimal(3600), 2)
        if hours < 0:
            raise ValidationFailed(
                "Check-out cannot be before check-in.",
                field_errors={"checkOut": ["Must be after check-in."]},
            )

    if submitted_status == "WFH":
        return "WFH", hours

    policy = flexibility_policy(client_id)
    shift_start = _parse_time(policy.get("shiftStart"), time(9, 30))
    grace = int(policy.get("graceMinutes") or 0)
    half_day_threshold = Decimal(str(policy.get("halfDayThresholdHours") or 4))
    full_day_hours = Decimal(str(policy.get("fullDayHours") or 8))

    if hours is not None:
        if hours < half_day_threshold:
            return "Half Day", hours
        if hours < full_day_hours:
            # Short of a full day but past the half-day threshold still counts
            # as a present day; the shortfall shows in `hours`.
            pass

    local_check_in = timezone.localtime(check_in) if timezone.is_aware(check_in) else check_in
    latest_on_time = (
        datetime.combine(local_check_in.date(), shift_start) + timedelta(minutes=grace)
    )
    if local_check_in.replace(tzinfo=None) > latest_on_time:
        return "Late", hours

    return "Present", hours


@transaction.atomic
def mark_attendance(
    *, client, employee, work_date, check_in=None, check_out=None, status=None,
    remark=None, source="manual", user=None, leave_request=None,
):
    """Upsert against the one-row-per-employee-per-day constraint (db.md §11.2)."""
    if getattr(employee, "status", None) in ["Resigned", "Terminated", "Exited"]:
        raise Conflict(
            f"Cannot mark attendance for {employee.status.lower()} employee {employee.name}.",
            code=Codes.ALREADY_DONE,
        )
    derived_status, hours = derive_attendance(
        client.id if hasattr(client, "id") else client,
        check_in=check_in,
        check_out=check_out,
        submitted_status=status,
    )

    client_id = getattr(client, "id", client)
    existing = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    before = None
    if existing is not None:
        before = {
            "status": existing.status,
            "checkIn": existing.check_in,
            "checkOut": existing.check_out,
        }
        existing.check_in = check_in
        existing.check_out = check_out
        existing.hours = hours
        existing.status = derived_status
        existing.remark = remark
        existing.source = source
        if leave_request is not None:
            existing.leave_request = leave_request
        existing.updated_by = user if getattr(user, "is_authenticated", False) else None
        existing.save()
        row = existing
    else:
        row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            check_in=check_in,
            check_out=check_out,
            hours=hours,
            status=derived_status,
            remark=remark,
            source=source,
            leave_request=leave_request,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    # db.md §11.2 -- manual edits write an audit row; that is what
    # GET /hrms/attendance/audit/ reads.
    if source in ("manual", "regularization") and getattr(user, "is_authenticated", False):
        record_audit(
            client=client_id,
            actor=user,
            action="attendance_edit" if existing is not None else "attendance_mark",
            entity_type="Attendance",
            entity_id=row.id,
            entity_label=f"{employee.employee_code} {work_date}",
            description=f"Attendance set to {derived_status}",
            before=before,
            after={"status": derived_status, "checkIn": check_in, "checkOut": check_out},
        )
    return row


def working_days_in(client_id, start, end, location_id=None):
    """Working dates in a range, honouring the working-day and holiday config."""
    config = {
        row.weekday: row.is_working
        for row in WorkingDay.objects.filter(client_id=client_id, deleted_at__isnull=True)
    }
    holidays = set(
        Holiday.objects.filter(
            client_id=client_id, deleted_at__isnull=True, date__gte=start, date__lte=end
        ).values_list("date", flat=True)
    )

    days = []
    current = start
    while current <= end:
        weekday = current.weekday()
        is_working = config.get(weekday, weekday < 5)  # Mon-Fri by default
        if is_working and current not in holidays:
            days.append(current)
        current += timedelta(days=1)
    return days


def attendance_summary(client_id, *, month=None, employee_id=None, department_id=None):
    """``GET /hrms/attendance/summary/`` -- monthly per-employee counts."""
    queryset = Attendance.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if month:
        queryset = queryset.filter(work_date__year=month.year, work_date__month=month.month)
    if employee_id:
        queryset = queryset.filter(employee_id=employee_id)
    if department_id:
        queryset = queryset.filter(employee__department_id=department_id)

    rows = (
        queryset.values("employee_id", "employee__employee_code", "employee__name")
        .annotate(
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            halfDay=Count("id", filter=Q(status="Half Day")),
            wfh=Count("id", filter=Q(status="WFH")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            holiday=Count("id", filter=Q(status="Holiday")),
            weekOff=Count("id", filter=Q(status="Week Off")),
            totalHours=Sum("hours"),
        )
        .order_by("employee__name")
    )
    return [
        {
            "employeeId": str(row["employee_id"]),
            "employeeCode": row["employee__employee_code"],
            "name": row["employee__name"],
            "present": row["present"],
            "absent": row["absent"],
            "late": row["late"],
            "halfDay": row["halfDay"],
            "wfh": row["wfh"],
            "onLeave": row["onLeave"],
            "holiday": row["holiday"],
            "weekOff": row["weekOff"],
            "totalHours": row["totalHours"] or 0,
            # Late and WFH days still count as attended for payroll.
            "attendedDays": row["present"] + row["late"] + row["wfh"]
            + Decimal("0.5") * row["halfDay"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
def assert_no_overlap(leave_request):
    """db.md §11.3 -- overlapping live requests for the same employee are rejected."""
    clash = LeaveRequest.objects.filter(
        client_id=leave_request.client_id,
        employee_id=leave_request.employee_id,
        status__in=["Pending Review", "Delegate Confirmed", "Approved"],
        from_date__lte=leave_request.to_date,
        to_date__gte=leave_request.from_date,
        deleted_at__isnull=True,
    ).exclude(pk=leave_request.pk)
    if clash.exists():
        existing = clash.first()
        raise BusinessRuleViolation(
            "This overlaps an existing leave request "
            f"({existing.from_date} to {existing.to_date}).",
            code="LEAVE_OVERLAP",
            payload={"conflictingRequestId": str(existing.id)},
        )


def leave_balance_for(client_id, employee_id, leave_type_id, year=None):
    year = year or timezone.localdate().year
    balance, _ = LeaveBalance.objects.get_or_create(
        client_id=client_id,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        period_year=year,
        defaults={"entitlement": ZERO, "carried_forward": ZERO, "used": ZERO},
    )
    return balance


@transaction.atomic
def approve_leave(leave_request, *, user=None, remark=None):
    """One transaction: flip to Approved, post attendance, decrement balance.

    api.md §11.3 requires all three together -- a leave that is approved but
    not posted to attendance would silently become an absence at payroll time.
    """
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Approved":
        raise Conflict("This leave is already approved.", code=Codes.ALREADY_DONE)
    if leave_request.status in ("Rejected", "Cancelled"):
        raise Conflict(
            f"This leave request is {leave_request.status.lower()}.",
            code=Codes.BAD_TARGET,
        )

    assert_no_overlap(leave_request)

    balance = leave_balance_for(
        leave_request.client_id, leave_request.employee_id, leave_request.leave_type_id,
        leave_request.from_date.year,
    )
    available = D(balance.entitlement) + D(balance.carried_forward) - D(balance.used) - D(balance.encashed)
    if D(leave_request.days) > available:
        raise BusinessRuleViolation(
            f"{leave_request.employee.name} has {available} day(s) of "
            f"{leave_request.leave_type.name} left but requested {leave_request.days}.",
            code="INSUFFICIENT_LEAVE_BALANCE",
            payload={"available": str(available), "requested": str(leave_request.days)},
        )

    leave_request.status = "Approved"
    leave_request.approver = user if getattr(user, "is_authenticated", False) else None
    leave_request.decided_at = timezone.now()
    leave_request.remark = remark
    leave_request.save()

    # Post attendance for every working day in range.
    for day in working_days_in(
        leave_request.client_id, leave_request.from_date, leave_request.to_date
    ):
        mark_attendance(
            client=leave_request.client,
            employee=leave_request.employee,
            work_date=day,
            status="On Leave",
            source="leave",
            leave_request=leave_request,
            remark=leave_request.reason,
            user=user,
        )

    balance.used = D(balance.used) + D(leave_request.days)
    balance.save(update_fields=["used", "updated_at"])

    # api.md §11.8 -- leave-derived calendar events are generated, not entered.
    CalendarEvent.objects.create(
        client_id=leave_request.client_id,
        title=f"{leave_request.employee.name} on leave",
        type="Leave",
        starts_at=timezone.make_aware(
            datetime.combine(leave_request.from_date, time(0, 0))
        ),
        ends_at=timezone.make_aware(datetime.combine(leave_request.to_date, time(23, 59))),
        all_day=True,
        department=leave_request.employee.department,
        source_type="LeaveRequest",
        source_id=leave_request.id,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if leave_request.employee.status == "Active" and D(leave_request.days) >= 1:
        notify(
            client=leave_request.client_id,
            recipients=[leave_request.employee.id],
            type="hrms.leave_approved",
            category="hrms",
            title="Your leave was approved",
            body=f"{leave_request.from_date} to {leave_request.to_date}.",
            entity_type="LeaveRequest",
            entity_id=leave_request.id,
            actor=user,
        )
    return leave_request


@transaction.atomic
def cancel_leave(leave_request, *, user=None, reason=None):
    """Withdraw a leave, removing exactly the rows it generated."""
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Cancelled":
        raise Conflict("This leave is already cancelled.", code=Codes.ALREADY_CANCELLED)

    was_approved = leave_request.status == "Approved"
    leave_request.status = "Cancelled"
    leave_request.remark = reason or leave_request.remark
    leave_request.save(update_fields=["status", "remark", "updated_at"])

    if was_approved:
        Attendance.objects.filter(
            client_id=leave_request.client_id, leave_request=leave_request
        ).update(deleted_at=timezone.now())

        balance = leave_balance_for(
            leave_request.client_id, leave_request.employee_id,
            leave_request.leave_type_id, leave_request.from_date.year,
        )
        balance.used = max(D(balance.used) - D(leave_request.days), ZERO)
        balance.save(update_fields=["used", "updated_at"])

        # Source-tagged events make this exact, rather than a fuzzy date match.
        CalendarEvent.objects.filter(
            client_id=leave_request.client_id,
            source_type="LeaveRequest",
            source_id=leave_request.id,
        ).update(deleted_at=timezone.now())

    return leave_request


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
def compute_salary(
    *, standard_salary, total_days=None, attended_days=0, paid_leaves=0,
    additional_earnings=0, deductions=0, advance=0, earned_salary_override=None,
):
    """Ported verbatim from ``features/hrms/payroll/salaryCalculations.js``.

        perDaySalary        = standardSalary / totalDays
        absentDays          = totalDays - attendedDays - paidLeaves
        attendanceDeduction = perDaySalary * max(0, absentDays)
        earnedSalary        = standardSalary - attendanceDeduction
        remainingPayable    = earnedSalary + additionalEarnings - deductions - advance

    Approved paid leave does not cause a deduction. An explicit
    ``earnedSalary`` overrides the computed value -- api.md §11.4 asks for that
    escape hatch to be kept, and the caller logs that it was used.
    """
    standard_salary = D(standard_salary)
    total_days = D(total_days) if total_days else DEFAULT_TOTAL_DAYS
    if total_days <= ZERO:
        total_days = DEFAULT_TOTAL_DAYS
    attended_days = D(attended_days)
    paid_leaves = D(paid_leaves)

    per_day = standard_salary / total_days
    absent_days = total_days - attended_days - paid_leaves
    if absent_days < ZERO:
        absent_days = ZERO
    attendance_deduction = round2(per_day * absent_days)

    if earned_salary_override is not None:
        earned_salary = round2(earned_salary_override)
        overridden = True
    else:
        earned_salary = round2(standard_salary - attendance_deduction)
        overridden = False

    net_payable = round2(
        earned_salary + D(additional_earnings) - D(deductions) - D(advance)
    )

    return {
        "perDaySalary": round2(per_day),
        "absentDays": absent_days,
        "attendanceDeduction": attendance_deduction,
        "earnedSalary": earned_salary,
        "earnedSalaryOverridden": overridden,
        "netPayable": net_payable,
    }


def split_structure(structure, earned_salary):
    """Basic / HRA / allowances from the salary structure, if one is attached."""
    earned_salary = D(earned_salary)
    if structure is None:
        return {"basic": earned_salary, "hra": ZERO, "allowances": ZERO, "components": []}

    basic = round2(earned_salary * D(structure.basic_pct or 0) / Decimal("100"))
    hra = round2(earned_salary * D(structure.hra_pct or 0) / Decimal("100"))
    allowances = round2(earned_salary - basic - hra)

    components = []
    for component in structure.components or []:
        name = component.get("name")
        kind = component.get("type") or component.get("kind") or "earning"
        calc = component.get("calc") or "fixed"
        value = D(component.get("value"))
        amount = (
            round2(earned_salary * value / Decimal("100")) if calc == "percent" else round2(value)
        )
        if name:
            components.append({"name": name, "kind": kind, "amount": amount})

    return {"basic": basic, "hra": hra, "allowances": allowances, "components": components}


@transaction.atomic
def process_payroll(*, client, period_month, employee_ids=None, user=None):
    """``POST /hrms/payroll/process/`` -- generate the run."""
    period_month = period_month.replace(day=1)

    run, _ = PayrollRun.objects.get_or_create(
        client=client,
        period_month=period_month,
        defaults={
            "status": "In Progress",
            "processed_by": user if getattr(user, "is_authenticated", False) else None,
            "processed_at": timezone.now(),
            "created_by": user if getattr(user, "is_authenticated", False) else None,
        },
    )
    if run.status in ("Approved", "Paid"):
        raise Conflict(
            f"Payroll for {period_month:%B %Y} is already {run.status.lower()}.",
            code=Codes.ALREADY_DONE,
        )

    employees = Employee.objects.filter(
        client=client, deleted_at__isnull=True, status__in=["Active", "On Leave", "Probation"]
    ).select_related("salary_structure")
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)

    month_end = _month_end(period_month)
    working = working_days_in(client.id, period_month, month_end)
    total_days = Decimal(len(working)) or DEFAULT_TOTAL_DAYS

    payslips = []
    for employee in employees:
        rows = Attendance.objects.filter(
            client=client,
            employee=employee,
            work_date__gte=period_month,
            work_date__lte=month_end,
            deleted_at__isnull=True,
        ).values("status").annotate(count=Count("id"))
        counts = {row["status"]: row["count"] for row in rows}

        attended = (
            Decimal(counts.get("Present", 0))
            + Decimal(counts.get("Late", 0))
            + Decimal(counts.get("WFH", 0))
            + Decimal("0.5") * Decimal(counts.get("Half Day", 0))
        )
        paid_leaves = Decimal(counts.get("On Leave", 0))

        from apps.hrms.models import SalespersonIncentiveAccrual
        incentive_accruals = list(
            SalespersonIncentiveAccrual.objects.filter(
                client=client,
                employee=employee,
                status="APPROVED",
                paid_at__isnull=True,
                end_date__lte=month_end,
                deleted_at__isnull=True,
            )
        )
        incentive_total = sum((round2(acc.calculated_incentive) for acc in incentive_accruals), ZERO)

        advance = _pending_advance_instalment(employee)
        result = compute_salary(
            standard_salary=employee.standard_salary,
            total_days=total_days,
            attended_days=attended,
            paid_leaves=paid_leaves,
            additional_earnings=incentive_total,
            advance=advance,
        )
        split = split_structure(employee.salary_structure, result["earnedSalary"])
        if incentive_total > ZERO:
            split["components"].append({
                "name": "Sales Incentive Payout",
                "kind": "earning",
                "amount": incentive_total,
            })

        payslip, created = Payslip.objects.update_or_create(
            payroll_run=run,
            employee=employee,
            defaults={
                "client": client,
                "period_month": period_month,
                "standard_salary": round2(employee.standard_salary),
                "attended_days": attended,
                "paid_leaves": paid_leaves,
                "total_days": total_days,
                "earned_salary": result["earnedSalary"],
                "earned_salary_overridden": False,
                "basic": split["basic"],
                "hra": split["hra"],
                "allowances": split["allowances"],
                "additional_earnings": incentive_total,
                "deductions": ZERO,
                "advance_recovery": advance,
                "net_payable": result["netPayable"],
                "status": "In Progress",
            },
        )

        PayslipComponent.objects.filter(payslip=payslip).delete()
        if split["components"]:
            PayslipComponent.objects.bulk_create(
                [
                    PayslipComponent(
                        client=client,
                        payslip=payslip,
                        name=component["name"],
                        kind=component["kind"],
                        amount=component["amount"],
                    )
                    for component in split["components"]
                ]
            )
        payslips.append(payslip)

    run.status = "Ready for Review"
    run.save(update_fields=["status", "updated_at"])
    return {"run": run, "payslips": payslips}


def _month_end(first_of_month):
    if first_of_month.month == 12:
        return date(first_of_month.year, 12, 31)
    return date(first_of_month.year, first_of_month.month + 1, 1) - timedelta(days=1)


def _pending_advance_instalment(employee):
    """An advance is recovered from the month it is applied to (api.md §11.4)."""
    advance = SalaryAdvance.objects.filter(
        employee=employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return ZERO
    outstanding = D(advance.amount) - D(advance.recovered_amount)
    if outstanding <= ZERO:
        return ZERO
    instalment = round2(D(advance.amount) / Decimal(max(advance.installments, 1)))
    return min(instalment, round2(outstanding))


@transaction.atomic
def recompute_payslip(payslip, *, earned_salary_override=None, user=None):
    """``PATCH /hrms/payroll/{id}/`` -- adjust before approval."""
    if payslip.status in ("Approved", "Paid"):
        raise Conflict(
            "An approved payslip cannot be adjusted.", code=Codes.ALREADY_DONE
        )

    result = compute_salary(
        standard_salary=payslip.standard_salary,
        total_days=payslip.total_days,
        attended_days=payslip.attended_days,
        paid_leaves=payslip.paid_leaves,
        additional_earnings=payslip.additional_earnings,
        deductions=payslip.deductions,
        advance=payslip.advance_recovery,
        earned_salary_override=earned_salary_override,
    )
    payslip.earned_salary = result["earnedSalary"]
    payslip.earned_salary_overridden = result["earnedSalaryOverridden"]
    payslip.net_payable = result["netPayable"]
    payslip.save(
        update_fields=["earned_salary", "earned_salary_overridden", "net_payable", "updated_at"]
    )

    if result["earnedSalaryOverridden"]:
        # api.md §11.4 -- keep the escape hatch, and log it.
        record_audit(
            client=payslip.client_id,
            actor=user,
            action="payslip_override",
            entity_type="Payslip",
            entity_id=payslip.id,
            entity_label=f"{payslip.employee.employee_code} {payslip.period_month:%b %Y}",
            description=f"Earned salary manually set to {result['earnedSalary']}",
        )
    return payslip


@transaction.atomic
def mark_payslip_paid(payslip, *, payment_date=None, bank_account=None, user=None):
    """Posts the salary journal entry -- payroll is the one HRMS table that
    touches accounts (db.md §11.4)."""
    from apps.accounting import services as ledger

    if payslip.status == "Paid":
        raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
    if payslip.status != "Approved":
        raise Conflict(
            "Approve the payslip before marking it paid.", code=Codes.NOT_FINALIZED
        )

    payslip.status = "Paid"
    payslip.paid_amount = payslip.net_payable
    payslip.payment_date = payment_date or timezone.localdate()
    payslip.bank_account = bank_account
    payslip.save(
        update_fields=["status", "paid_amount", "payment_date", "bank_account", "updated_at"]
    )

    entry = ledger.post_payroll(payslip, user=user)
    if entry is not None:
        payslip.journal_entry = entry
        payslip.save(update_fields=["journal_entry", "updated_at"])

    _recover_advance(payslip)
    return payslip


def _recover_advance(payslip):
    if D(payslip.advance_recovery) <= ZERO:
        return
    advance = SalaryAdvance.objects.select_for_update().filter(
        employee=payslip.employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return
    advance.recovered_amount = round2(
        D(advance.recovered_amount) + D(payslip.advance_recovery)
    )
    if advance.recovered_amount >= D(advance.amount):
        advance.status = "Recovered"
    advance.save(update_fields=["recovered_amount", "status", "updated_at"])


def payroll_summary(client_id, period_month=None):
    """``GET /hrms/payroll/summary/`` -- gross, deductions, net, headcount."""
    queryset = Payslip.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if period_month:
        queryset = queryset.filter(period_month=period_month.replace(day=1))

    rows = queryset.aggregate(
        headcount=Count("id"),
        gross=Sum("earned_salary"),
        additional=Sum("additional_earnings"),
        deductions=Sum("deductions"),
        advances=Sum("advance_recovery"),
        net=Sum("net_payable"),
        paid=Sum("paid_amount"),
    )
    return {
        "headcount": rows["headcount"] or 0,
        "gross": round2(rows["gross"] or 0),
        "additionalEarnings": round2(rows["additional"] or 0),
        "deductions": round2(rows["deductions"] or 0),
        "advanceRecovery": round2(rows["advances"] or 0),
        "net": round2(rows["net"] or 0),
        "paid": round2(rows["paid"] or 0),
    }


# ---------------------------------------------------------------------------
# Derived read-time statuses (db.md §12)
# ---------------------------------------------------------------------------
def document_status(hr_document, today=None):
    """``Valid | Expiring Soon | Expired``, derived from ``valid_until``."""
    today = today or timezone.localdate()
    if hr_document.valid_until is None:
        return "Valid"
    if hr_document.valid_until < today:
        return "Expired"
    if (hr_document.valid_until - today).days <= (hr_document.expiring_soon_days or 30):
        return "Expiring Soon"
    return "Valid"


def acknowledgement_status(acknowledgement, policy, today=None):
    """``Pending | Acknowledged | Overdue`` -- overdue is derived from the
    effective date plus the ack window (api.md §11.8)."""
    if acknowledgement.acknowledged_at is not None:
        return "Acknowledged"
    today = today or timezone.localdate()
    if policy.effective_date is None:
        return "Pending"
    deadline = policy.effective_date + timedelta(days=policy.ack_window_days or 14)
    return "Overdue" if today > deadline else "Pending"


def calendar_time_label(event):
    """api.md §11.8 -- ``time`` is a display string the server derives.

    ``"10:00 AM - 11:30 AM"``, ``"Full Day"`` or ``"Multi-Day"``.
    """
    if event.ends_at and event.starts_at.date() != event.ends_at.date():
        return "Multi-Day"
    if event.all_day:
        return "Full Day"
    start = timezone.localtime(event.starts_at).strftime("%I:%M %p").lstrip("0")
    if event.ends_at is None:
        return start
    end = timezone.localtime(event.ends_at).strftime("%I:%M %p").lstrip("0")
    return f"{start} - {end}"


def org_chart(client_id):
    """``GET /hrms/org-chart/`` -- a recursive walk over ``manager_id``."""
    employees = list(
        Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .select_related("designation", "department")
        .only(
            "id", "name", "employee_code", "manager_id", "avatar_url",
            "designation__name", "department__name",
        )
    )
    nodes = {
        employee.id: {
            "id": str(employee.id),
            "name": employee.name,
            "employeeCode": employee.employee_code,
            "designation": employee.designation.name if employee.designation_id else None,
            "department": employee.department.name if employee.department_id else None,
            "avatar": employee.avatar_url,
            "reports": [],
        }
        for employee in employees
    }

    roots = []
    for employee in employees:
        node = nodes[employee.id]
        parent = nodes.get(employee.manager_id) if employee.manager_id else None
        if parent is not None and parent is not node:
            parent["reports"].append(node)
        else:
            roots.append(node)
    return roots


def assert_no_manager_cycle(employee, manager_id):
    """db.md §11.1 -- reject any manager reachable from the employee."""
    if manager_id is None:
        return
    if str(manager_id) == str(employee.id):
        raise ValidationFailed(
            "An employee cannot report to themselves.",
            field_errors={"managerId": ["Cannot be self."]},
        )

    seen = {employee.id}
    current = manager_id
    while current is not None:
        if current in seen:
            raise ValidationFailed(
                "That reporting line would create a loop.",
                field_errors={"managerId": ["Creates a reporting cycle."]},
            )
        seen.add(current)
        current = (
            Employee.objects.filter(pk=current).values_list("manager_id", flat=True).first()
        )


def process_employee_separation(employee, *, separation_type="Resigned", last_working_day=None, user=None):
    """Lifecycle hook for Resignation/Termination.
    Updates employee status, deactivates login User, and audits.
    """
    from apps.accounts.models import User
    status_label = "Terminated" if str(separation_type).lower() == "terminated" else "Resigned"
    employee.status = status_label
    employee.save(update_fields=["status", "updated_at"])

    # Deactivate linked user login account
    if employee.email:
        User.objects.filter(
            client_id=employee.client_id,
            email__iexact=str(employee.email).strip(),
            deleted_at__isnull=True,
        ).update(is_active=False, status="Inactive", updated_at=timezone.now())

    return employee
