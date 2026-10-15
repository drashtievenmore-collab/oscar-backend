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
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
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


def instruction_completion(instruction):
    """Full Production Completion & Verification payload.

    Derived only, never stored:
      totalProduced = SUM(entries.meters)
      acceptedQty = totalProduced - rejected_qty
      shortageQty = order_meter - acceptedQty
      finalAmount = acceptedQty * agreed_job_rate
    Daily rows carry running cumulative/balance ordered by date.
    """
    from .models import DailyProductionEntry

    entries = list(
        DailyProductionEntry.objects.filter(
            instruction=instruction, deleted_at__isnull=True
        )
        .select_related("entered_by_employee")
        .order_by("entry_date", "created_at")
    )
    order_meter = D(instruction.order_meter)
    rate = D(instruction.agreed_job_rate)
    total = ZERO
    rows = []
    running = ZERO
    for idx, entry in enumerate(entries, start=1):
        meters = D(entry.meters)
        total += meters
        running += meters
        employee = getattr(entry, "entered_by_employee", None)
        rows.append(
            {
                "n": idx,
                "id": str(entry.id),
                "date": entry.entry_date.isoformat() if entry.entry_date else None,
                "produced": meters,
                "cumulative": running,
                "balance": compute_balance(order_meter, running),
                "enteredBy": getattr(employee, "name", "") if employee else "",
                "employeeCode": getattr(employee, "employee_code", None) if employee else None,
                "remarks": entry.remarks or "-",
            }
        )

    rejected = D(getattr(instruction, "rejected_qty", 0))
    accepted = total - rejected
    if accepted < ZERO:
        accepted = ZERO
    shortage = order_meter - accepted
    employee = getattr(instruction, "employee", None)
    return {
        "piNo": instruction.instruction_number,
        "piDate": instruction.pi_date.isoformat() if getattr(instruction, "pi_date", None) else None,
        "agency": instruction.agency_name,
        "fabric": getattr(instruction, "fabric", None),
        "processType": getattr(instruction, "process_type", None),
        "assignedEmployee": getattr(employee, "name", "") if employee else "",
        "employeeCode": getattr(employee, "employee_code", None) if employee else None,
        "orderQty": order_meter,
        "totalProduced": total,
        "rejectedQty": rejected,
        "acceptedQty": accepted,
        "shortageQty": shortage,
        "jobRate": rate,
        "finalAmount": round2(accepted * rate),
        "status": instruction.status,
        "dailySummary": rows,
    }


@transaction.atomic
def create_instruction(
    *, client, employee, agency_name=None, order_reference, order_meter=ZERO,
    supervisor=None, user=None, notes=None, pi_date=None, fabric=None,
    process_type=None, agreed_job_rate=ZERO,
    agency=None, grey_lot=None, taka_count=0, total_weight=ZERO,
    width=None, gsm=None, tentative_rate=ZERO, agreed_rate=ZERO,
    rate_uom="Meter", special_terms=None, expected_start_date=None,
    status="Draft",
):
    """Create a PROD_INSTRUCTION_NEW row after the employee-status gate (Phase 2.4)."""
    from .models import ProductionInstruction

    validate_employee_for_production(employee, field="employeeId")
    if supervisor is not None:
        validate_employee_for_production(supervisor, field="supervisorId")
    if D(order_meter) < ZERO:
        raise ValidationFailed(
            "Order meters cannot be negative.",
            field_errors={"orderMeter": ["Must be zero or more."]},
        )
    if D(agreed_job_rate) < ZERO:
        raise ValidationFailed(
            "Job rate cannot be negative.",
            field_errors={"agreedJobRate": ["Must be zero or more."]},
        )

    final_rate = D(agreed_rate or agreed_job_rate or 0)
    final_agency_name = (agency.name if agency else agency_name) or "Unknown Agency"

    instruction = ProductionInstruction.objects.create(
        client=client,
        instruction_number=allocate_number(client, "PROD"),
        agency=agency,
        agency_name=final_agency_name,
        order_reference=order_reference,
        order_meter=D(order_meter),
        taka_count=int(taka_count or 0),
        total_weight=D(total_weight or 0),
        width=D(width) if width not in (None, "") else None,
        gsm=D(gsm) if gsm not in (None, "") else None,
        tentative_rate=D(tentative_rate or 0),
        agreed_rate=final_rate,
        rate_uom=rate_uom or "Meter",
        special_terms=special_terms,
        expected_start_date=expected_start_date,
        pi_date=pi_date or timezone.localdate(),
        fabric=fabric,
        process_type=process_type,
        agreed_job_rate=final_rate,
        employee=employee,
        supervisor=supervisor,
        grey_lot=grey_lot,
        status=status or "Draft",
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
        description=f"Monitoring assigned to {employee.employee_code} for {final_agency_name}.",
        after={"status": instruction.status, "employeeCode": employee.employee_code},
    )
    return instruction


