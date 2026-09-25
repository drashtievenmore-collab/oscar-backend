"""
Grey fabric production monitoring, hosted in PMS.

Rules enforced here, not in HRMS:

  - only an ``Employee`` with status Active / On Leave / Probation can be
    assigned production work; Resigned / Terminated is rejected
  - ``balance`` is always computed as order_meter minus cumulative entries
  - incentive is computed from net sales and posted as its own journal entry;
    ``apps/hrms/services.py`` is never touched and ``post_payroll`` is never
    called from incentive code
"""
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

#: HRMS_ERP_CHANGES.md -- the only employee states that may take production work.
ACTIVE_PRODUCTION_STATUSES = ["Active", "On Leave", "Probation"]
EXITED_STATUSES = ["Resigned", "Terminated"]


def active_employee_queryset(client_id):
    """The selector query from HRMS_ERP_CHANGES.md."""
    from apps.hrms.models import Employee

    return Employee.objects.filter(
        client_id=client_id,
        status__in=ACTIVE_PRODUCTION_STATUSES,
        deleted_at__isnull=True,
    ).order_by("name")


def validate_employee_for_production(employee, *, field="employeeId"):
    """Reject missing or exited employees with a field-level error."""
    if employee is None:
        raise ValidationFailed(
            "Select an employee.",
            field_errors={field: ["This employee does not exist."]},
        )
    if employee.status in EXITED_STATUSES:
        raise ValidationFailed(
            f"{employee.name} is {employee.status} and cannot take production work.",
            code="EXITED_EMPLOYEE",
            field_errors={field: [f"Employee is {employee.status}."]},
        )
    if employee.status not in ACTIVE_PRODUCTION_STATUSES:
        raise ValidationFailed(
            f"{employee.name} has status {employee.status}.",
            field_errors={field: [f"Expected one of {', '.join(ACTIVE_PRODUCTION_STATUSES)}."]},
        )
    return employee


def compute_balance(order_meter, cumulative):
    """``balance = order_meter - cumulative`` -- always in code, never stored."""
    return D(order_meter) - D(cumulative)


def instruction_progress(instruction):
    """Cumulative and balance for one instruction, computed from its entries."""
    from .models import DailyProductionEntry

    cumulative = (
        DailyProductionEntry.objects.filter(
            instruction=instruction, deleted_at__isnull=True
        ).aggregate(value=Sum("meters"))["value"]
        or ZERO
    )
    order_meter = D(instruction.order_meter)
    return {
        "orderMeter": order_meter,
        "cumulative": cumulative,
        "balance": compute_balance(order_meter, cumulative),
    }


@transaction.atomic
def create_instruction(
    *, client, employee, agency_name, order_reference, order_meter=ZERO,
    supervisor=None, user=None, notes=None,
):
    """Create a PROD_INSTRUCTION_NEW row after the employee-status gate."""
    from .models import ProductionInstruction

    validate_employee_for_production(employee, field="employeeId")
    if supervisor is not None:
        validate_employee_for_production(supervisor, field="supervisorId")
    if D(order_meter) < ZERO:
        raise ValidationFailed(
            "Order meters cannot be negative.",
            field_errors={"orderMeter": ["Must be zero or more."]},
        )

    instruction = ProductionInstruction.objects.create(
        client=client,
        instruction_number=allocate_number(client, "PROD"),
        agency_name=agency_name,
        order_reference=order_reference,
        order_meter=D(order_meter),
        employee=employee,
        supervisor=supervisor,
        status="In Progress",
        notes=notes,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        updated_by=user if getattr(user, "is_authenticated", False) else None,
    )
    record_audit(
        client=client.id if hasattr(client, "id") else client,
        actor=user,
        action="production_instruction_create",
        entity_type="ProductionInstruction",
        entity_id=instruction.id,
        entity_label=instruction.instruction_number,
        description=f"Monitoring assigned to {employee.employee_code} for {agency_name}.",
        after={"status": instruction.status, "employeeCode": employee.employee_code},
    )
    return instruction


@transaction.atomic
def verify_instruction(instruction, *, user=None, reason=None):
    """Approve an instruction. Every approval lands in ``core.AUDIT_LOG``."""
    from .models import ProductionInstruction

    instruction = ProductionInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status == "Verified":
        raise Conflict(
            "This instruction is already verified.", code=Codes.ALREADY_DONE
        )
    before = {"status": instruction.status}
    instruction.status = "Verified"
    instruction.verified_by = user if getattr(user, "is_authenticated", False) else None
    instruction.verified_at = timezone.now()
    instruction.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
    record_audit(
        client=instruction.client_id,
        actor=user,
        action="production_instruction_verify",
        entity_type="ProductionInstruction",
        entity_id=instruction.id,
        entity_label=instruction.instruction_number,
        description=reason or "Instruction verified.",
        before=before,
        after={"status": "Verified"},
        from_value=before["status"],
        to_value="Verified",
        comments=reason,
    )
    return instruction


@transaction.atomic
def record_daily_entry(
    *, client, instruction, entry_date, meters, entered_by_employee,
    entered_by_user=None, remarks=None,
):
    """Post a DAILY_PROD_ENTRY_NEW row with employee code and timestamp."""
    from .models import DailyProductionEntry

    validate_employee_for_production(entered_by_employee, field="enteredByEmployeeId")
    if D(meters) <= ZERO:
        raise ValidationFailed(
            "Meters must be more than zero.",
            field_errors={"meters": ["Must be more than zero."]},
        )

    return DailyProductionEntry.objects.create(
        client=client,
        instruction=instruction,
        entry_date=entry_date,
        meters=D(meters),
        entered_by_employee=entered_by_employee,
        entered_by_user=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
        remarks=remarks,
        created_by=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
    )


# ---------------------------------------------------------------------------
# Incentive (kept apart from payroll on purpose)
# ---------------------------------------------------------------------------
def sales_totals_for_period(client_id, start, end):
    """Net sales = posted invoices minus posted returns/credit notes."""
    from apps.sales.models import SalesInvoice, SalesReturn

    invoices = SalesInvoice.objects.filter(
        client_id=client_id,
        doc_date__gte=start,
        doc_date__lte=end,
        deleted_at__isnull=True,
    ).exclude(status__in=["Draft", "Cancelled"])
    returns = SalesReturn.objects.filter(
        client_id=client_id,
        doc_date__gte=start,
        doc_date__lte=end,
        deleted_at__isnull=True,
        status="Posted",
    )
    invoice_total = invoices.aggregate(value=Sum("total"))["value"] or ZERO
    discount_total = invoices.aggregate(value=Sum("total_discount"))["value"] or ZERO
    collection_total = invoices.aggregate(value=Sum("amount_paid"))["value"] or ZERO
    returns_total = returns.aggregate(value=Sum("total"))["value"] or ZERO
    return {
        "gross_sales": round2(invoice_total),
        "discount": round2(discount_total),
        "returns": round2(returns_total),
        "collection": round2(collection_total),
        "net_sales": round2(D(invoice_total) - D(returns_total)),
        "invoice_ids": list(invoices.values_list("id", flat=True)),
        "invoice_numbers": list(
            invoices.exclude(invoice_number__isnull=True).values_list(
                "invoice_number", flat=True
            )
        ),
    }


@transaction.atomic
def calculate_incentive(
    *, client, scheme, employee, period_label, period_start, period_end, user=None,
):
    """Compute incentive into INCENTIVE_CALC_NEW and post its own journal entry.

    Never writes ``Payslip.additional_earnings`` and never calls
    ``post_payroll`` -- payroll stays salary-only.
    """
    from apps.accounting import services as ledger

    from .models import IncentiveCalculation

    if not scheme.is_active:
        raise BusinessRuleViolation(
            f"The scheme {scheme.name} is not active.",
            code="INACTIVE_INCENTIVE_SCHEME",
        )
    validate_employee_for_production(employee, field="employeeId")

    totals = sales_totals_for_period(
        getattr(client, "id", client), period_start, period_end
    )
    amount = round2(D(totals["net_sales"]) * D(scheme.rate_pct) / D(100))

    calc = IncentiveCalculation.objects.create(
        client=client,
        calc_number=allocate_number(client, "INC"),
        scheme=scheme,
        employee=employee,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        net_sales=totals["net_sales"],
        returns_total=totals["returns"],
        incentive_amount=amount,
        status="Calculated",
        detail={
            "ratePct": str(scheme.rate_pct),
            "invoiceIds": [str(pk) for pk in totals["invoice_ids"]],
            "invoiceNumbers": totals["invoice_numbers"],
            "discount": str(totals["discount"]),
        },
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if amount > ZERO:
        entry = ledger.post_entry(
            client=client,
            postings=[
                ledger.debit(
                    ledger.system_account(getattr(client, "id", client), "general_expense"),
                    amount,
                    description=f"Incentive {calc.calc_number} - {employee.name}",
                ),
                ledger.credit(
                    ledger.system_account(getattr(client, "id", client), "salary_payable"),
                    amount,
                    description=f"Incentive payable {period_label}",
                ),
            ],
            narration=f"Sales incentive {calc.calc_number} ({period_label})",
            source_document_type="IncentiveCalc",
            source_document_id=calc.id,
            user=user,
        )
        if entry is not None:
            calc.journal_entry = entry
            calc.status = "Posted"
            calc.save(update_fields=["journal_entry", "status", "updated_at"])

    record_audit(
        client=getattr(client, "id", client),
        actor=user,
        action="incentive_calculate",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive {amount} for {employee.employee_code} ({period_label}).",
        after={"netSales": str(totals["net_sales"]), "incentive": str(amount)},
    )
    return calc


@transaction.atomic
def reverse_incentive(calc, *, user=None, reason=None):
    """Reverse incentive when the invoice becomes a return, credit note, or bad debt."""
    from apps.accounting import services as ledger

    from .models import IncentiveCalculation

    calc = IncentiveCalculation.objects.select_for_update().get(pk=calc.pk)
    if calc.status == "Reversed":
        raise Conflict("This incentive is already reversed.", code=Codes.ALREADY_DONE)

    if calc.journal_entry_id is not None:
        ledger.reverse_document_entries(
            client_id=calc.client_id,
            source_document_type="IncentiveCalc",
            source_document_id=calc.id,
            user=user,
        )
    before = {"status": calc.status}
    calc.status = "Reversed"
    calc.reversed_at = timezone.now()
    calc.reversal_reason = reason
    calc.save(update_fields=["status", "reversed_at", "reversal_reason", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_reverse",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=reason or "Incentive reversed.",
        before=before,
        after={"status": "Reversed"},
        from_value=before["status"],
        to_value="Reversed",
        comments=reason,
    )
    return calc


def reverse_incentives_for_invoice(invoice, *, user=None, reason=None):
    """Find posted calcs built on an invoice and reverse each of them."""
    from .models import IncentiveCalculation

    reversed_calcs = []
    for calc in IncentiveCalculation.objects.filter(
        client_id=invoice.client_id, status="Posted", deleted_at__isnull=True
    ):
        invoice_ids = (calc.detail or {}).get("invoiceIds") or []
        if str(invoice.id) in {str(pk) for pk in invoice_ids}:
            reversed_calcs.append(
                reverse_incentive(
                    calc,
                    user=user,
                    reason=reason or f"Invoice {invoice.invoice_number} returned/credited.",
                )
            )
    return reversed_calcs