@transaction.atomic
def approve_instruction(instruction, *, user=None):
    """Draft → Approved (Phase 2.4)."""
    instruction = instruction.__class__.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status != "Draft":
        raise ValidationFailed(
            f"Only a Draft instruction can be approved (currently {instruction.status}).",
            field_errors={"status": ["Must be Draft to approve."]},
        )
    before = {"status": instruction.status}
    instruction.status = "Approved"
    instruction.updated_by = user if getattr(user, "is_authenticated", False) else None
    instruction.save(update_fields=["status", "updated_by", "updated_at"])
    record_audit(
        client=instruction.client_id, actor=user, action="production_instruction_approve",
        entity_type="ProductionInstruction", entity_id=instruction.id,
        entity_label=instruction.instruction_number, description="Production instruction approved.",
        before=before, after={"status": "Approved"},
    )
    return instruction


@transaction.atomic
def start_instruction(instruction, *, user=None):
    """Approved / Not Started → Running (Phase 2.4)."""
    instruction = instruction.__class__.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status not in ("Approved", "Not Started", "Draft"):
        raise ValidationFailed(
            f"Cannot start instruction in status '{instruction.status}'.",
            field_errors={"status": ["Must be Approved or Not Started to start."]},
        )
    before = {"status": instruction.status}
    instruction.status = "Running"
    instruction.updated_by = user if getattr(user, "is_authenticated", False) else None
    instruction.save(update_fields=["status", "updated_by", "updated_at"])
    record_audit(
        client=instruction.client_id, actor=user, action="production_instruction_start",
        entity_type="ProductionInstruction", entity_id=instruction.id,
        entity_label=instruction.instruction_number, description="Production started (Running).",
        before=before, after={"status": "Running"},
    )
    return instruction


@transaction.atomic
def complete_instruction(instruction, *, user=None):
    """Mark an instruction Completed (Verification Pending)."""
    from .models import DailyProductionEntry

    instruction = instruction.__class__.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status in ("Completed", "Verification Pending", "Verified", "PO Pending", "PO Created", "Closed"):
        raise Conflict(
            "This instruction is already completed.", code=Codes.ALREADY_DONE
        )
    if instruction.status == "Draft":
        raise ValidationFailed(
            "Start production before completing.",
            field_errors={"status": ["Only an active instruction can be completed."]},
        )
    has_entries = DailyProductionEntry.objects.filter(
        instruction=instruction, deleted_at__isnull=True
    ).exists()
    if not has_entries:
        raise ValidationFailed(
            "Record at least one daily entry before completing.",
            field_errors={"dailyEntries": ["At least one daily production entry is required."]},
        )
    before = {"status": instruction.status}
    instruction.status = "Completed"
    instruction.completed_at = timezone.now()
    if getattr(user, "is_authenticated", False):
        instruction.completed_by = user
    instruction.save(update_fields=["status", "completed_at", "completed_by", "updated_at"])
    record_audit(
        client=instruction.client_id,
        actor=user,
        action="production_instruction_complete",
        entity_type="ProductionInstruction",
        entity_id=instruction.id,
        entity_label=instruction.instruction_number,
        description="Production marked completed, pending verification.",
        before=before,
        after={"status": "Completed"},
        from_value=before["status"],
        to_value="Completed",
    )
    return instruction


@transaction.atomic
def verify_instruction(instruction, *, user=None, reason=None, rejected_qty=None):
    """Approve an instruction and auto-create grey lot at QC Hold (Phase 2.4)."""
    from .models import DailyProductionEntry, ProductionInstruction
    from apps.inventory import services as stock
    from apps.inventory.models import LotTaka
    from apps.masters.models import Item

    instruction = ProductionInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status in ("Verified", "PO Created", "Closed"):
        raise Conflict(
            "This instruction is already verified.", code=Codes.ALREADY_DONE
        )
    if instruction.status in ("Draft", "Approved", "Not Started"):
        raise ValidationFailed(
            "Complete production before verifying.",
            field_errors={"status": ["Production must be recorded before verifying."]},
        )
    total = (
        DailyProductionEntry.objects.filter(
            instruction=instruction, deleted_at__isnull=True
        ).aggregate(value=Sum("meters"))["value"]
        or ZERO
    )
    rejected = D(rejected_qty) if rejected_qty is not None else D(instruction.rejected_qty)
    if rejected < ZERO:
        raise ValidationFailed(
            "Rejected quantity cannot be negative.",
            field_errors={"rejectedQty": ["Must be zero or more."]},
        )
    if total > ZERO and rejected > D(total):
        raise ValidationFailed(
            "Rejected quantity cannot exceed total produced.",
            field_errors={"rejectedQty": [f"Total produced is {total} M."]},
        )
    if total <= ZERO and rejected > ZERO:
        raise ValidationFailed(
            "No production recorded yet.",
            field_errors={"rejectedQty": ["Record daily production before rejecting."]},
        )
    before = {"status": instruction.status}
    instruction.status = "PO Pending"
    instruction.rejected_qty = rejected
    if reason is not None:
        instruction.verification_remarks = reason
    instruction.verified_by = user if getattr(user, "is_authenticated", False) else None
    instruction.verified_at = timezone.now()

    # Phase 2.4: Auto-create grey lot and taka in QC_HOLD location for accepted qty
    lot_skip_note = None
    accepted_qty = total - rejected
    if accepted_qty > ZERO and instruction.grey_lot is None:
        client = instruction.client
        qc_hold = stock.system_location(instruction.client_id, "QC_HOLD")

        # The grey lot needs the inventory item this instruction produces.
        # Match the fabric text exactly against SKU, name or quality; never
        # guess another item. With no match the lot is not created and the
        # audit entry says so.
        fabric_item = None
        fabric_text = (instruction.fabric or "").strip()
        if fabric_text:
            fabric_item = (
                Item.objects.filter(client_id=instruction.client_id, deleted_at__isnull=True)
                .exclude(item_kind="Service")
                .filter(Q(sku__iexact=fabric_text) | Q(name__iexact=fabric_text) | Q(fabric_quality__iexact=fabric_text))
                .order_by("created_at")
                .first()
            )
        if fabric_item is None:
            lot_skip_note = (
                f"Grey lot not created: no inventory item matches fabric '{fabric_text}'."
                if fabric_text else "Grey lot not created: instruction has no fabric item."
            )

        if fabric_item:
            lot_no = allocate_number(client, "LOT")
            taka_code = allocate_number(client, "TAK")
            rate = D(instruction.agreed_rate or instruction.agreed_job_rate or 0)
            lot_taka = LotTaka.objects.create(
                client_id=instruction.client_id,
                taka_id=taka_code,
                lot_number=lot_no,
                taka_number="1",
                item=fabric_item,
                source_document_type="Production",
                source_document_number=instruction.instruction_number,
                source_document_id=instruction.id,
                location=qc_hold,
                received_date=timezone.localdate(),
                received_quantity=accepted_qty,
                available_quantity=ZERO,
                uom=instruction.rate_uom or fabric_item.uom or "Meter",
                qc_status="Pending",
                status="QC Hold",
                barcode=taka_code,
                rate=rate,
                width=instruction.width,
                gsm=instruction.gsm,
                remarks=f"Produced via {instruction.instruction_number} ({instruction.agency_name})",
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
            stock.post_movement(
                client_id=instruction.client_id,
                item=fabric_item,
                location=qc_hold,
                type="PURCHASE",
                quantity=accepted_qty,
                unit_cost=rate,
                batch_number=lot_no,
                lot_taka=lot_taka,
                reference_type="ProductionInstruction",
                reference_id=instruction.id,
                reference_number=instruction.instruction_number,
                movement_date=timezone.localdate(),
                notes=f"Grey fabric received from production {instruction.instruction_number}",
                user=user,
            )
            instruction.grey_lot = lot_taka

    instruction.save(
        update_fields=[
            "status", "rejected_qty", "verification_remarks",
            "verified_by", "verified_at", "grey_lot", "updated_at",
        ]
    )
    record_audit(
        client=instruction.client_id,
        actor=user,
        action="production_instruction_verify",
        entity_type="ProductionInstruction",
        entity_id=instruction.id,
        entity_label=instruction.instruction_number,
        description=" ".join(
            filter(None, [reason or "Instruction verified, pending PO creation.", lot_skip_note])
        ),
        before=before,
        after={"status": "PO Pending", "rejectedQty": str(rejected), "totalProduced": str(total)},
        from_value=before["status"],
        to_value="PO Pending",
        comments=reason,
    )
    return instruction


@transaction.atomic
def generate_final_job_work_po(instruction, *, user=None):
    """Generate final job work PO against verified production (Phase 2.4)."""
    from apps.purchase.models import PurchaseOrder, PurchaseOrderLine
    from apps.masters.models import Party, Item

    instruction = instruction.__class__.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status not in ("PO Pending", "Verified", "Completed"):
        raise ValidationFailed(
            f"Instruction must be verified before generating PO (currently {instruction.status}).",
            field_errors={"status": ["Must be PO Pending / Verified."]},
        )
    if instruction.final_po_id:
        raise Conflict("Final PO has already been created for this instruction.", code=Codes.ALREADY_DONE)

    completion = instruction_completion(instruction)
    accepted_qty = round4(D(completion["acceptedQty"]))
    rate = D(instruction.agreed_rate or instruction.agreed_job_rate or 0)
    if accepted_qty <= ZERO:
        raise ValidationFailed(
            "No accepted production to raise a PO for.",
            field_errors={"acceptedQty": ["Accepted quantity is zero."]},
        )
    if rate <= ZERO:
        raise ValidationFailed(
            "Set the agreed rate before generating the final PO.",
            field_errors={"agreedRate": ["Agreed rate is required."]},
        )

    # The agency must exist as a vendor party (same name); never guess one.
    agency_name = (getattr(instruction.agency, "name", None) or instruction.agency_name or "").strip()
    vendor = (
        Party.objects.filter(
            client_id=instruction.client_id, deleted_at__isnull=True,
            type__in=["Vendor", "Both"], name__iexact=agency_name,
        ).order_by("created_at").first()
        if agency_name else None
    )
    if vendor is None:
        raise ValidationFailed(
            f"No vendor party named '{agency_name}' exists. Create the agency as a vendor first.",
            field_errors={"agencyId": ["Agency is not set up as a vendor."]},
        )

    fabric_text = (instruction.fabric or "").strip()
    fabric_item = (
        Item.objects.filter(client_id=instruction.client_id, deleted_at__isnull=True)
        .exclude(item_kind="Service")
        .filter(Q(sku__iexact=fabric_text) | Q(name__iexact=fabric_text) | Q(fabric_quality__iexact=fabric_text))
        .order_by("created_at")
        .first()
        if fabric_text else None
    )
    if fabric_item is None:
        raise ValidationFailed(
            f"No inventory item matches fabric '{fabric_text}'.",
            field_errors={"fabric": ["Set the fabric to an existing item."]},
        )

    if fabric_item.tax_pct is None:
        raise ValidationFailed(
            f"Set the GST % on item '{fabric_item.name}' before generating the PO.",
            field_errors={"fabric": ["Item has no GST %."]},
        )

    from apps.sales.services import recalculate_document

    po_no = allocate_number(instruction.client, "PO")
    po = PurchaseOrder(
        client=instruction.client,
        po_number=po_no,
        party=vendor,
        doc_date=timezone.localdate(),
        # Final PO waits for approval (Phase 4 engine will own this step).
        status="Pending Approval",
        reference_number=instruction.instruction_number,
        notes=f"Final Job Work PO for Production Instruction {instruction.instruction_number}",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    po.freeze_party_snapshot()
    po.save()
    line = PurchaseOrderLine(
        client=instruction.client,
        purchase_order=po,
        line_no=1,
        item=fabric_item,
        qty=accepted_qty,
        rate=rate,
        tax_pct=fabric_item.tax_pct,
        uom=instruction.rate_uom or fabric_item.uom,
    )
    line.freeze_item_snapshot()
    line.save()
    recalculate_document(po)

    instruction.final_po = po
    instruction.status = "PO Created"
    instruction.save(update_fields=["final_po", "status", "updated_at"])

    record_audit(
        client=instruction.client_id,
        actor=user,
        action="production_instruction_generate_po",
        entity_type="ProductionInstruction",
        entity_id=instruction.id,
        entity_label=instruction.instruction_number,
        description=f"Final Job Work PO {po_no} generated ({accepted_qty} M @ ₹{rate}).",
        after={"finalPo": po_no, "status": "PO Created"},
    )
    return instruction


def agency_three_way_match(instruction, invoice_qty, invoice_rate):
    """Agency bill 3-way match: invoice vs verified production vs final PO (Phase 2.4)."""
    completion = instruction_completion(instruction)
    verified_qty = D(completion["acceptedQty"])
    agreed_rate = D(instruction.agreed_rate or instruction.agreed_job_rate or 0)

    po_qty = verified_qty
    po_rate = agreed_rate
    if instruction.final_po:
        line = instruction.final_po.line_items.first()
        if line:
            po_qty = D(line.qty)
            po_rate = D(line.rate)

    inv_q = D(invoice_qty or 0)
    inv_r = D(invoice_rate or 0)

    qty_diff = inv_q - verified_qty
    rate_diff = inv_r - agreed_rate
    matched = (abs(qty_diff) <= Decimal("0.01")) and (abs(rate_diff) <= Decimal("0.01"))

    return {
        "instructionNo": instruction.instruction_number,
        "poNumber": instruction.final_po.po_number if instruction.final_po else None,
        "verifiedQty": float(verified_qty),
        "poQty": float(po_qty),
        "invoiceQty": float(inv_q),
        "qtyDifference": float(qty_diff),
        "agreedRate": float(agreed_rate),
        "poRate": float(po_rate),
        "invoiceRate": float(inv_r),
        "rateDifference": float(rate_diff),
        "matched": matched,
        "requiresApproval": not matched,
    }


LOCKED_INSTRUCTION_STATUSES = ("Verified", "PO Pending", "PO Created", "Closed")


def lock_open_instruction(instruction):
    """Row-lock the instruction and refuse it once verified or closed.

    Called by every daily-entry create/update/delete so an entry can never
    change after verification (the lock serialises against verify).
    """
    from .models import ProductionInstruction

    instruction = ProductionInstruction.objects.select_for_update().get(pk=instruction.pk)
    if instruction.status in LOCKED_INSTRUCTION_STATUSES:
        raise ValidationFailed(
            f"Daily production entries cannot change on an instruction with status '{instruction.status}'.",
            field_errors={"instructionId": [f"Instruction is already {instruction.status}."]},
        )
    if instruction.status == "Draft":
        raise ValidationFailed(
            "Approve the production instruction before recording production.",
            field_errors={"instructionId": ["Instruction is still Draft."]},
        )
    return instruction


@transaction.atomic
def record_daily_entry(
    *, client, instruction, entry_date, meters, entered_by_employee,
    entered_by_user=None, remarks=None,
    produced_takas=0, produced_weight=ZERO, good_meter=ZERO,
    reject_meter=ZERO, reprocess_meter=ZERO, photo_file=None,
):
    """Post a DAILY_PROD_ENTRY_NEW row with computed cumulative & balance (Phase 2.4)."""
    instruction = lock_open_instruction(instruction)
    validate_employee_for_production(entered_by_employee, field="enteredByEmployeeId")
    if D(meters) <= ZERO:
        raise ValidationFailed(
            "Meters must be more than zero.",
            field_errors={"meters": ["Must be more than zero."]},
        )

    from .models import DailyProductionEntry

    # Compute previous cumulative
    prev_cum = DailyProductionEntry.objects.filter(
        instruction=instruction, deleted_at__isnull=True
    ).aggregate(value=Sum("meters"))["value"] or ZERO

    opening_pending = compute_balance(instruction.order_meter, prev_cum)
    cumulative_meter = D(prev_cum) + D(meters)
    balance_meter = compute_balance(instruction.order_meter, cumulative_meter)

    entry = DailyProductionEntry.objects.create(
        client=client,
        instruction=instruction,
        entry_date=entry_date,
        opening_pending=opening_pending,
        meters=D(meters),
        produced_takas=int(produced_takas or 0),
        produced_weight=D(produced_weight or 0),
        good_meter=D(good_meter or meters),
        reject_meter=D(reject_meter or 0),
        reprocess_meter=D(reprocess_meter or 0),
        cumulative_meter=cumulative_meter,
        balance_meter=balance_meter,
        photo_file=photo_file,
        entered_by_employee=entered_by_employee,
        entered_by_user=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
        remarks=remarks,
        created_by=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
    )

    # Update instruction status
    if instruction.status in ("Approved", "Not Started"):
        instruction.status = "Running"
    elif balance_meter <= ZERO:
        instruction.status = "Completed"
    else:
        instruction.status = "Partially Completed"
    instruction.save(update_fields=["status", "updated_at"])

    return entry


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Incentive (kept apart from payroll on purpose) (PDF §12)
# ---------------------------------------------------------------------------
def sales_totals_for_period(
    client_id, start, end, employee=None, exclude_gst=False, basis="INVOICE"
):
    """Net sales = posted invoices minus posted returns/credit notes.
    Optionally filters by salesperson/employee and calculates collection / gross profit.
    """
    from decimal import Decimal
    from django.db.models import DecimalField, Value
    from django.db.models.functions import Coalesce
    from apps.sales.models import SalesInvoice, SalesReturn, PaymentAllocation

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

    if employee is not None:
        emp_filter = Q(salesperson_fk=employee)
        if getattr(employee, "name", None):
            emp_filter |= Q(salesperson__iexact=employee.name)
        if getattr(employee, "employee_code", None):
            emp_filter |= Q(salesperson__iexact=employee.employee_code)

        emp_invoices = invoices.filter(emp_filter)
        emp_returns = returns.filter(emp_filter)

        # If invoices in the workspace carry salesperson tags, isolate this employee's
        if emp_invoices.exists():
            invoices = emp_invoices
            returns = emp_returns
        elif invoices.filter(Q(salesperson_fk__isnull=False) | (Q(salesperson__isnull=False) & ~Q(salesperson=""))).exists():
            invoices = emp_invoices
            returns = emp_returns

    invoice_ids = list(invoices.values_list("id", flat=True))
    invoice_numbers = list(
        invoices.exclude(invoice_number__isnull=True).values_list(
            "invoice_number", flat=True
        )
    )

    if exclude_gst:
        gross_sales = sum(
            (
                D(inv.taxable_value) if inv.taxable_value and D(inv.taxable_value) > ZERO
                else (D(inv.total) - D(inv.total_tax) if inv.total_tax else D(inv.total))
                for inv in invoices
            ),
            ZERO,
        )
        returns_total = sum(
            (
                D(ret.taxable_value) if hasattr(ret, "taxable_value") and ret.taxable_value and D(ret.taxable_value) > ZERO
                else (D(ret.total) - D(getattr(ret, "total_tax", 0)) if getattr(ret, "total_tax", 0) else D(ret.total))
                for ret in returns
            ),
            ZERO,
        )
    else:
        gross_sales = invoices.aggregate(val=Coalesce(Sum("total"), Value(Decimal("0.00")), output_field=DecimalField()))["val"] or ZERO
        returns_total = returns.aggregate(val=Coalesce(Sum("total"), Value(Decimal("0.00")), output_field=DecimalField()))["val"] or ZERO

    discount_total = invoices.aggregate(val=Coalesce(Sum("total_discount"), Value(Decimal("0.00")), output_field=DecimalField()))["val"] or ZERO

    # Collection total from payment allocations
    allocations = PaymentAllocation.objects.filter(
        client_id=client_id,
        payment_side="in",
        document_type="SalesInvoice",
        document_id__in=invoice_ids,
    )
    collection_total = allocations.aggregate(val=Coalesce(Sum("amount"), Value(Decimal("0.00")), output_field=DecimalField()))["val"] or ZERO

    # Gross profit = (selling amount - landed cost)
    gross_profit = ZERO
    for inv in invoices.prefetch_related("line_items", "line_items__item"):
        for line in inv.line_items.all():
            selling_val = D(line.amount or line.line_total or 0)
            cost_val = D(line.landed_cost or (line.item.cost_price if line.item else 0)) * D(line.qty)
            gross_profit += (selling_val - cost_val)
    gross_profit = max(ZERO, gross_profit - D(returns_total))

    total_invoiced = sum((D(inv.total) for inv in invoices), ZERO)
    total_paid = sum((D(inv.amount_paid) for inv in invoices), ZERO)

    return {
        "gross_sales": round2(gross_sales),
        "discount": round2(discount_total),
        "returns": round2(returns_total),
        "collection": round2(collection_total),
        "net_sales": round2(max(ZERO, D(gross_sales) - D(returns_total))),
        "gross_profit": round2(gross_profit),
        "total_invoiced": round2(total_invoiced),
        "total_paid": round2(total_paid),
        "invoice_ids": invoice_ids,
        "invoice_numbers": invoice_numbers,
    }


def compute_scheme_incentive(scheme, base_amount):
    """Calculate incentive on base_amount according to scheme slab_type and slabs."""
    base_val = round2(D(base_amount or 0))
    if base_val <= ZERO:
        return ZERO

    slabs = scheme.slabs or []
    slab_type = scheme.slab_type or "FLAT"

    if slab_type == "TIERED" and slabs:
        sorted_slabs = sorted(slabs, key=lambda s: D(s.get("min", 0)))
        total_inc = ZERO
        for slab in sorted_slabs:
            s_min = D(slab.get("min", 0))
            s_max = D(slab.get("max")) if slab.get("max") not in (None, "") else None
            rate = D(slab.get("rate_pct") or slab.get("rate") or 0)
            if base_val > s_min:
                cap = (min(base_val, s_max) - s_min) if s_max is not None else (base_val - s_min)
                if cap > ZERO:
                    total_inc += round2(cap * rate / D(100))
        return total_inc

    elif slabs:  # FLAT with slab tiers
        sorted_slabs = sorted(slabs, key=lambda s: D(s.get("min", 0)))
        matched_rate = D(scheme.rate_pct or 0)
        for slab in sorted_slabs:
            s_min = D(slab.get("min", 0))
            s_max = D(slab.get("max")) if slab.get("max") not in (None, "") else None
            if base_val >= s_min and (s_max is None or base_val <= s_max):
                matched_rate = D(slab.get("rate_pct") or slab.get("rate") or 0)
                break
        return round2(base_val * matched_rate / D(100))

    else:
        return round2(base_val * D(scheme.rate_pct or 0) / D(100))


@transaction.atomic
def calculate_incentive(
    *, client, scheme, employee, period_label, period_start, period_end, user=None, auto_post=True,
):
    """Compute incentive into INCENTIVE_CALC_NEW and post its own journal entry (PDF §12)."""
    from apps.accounting import services as ledger
    from .models import IncentiveCalculation

    if not scheme.is_active:
        raise BusinessRuleViolation(
            f"The scheme {scheme.name} is not active.",
            code="INACTIVE_INCENTIVE_SCHEME",
        )
    validate_employee_for_production(employee, field="employeeId")

    basis = scheme.basis or "INVOICE"
    exclude_gst = getattr(scheme, "exclude_gst", False)
    hold_unpaid = getattr(scheme, "hold_unpaid_invoices", False)

    client_id = getattr(client, "id", client)
    totals = sales_totals_for_period(
        client_id, period_start, period_end,
        employee=employee, exclude_gst=exclude_gst, basis=basis
    )

    if basis == "COLLECTION":
        base_amount = totals["collection"]
    elif basis == "GROSS_PROFIT":
        base_amount = totals["gross_profit"]
    else:  # INVOICE
        base_amount = totals["net_sales"]

    total_incentive = compute_scheme_incentive(scheme, base_amount)

    hold_amount = ZERO
    if hold_unpaid and totals["total_invoiced"] > ZERO:
        unpaid_amount = max(ZERO, totals["total_invoiced"] - totals["total_paid"])
        unpaid_ratio = unpaid_amount / totals["total_invoiced"]
        hold_amount = round2(total_incentive * unpaid_ratio)

    payable_now = max(ZERO, total_incentive - hold_amount)

    calc = IncentiveCalculation.objects.create(
        client=client,
        calc_number=allocate_number(client, "INC"),
        scheme=scheme,
        employee=employee,
        salesperson=employee,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        basis=basis,
        net_sales=totals["net_sales"],
        returns_total=totals["returns"],
        incentive_amount=total_incentive,
        hold_amount=hold_amount,
        paid_amount=ZERO,
        status="Calculated",
        detail={
            "basis": basis,
            "baseAmount": str(base_amount),
            "ratePct": str(scheme.rate_pct),
            "slabType": scheme.slab_type,
            "invoiceIds": [str(pk) for pk in totals["invoice_ids"]],
            "invoiceNumbers": totals["invoice_numbers"],
            "discount": str(totals["discount"]),
            "collection": str(totals["collection"]),
            "grossProfit": str(totals["gross_profit"]),
            "totalInvoiced": str(totals["total_invoiced"]),
            "totalPaid": str(totals["total_paid"]),
            "holdAmount": str(hold_amount),
            "payableAmount": str(payable_now),
        },
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if auto_post and total_incentive > ZERO:
        entry = ledger.post_entry(
            client=client,
            postings=[
                ledger.debit(
                    ledger.system_account(client_id, "general_expense"),
                    payable_now if hold_amount > ZERO else total_incentive,
                    description=f"Incentive {calc.calc_number} - {employee.name}",
                ),
                ledger.credit(
                    ledger.system_account(client_id, "salary_payable"),
                    payable_now if hold_amount > ZERO else total_incentive,
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
        client=client_id,
        actor=user,
        action="incentive_calculate",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive {total_incentive} (Hold: {hold_amount}) for {employee.employee_code} ({period_label}).",
        after={"netSales": str(totals["net_sales"]), "incentive": str(total_incentive), "status": calc.status},
    )
    return calc


@transaction.atomic
def approve_incentive(calc, *, user=None):
    """Calculated → Approved (PDF §12)."""
    calc = calc.__class__.objects.select_for_update().get(pk=calc.pk)
    if calc.status != "Calculated":
        raise ValidationFailed(
            f"Only Calculated incentives can be approved (currently {calc.status}).",
            field_errors={"status": ["Must be in Calculated status."]},
        )
    calc.status = "Approved"
    calc.approved_by = user if getattr(user, "is_authenticated", False) else None
    calc.approved_at = timezone.now()
    calc.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_approve",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive {calc.calc_number} approved.",
        after={"status": "Approved"},
    )
    return calc


@transaction.atomic
def post_incentive(calc, *, user=None):
    """Approved → Posted (PDF §12). Posts to ledger."""
    from apps.accounting import services as ledger
    calc = calc.__class__.objects.select_for_update().get(pk=calc.pk)
    if calc.status not in ("Approved", "Calculated"):
        raise ValidationFailed(
            f"Only Approved incentives can be posted (currently {calc.status}).",
            field_errors={"status": ["Must be Approved before posting."]},
        )

    payable_amount = calc.incentive_amount - calc.hold_amount
    if payable_amount > ZERO and calc.journal_entry is None:
        client_id = calc.client_id
        expense_acc = ledger.system_account(client_id, "general_expense")
        payable_acc = ledger.system_account(client_id, "salary_payable")
        entry = ledger.post_entry(
            client=calc.client,
            postings=[
                ledger.debit(
                    expense_acc,
                    payable_amount,
                    description=f"Incentive {calc.calc_number} - {calc.employee.name}",
                ),
                ledger.credit(
                    payable_acc,
                    payable_amount,
                    description=f"Incentive payable {calc.period_label}",
                ),
            ],
            narration=f"Sales incentive {calc.calc_number} ({calc.period_label})",
            source_document_type="IncentiveCalc",
            source_document_id=calc.id,
            user=user,
        )
        calc.journal_entry = entry

    calc.status = "Posted"
    calc.save(update_fields=["status", "journal_entry", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_post",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive {calc.calc_number} posted to ledger.",
        after={"status": "Posted"},
    )
    return calc


@transaction.atomic
def pay_incentive(calc, *, bank_account=None, amount=None, user=None):
    """Posted → Paid (PDF §12). Posts Dr Incentive Payable / Cr Bank."""
    from apps.accounting import services as ledger
    calc = calc.__class__.objects.select_for_update().get(pk=calc.pk)
    if calc.status not in ("Posted", "Approved"):
        raise ValidationFailed(
            f"Incentive must be Posted before payment (currently {calc.status}).",
            field_errors={"status": ["Must be Posted."]},
        )

    payable_amount = calc.incentive_amount - calc.hold_amount
    pay_amount = D(amount) if amount is not None else (payable_amount - D(calc.paid_amount))
    if pay_amount <= ZERO:
        raise ValidationFailed("Payment amount must be greater than zero.")

    if bank_account:
        client_id = calc.client_id
        payable_acc = ledger.system_account(client_id, "salary_payable")
        bank_ledger_acc = bank_account.account if hasattr(bank_account, "account") and bank_account.account else ledger.system_account(client_id, "bank")
        ledger.post_entry(
            client=calc.client,
            postings=[
                ledger.debit(payable_acc, pay_amount, description=f"Incentive payment {calc.calc_number}"),
                ledger.credit(bank_ledger_acc, pay_amount, description=f"Bank payout for {calc.calc_number}"),
            ],
            narration=f"Payout for sales incentive {calc.calc_number}",
            source_document_type="IncentivePayout",
            source_document_id=calc.id,
            user=user,
        )

    calc.paid_amount = D(calc.paid_amount or 0) + pay_amount
    if calc.paid_amount >= payable_amount:
        calc.status = "Paid"
    calc.paid_at = timezone.now()
    calc.save(update_fields=["status", "paid_amount", "paid_at", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_pay",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive {calc.calc_number} paid {pay_amount}.",
        after={"status": calc.status, "paidAmount": str(calc.paid_amount)},
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
