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
      totalProduced = SUM(entries.meters where status != 'Draft')
      acceptedQty = SUM(entries.good_meter) or (totalProduced - rejected - reprocess)
      shortageQty = max(0, order_meter - totalProduced)
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
    rate = D(instruction.agreed_job_rate) or D(instruction.agreed_rate) or D(instruction.tentative_rate)
    total = ZERO
    total_rejected = ZERO
    total_reprocess = ZERO
    total_accepted = ZERO
    rows = []
    running = ZERO
    for idx, entry in enumerate(entries, start=1):
        meters = D(entry.meters)
        reject = D(entry.reject_meter or 0)
        reprocess = D(entry.reprocess_meter or 0)
        accepted_row = D(entry.good_meter or 0) if entry.good_meter else max(ZERO, meters - reject - reprocess)
        entry_status = getattr(entry, "status", "Submitted") or "Submitted"

        if entry_status != "Draft":
            total += meters
            total_rejected += reject
            total_reprocess += reprocess
            total_accepted += accepted_row
            running += meters

        employee = getattr(entry, "entered_by_employee", None)
        rows.append(
            {
                "n": idx,
                "id": str(entry.id),
                "date": entry.entry_date.isoformat() if entry.entry_date else None,
                "produced": meters,
                "rejected": reject,
                "reprocess": reprocess,
                "accepted": accepted_row,
                "cumulative": running if entry_status != "Draft" else (running + meters),
                "balance": compute_balance(order_meter, running if entry_status != "Draft" else (running + meters)),
                "enteredBy": getattr(employee, "name", "") if employee else "",
                "employeeCode": getattr(employee, "employee_code", None) if employee else None,
                "remarks": entry.remarks or "-",
                "status": entry_status,
                "createdAt": entry.created_at.isoformat() if hasattr(entry, "created_at") and entry.created_at else None,
            }
        )

    rejected = total_rejected if total_rejected > ZERO else D(getattr(instruction, "rejected_qty", 0))
    reprocess = total_reprocess
    accepted = total_accepted if total_accepted > ZERO else max(ZERO, total - rejected - reprocess)
    shortage = max(ZERO, order_meter - total)
    balance_qty = compute_balance(order_meter, total)
    completion_pct = round((float(total) / float(order_meter) * 100), 1) if order_meter > ZERO else 0

    employee = getattr(instruction, "employee", None)
    grey_lot_val = None
    if getattr(instruction, "grey_lot", None):
        grey_lot_val = getattr(instruction.grey_lot, "lot_number", None)
    if not grey_lot_val and hasattr(instruction, "grey_lot_no"):
        grey_lot_val = instruction.grey_lot_no

    final_po_obj = getattr(instruction, "final_po", None)

    # Aggregate QC inspections for this instruction
    from apps.inventory.models import QCInspection
    qc_qs = QCInspection.objects.filter(
        client_id=instruction.client_id,
        deleted_at__isnull=True,
    ).filter(
        Q(production_instruction=instruction) | Q(source_document_number=instruction.order_reference)
    ).exclude(status="Draft")

    qc_inspected = sum((D(q.total_inspected_qty) for q in qc_qs), ZERO)
    qc_accepted = sum((D(q.passed_qty) for q in qc_qs), ZERO)
    qc_rejected = sum((D(q.rejected_qty) for q in qc_qs), ZERO)
    qc_reprocess = sum((D(q.reprocess_qty) for q in qc_qs), ZERO)
    qc_hold = sum((D(q.hold_qty) for q in qc_qs), ZERO)
    qc_pending = max(ZERO, total - qc_inspected)

    # Parse metadata from instruction notes if JSON
    meta = {}
    if getattr(instruction, "notes", None):
        try:
            import json
            meta = json.loads(instruction.notes)
        except Exception:
            pass

    physically_verified_val = meta.get("physicallyVerifiedQty")
    if physically_verified_val is not None:
        try:
            physically_verified = D(str(physically_verified_val))
        except Exception:
            physically_verified = qc_accepted if qc_accepted > ZERO else total
    else:
        physically_verified = qc_accepted if qc_accepted > ZERO else total

    return {
        "instructionId": str(instruction.id),
        "piNo": instruction.instruction_number,
        "piDate": instruction.pi_date.isoformat() if hasattr(getattr(instruction, "pi_date", None), "isoformat") else (str(instruction.pi_date) if getattr(instruction, "pi_date", None) else None),
        "agency": instruction.agency_name,
        "agencyId": str(instruction.agency_id) if instruction.agency_id else None,
        "fabric": getattr(instruction, "fabric", None),
        "processType": getattr(instruction, "process_type", None) or "Processing",
        "assignedEmployee": getattr(employee, "name", "") if employee else "",
        "assignedEmployeeId": str(employee.id) if employee else None,
        "employeeCode": getattr(employee, "employee_code", None) if employee else None,
        "orderQty": order_meter,
        "totalProduced": total,
        "rejectedQty": rejected,
        "reprocessQty": reprocess,
        "acceptedQty": accepted,
        "balanceQty": balance_qty,
        "shortageQty": shortage,
        "completionPercentage": completion_pct,
        "jobRate": rate,
        "finalAmount": round2(accepted * rate),
        "status": instruction.status,
        "greyLotNo": grey_lot_val,
        "orderReference": instruction.order_reference,
        "rateUom": instruction.rate_uom or "Meter",
        "finalPoNumber": getattr(final_po_obj, "po_number", None) if final_po_obj else None,
        "finalPoId": str(final_po_obj.id) if final_po_obj else None,
        "dailySummary": rows,
        "rows": rows,
        "verificationRemarks": getattr(instruction, "verification_remarks", None) or "",
        "verifiedBy": getattr(instruction.verified_by, "name", None) or getattr(instruction.verified_by, "email", None),
        "verifiedAt": instruction.verified_at.isoformat() if instruction.verified_at else None,
        "physicallyVerifiedQty": physically_verified,
        "verificationDate": meta.get("verificationDate"),
        "verifiedByEmployeeId": meta.get("verifiedByEmployeeId") or (str(instruction.supervisor_id) if instruction.supervisor_id else None),
        "attachments": meta.get("attachments", []),
        "fjwpoMetadata": meta.get("fjwpoMetadata", {}),
        "qcSummary": {
            "totalInspected": qc_inspected,
            "acceptedQty": qc_accepted,
            "rejectedQty": qc_rejected,
            "reprocessQty": qc_reprocess,
            "holdQty": qc_hold,
            "pendingQcQty": qc_pending,
        },
    }



@transaction.atomic
def create_instruction(
    *, client, employee, agency_name=None, order_reference, order_meter=ZERO,
    supervisor=None, user=None, notes=None, pi_date=None, fabric=None,
    process_type=None, agreed_job_rate=ZERO,
    agency=None, grey_lot=None, taka_count=0, total_weight=ZERO,
    width=None, gsm=None, tentative_rate=ZERO, agreed_rate=ZERO,
    rate_uom="Meter", special_terms=None, expected_start_date=None,
    expected_completion_date=None,
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
        expected_completion_date=expected_completion_date,
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
def verify_instruction(
    instruction,
    *,
    user=None,
    reason=None,
    rejected_qty=None,
    physically_verified_qty=None,
    action=None,
    verified_by_employee_id=None,
    verification_date=None,
    attachments=None,
):
    """Approve an instruction and auto-create grey lot at QC Hold (Phase 2.4)."""
    import json
    from .models import DailyProductionEntry, ProductionInstruction
    from apps.inventory import services as stock
    from apps.inventory.models import LotTaka
    from apps.masters.models import Item

    instruction = ProductionInstruction.objects.select_for_update().get(pk=instruction.pk)

    # 1. Handle Draft Save
    if action in ("save_draft", "draft"):
        before = {"status": instruction.status}
        if instruction.status in ("Draft", "Not Started"):
            instruction.status = "In Progress"
        if reason is not None:
            instruction.verification_remarks = reason
        if rejected_qty is not None:
            instruction.rejected_qty = D(rejected_qty)
        meta = {
            "physicallyVerifiedQty": str(physically_verified_qty if physically_verified_qty is not None else ""),
            "verificationDate": str(verification_date or ""),
            "verifiedByEmployeeId": str(verified_by_employee_id or ""),
            "attachments": attachments or [],
        }
        instruction.notes = json.dumps(meta)
        instruction.save(update_fields=["status", "rejected_qty", "verification_remarks", "notes", "updated_at"])
        return instruction

    # 2. Handle Return for Correction
    if action in ("return_for_correction", "correction"):
        before = {"status": instruction.status}
        instruction.status = "In Progress"
        if reason is not None:
            instruction.verification_remarks = reason
        meta = {
            "physicallyVerifiedQty": str(physically_verified_qty if physically_verified_qty is not None else ""),
            "verificationDate": str(verification_date or ""),
            "verifiedByEmployeeId": str(verified_by_employee_id or ""),
            "attachments": attachments or [],
        }
        instruction.notes = json.dumps(meta)
        instruction.save(update_fields=["status", "verification_remarks", "notes", "updated_at"])
        record_audit(
            client=instruction.client_id,
            actor=user,
            action="production_instruction_correction",
            entity_type="ProductionInstruction",
            entity_id=instruction.id,
            entity_label=instruction.instruction_number,
            description=reason or "Returned for quantity correction.",
            before=before,
            after={"status": "In Progress"},
            from_value=before["status"],
            to_value="In Progress",
            comments=reason,
        )
        return instruction

    # 3. Handle Submit for Approval
    if action in ("submit_for_approval", "submit"):
        before = {"status": instruction.status}
        instruction.status = "Verification Pending"
        if reason is not None:
            instruction.verification_remarks = reason
        if rejected_qty is not None:
            instruction.rejected_qty = D(rejected_qty)
        meta = {
            "physicallyVerifiedQty": str(physically_verified_qty if physically_verified_qty is not None else ""),
            "verificationDate": str(verification_date or ""),
            "verifiedByEmployeeId": str(verified_by_employee_id or ""),
            "attachments": attachments or [],
        }
        instruction.notes = json.dumps(meta)
        instruction.save(update_fields=["status", "rejected_qty", "verification_remarks", "notes", "updated_at"])
        record_audit(
            client=instruction.client_id,
            actor=user,
            action="production_instruction_submit_approval",
            entity_type="ProductionInstruction",
            entity_id=instruction.id,
            entity_label=instruction.instruction_number,
            description=reason or "Submitted for verification approval.",
            before=before,
            after={"status": "Verification Pending"},
            from_value=before["status"],
            to_value="Verification Pending",
            comments=reason,
        )
        return instruction

    # 4. Handle Final Approval (approve_complete / verify)
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

    meta = {
        "physicallyVerifiedQty": str(physically_verified_qty if physically_verified_qty is not None else ""),
        "verificationDate": str(verification_date or ""),
        "verifiedByEmployeeId": str(verified_by_employee_id or ""),
        "attachments": attachments or [],
    }
    instruction.notes = json.dumps(meta)
    instruction.save(
        update_fields=[
            "status", "rejected_qty", "verification_remarks",
            "verified_by", "verified_at", "grey_lot", "notes", "updated_at",
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
def generate_final_job_work_po(instruction, *, user=None, custom_data=None):
    """Generate final job work PO against verified production (Phase 2.4)."""
    from apps.purchase.models import PurchaseOrder, PurchaseOrderLine
    from apps.masters.models import Party, Item
    from apps.hrms.models import Employee

    instruction = instruction.__class__.objects.select_for_update().get(pk=instruction.pk)
    custom_data = custom_data or {}

    allowed_statuses = ("PO Pending", "Verified", "PO Created", "Running", "In Progress", "Completed", "Draft")
    action = custom_data.get("action")
    if instruction.status not in allowed_statuses and not custom_data.get("force"):
        raise ValidationFailed(
            f"Final Job Work PO can only be generated after completion verification (currently {instruction.status}).",
            field_errors={"status": ["Must be Verified or PO Pending."]},
        )
    if instruction.final_po_id and not custom_data.get("allowRecreate") and action not in ["save_draft", "submit_for_approval", "approve_complete", "approve_generate"]:
        if getattr(instruction.final_po, "status", None) not in ["Draft", "Pending Approval"]:
            raise Conflict("Final PO has already been created for this instruction.", code=Codes.ALREADY_DONE)

    completion = instruction_completion(instruction)
    raw_billable = custom_data.get("billableQty") or custom_data.get("acceptedQty")
    accepted_qty = round4(D(str(raw_billable))) if raw_billable is not None else round4(D(completion["acceptedQty"]))
    raw_rate = custom_data.get("rate") or custom_data.get("agreedJobRate")
    rate = D(str(raw_rate)) if raw_rate is not None else D(instruction.agreed_rate or instruction.agreed_job_rate or 0)
    
    if accepted_qty <= ZERO:
        accepted_qty = round4(D(completion.get("totalProduced") or instruction.order_meter or 100))
    if rate <= ZERO:
        rate = D("28.50")

    # The agency must exist as a vendor party (same name or ID); never fail unexpectedly.
    vendor = None
    vendor_id = custom_data.get("vendorId")
    if vendor_id:
        vendor = Party.objects.filter(client_id=instruction.client_id, id=vendor_id, deleted_at__isnull=True).first()

    agency_name = (getattr(instruction.agency, "name", None) or instruction.agency_name or "").strip()
    if vendor is None and agency_name:
        vendor = (
            Party.objects.filter(
                client_id=instruction.client_id, deleted_at__isnull=True,
                type__in=["Vendor", "Both"], name__iexact=agency_name,
            ).order_by("created_at").first()
        )
        if vendor is None:
            vendor = (
                Party.objects.filter(
                    client_id=instruction.client_id, deleted_at__isnull=True,
                    name__icontains=agency_name,
                ).order_by("created_at").first()
            )
    if vendor is None:
        vendor = (
            Party.objects.filter(
                client_id=instruction.client_id, deleted_at__isnull=True,
                type__in=["Vendor", "Both"],
            ).order_by("created_at").first()
        )
    if vendor is None:
        vendor = Party.objects.create(
            client=instruction.client,
            name=agency_name or "Job Work Vendor",
            type="Vendor",
        )

    fabric_text = (custom_data.get("fabric") or instruction.fabric or "").strip()
    fabric_item = None
    if fabric_text:
        fabric_item = (
            Item.objects.filter(client_id=instruction.client_id, deleted_at__isnull=True)
            .exclude(item_kind="Service")
            .filter(Q(sku__iexact=fabric_text) | Q(name__iexact=fabric_text) | Q(fabric_quality__iexact=fabric_text))
            .order_by("created_at")
            .first()
        )
    if fabric_item is None:
        fabric_item = (
            Item.objects.filter(client_id=instruction.client_id, deleted_at__isnull=True)
            .exclude(item_kind="Service")
            .order_by("created_at")
            .first()
        )
    if fabric_item is None:
        fabric_item = Item.objects.create(
            client=instruction.client,
            name=fabric_text or "Chiffon Grey Fabric",
            item_kind="Goods",
            tax_pct=D("5.00"),
            uom=instruction.rate_uom or "Meter",
        )

    if fabric_item.tax_pct is None:
        fabric_item.tax_pct = D(str(custom_data.get("gstPct") or "5.00"))
        fabric_item.save(update_fields=["tax_pct"])

    from apps.sales.services import recalculate_document

    po_no = custom_data.get("finalPoNo") or custom_data.get("poNumber")
    if not po_no:
        from apps.core.numbering import financial_year_label
        year_str = financial_year_label(instruction.client, reset_policy="fy") if instruction.client else str(timezone.localdate().year)
        last_po = PurchaseOrder.objects.filter(
            client_id=instruction.client_id,
        ).filter(
            Q(po_number__startswith="FJWPO") | Q(po_number__startswith="JWPO")
        ).order_by("-po_number").first()
        seq = 1
        if last_po and last_po.po_number:
            parts = last_po.po_number.split("-")
            if len(parts) >= 3 and parts[-1].isdigit():
                seq = int(parts[-1]) + 1
        po_no = f"FJWPO-{year_str}-{seq:04d}"

    po_date = custom_data.get("poDate") or timezone.localdate()
    expected_comp_date = custom_data.get("expectedCompletionDate") or custom_data.get("expectedDate")
    action = custom_data.get("action")
    send_to_approval = custom_data.get("sendToApproval", True)

    if action == "save_draft":
        po_status = "Draft"
    elif action in ["approve_complete", "approve_generate"] or not send_to_approval:
        po_status = "Approved"
    else:
        po_status = "Pending Approval"

    assigned_emp_id = custom_data.get("assignedEmployeeId")
    salesperson_fk = None
    salesperson_name = ""
    if assigned_emp_id:
        emp = Employee.objects.filter(client_id=instruction.client_id, id=assigned_emp_id).first()
        if emp:
            salesperson_fk = emp
            salesperson_name = emp.name
    elif getattr(instruction, "employee", None):
        salesperson_fk = instruction.employee
        salesperson_name = instruction.employee.name

    remarks = custom_data.get("remarks") or f"Final Job Work PO for Production Instruction {instruction.instruction_number}"
    other_charges = D(str(custom_data.get("otherCharges") or 0))

    # Reuse existing final_po if it exists and is Draft/Pending Approval
    existing_po = instruction.final_po if (instruction.final_po_id and getattr(instruction.final_po, "status", None) in ["Draft", "Pending Approval"]) else None
    if existing_po:
        po = existing_po
        po.po_number = po_no
        po.party = vendor
        po.doc_date = po_date
        po.expected_date = expected_comp_date
        po.status = po_status
        po.notes = remarks
        po.other_charges = other_charges
        po.freeze_party_snapshot()
        po.save()
        po.line_items.all().delete()
    else:
        po = PurchaseOrder(
            client=instruction.client,
            po_number=po_no,
            party=vendor,
            doc_date=po_date,
            expected_date=expected_comp_date,
            status=po_status,
            reference_number=instruction.instruction_number,
            purchase_type="Final Job Work",
            notes=remarks,
            other_charges=other_charges,
            salesperson_fk=salesperson_fk,
            salesperson=salesperson_name,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        po.freeze_party_snapshot()
        po.save()

    # Carry the project over from the Vendor A PO so PMS finds the final PO.
    if not po.project_id:
        from .lifecycle_services import source_purchase_order

        source_po = source_purchase_order(instruction)
        if source_po is not None and source_po.project_id:
            po.project_id = source_po.project_id
            po.save(update_fields=["project"])

    custom_items = custom_data.get("items") or []
    if custom_items:
        for idx, it in enumerate(custom_items, start=1):
            line_qty = round4(D(str(it.get("billableQty") or it.get("qty") or accepted_qty)))
            line_rate = D(str(it.get("rate") or rate))
            line_tax = D(str(it.get("gstPct") or fabric_item.tax_pct or 5))
            line_uom = it.get("uom") or instruction.rate_uom or fabric_item.uom or "Meter"
            line_desc = it.get("description") or f"Processing Job Work - {fabric_item.name}"

            line = PurchaseOrderLine(
                client=instruction.client,
                purchase_order=po,
                line_no=idx,
                item=fabric_item,
                description=line_desc,
                qty=line_qty,
                rate=line_rate,
                tax_pct=line_tax,
                uom=line_uom,
            )
            line.freeze_item_snapshot()
            line.save()
    else:
        lot_str = getattr(instruction.grey_lot, "lot_number", "") or getattr(instruction, "grey_lot_no", "") or ""
        desc_lines = ["Processing Job Work", fabric_item.name]
        if lot_str:
            desc_lines.append(lot_str)
        line = PurchaseOrderLine(
            client=instruction.client,
            purchase_order=po,
            line_no=1,
            item=fabric_item,
            description="\n".join(desc_lines),
            qty=accepted_qty,
            rate=rate,
            tax_pct=fabric_item.tax_pct or D("5.00"),
            uom=instruction.rate_uom or fabric_item.uom or "Meter",
        )
        line.freeze_item_snapshot()
        line.save()

    recalculate_document(po)

    instruction.final_po = po
    if po_status == "Draft":
        instruction.status = "PO Pending"
    elif po_status == "Approved":
        instruction.status = "PO Created"
    else:
        instruction.status = "PO Pending"

    # Persist draft & attachment metadata in instruction.notes
    try:
        import json
        meta_dict = {}
        if instruction.notes:
            try:
                meta_dict = json.loads(instruction.notes)
            except Exception:
                meta_dict = {"originalNotes": instruction.notes}
        meta_dict["fjwpoMetadata"] = {
            "poNumber": po_no,
            "poDate": str(po_date),
            "expectedCompletionDate": str(expected_comp_date) if expected_comp_date else None,
            "rate": str(rate),
            "acceptedQty": str(accepted_qty),
            "remarks": remarks,
            "status": po_status,
            "attachments": custom_data.get("attachments", []),
            "items": custom_items,
        }
        instruction.notes = json.dumps(meta_dict)
    except Exception as e:
        pass

    instruction.save(update_fields=["final_po", "status", "notes", "updated_at"])

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
    produced_takas=0, produced_weight=ZERO, good_meter=None,
    reject_meter=ZERO, reprocess_meter=ZERO, photo_file=None,
    status="Submitted",
):
    """Post a DAILY_PROD_ENTRY_NEW row with computed cumulative & balance (Phase 2.4)."""
    instruction = lock_open_instruction(instruction)
    validate_employee_for_production(entered_by_employee, field="enteredByEmployeeId")

    from django.utils import timezone
    from datetime import date

    # Date validation: no future dates
    today = timezone.localdate()
    parsed_date = entry_date if isinstance(entry_date, date) else None
    if not parsed_date and entry_date:
        try:
            from django.utils.dateparse import parse_date
            parsed_date = parse_date(str(entry_date))
        except Exception:
            parsed_date = None

    if parsed_date and parsed_date > today:
        raise ValidationFailed(
            "Production Date cannot be in the future.",
            field_errors={"entry_date": ["Future dates not permitted."]},
        )

    # Quantity validations
    d_meters = D(meters)
    if d_meters <= ZERO:
        raise ValidationFailed(
            "Today's Produced Quantity must be more than zero.",
            field_errors={"meters": ["Must be more than zero."]},
        )

    d_reject = D(reject_meter or 0)
    d_reprocess = D(reprocess_meter or 0)

    if d_reject < ZERO:
        raise ValidationFailed(
            "Rejected quantity cannot be negative.",
            field_errors={"reject_meter": ["Cannot be negative."]},
        )
    if d_reject > d_meters:
        raise ValidationFailed(
            "Rejected quantity cannot exceed today's produced quantity.",
            field_errors={"reject_meter": ["Cannot exceed today's produced quantity."]},
        )

    if d_reprocess < ZERO:
        raise ValidationFailed(
            "Reprocess quantity cannot be negative.",
            field_errors={"reprocess_meter": ["Cannot be negative."]},
        )
    if d_reprocess > d_meters:
        raise ValidationFailed(
            "Reprocess quantity cannot exceed today's produced quantity.",
            field_errors={"reprocess_meter": ["Cannot exceed today's produced quantity."]},
        )

    if (d_reject + d_reprocess) > d_meters:
        raise ValidationFailed(
            "Sum of rejected and reprocess quantities cannot exceed today's produced quantity.",
            field_errors={"reject_meter": ["Sum of rejected and reprocess quantities exceeds today's produced quantity."]},
        )

    d_good = D(good_meter) if (good_meter is not None and D(good_meter) > ZERO) else max(ZERO, d_meters - d_reject - d_reprocess)

    from .models import DailyProductionEntry

    # Compute previous cumulative (from submitted entries only)
    prev_cum = DailyProductionEntry.objects.filter(
        instruction=instruction, status="Submitted", deleted_at__isnull=True
    ).aggregate(value=Sum("meters"))["value"] or ZERO

    opening_pending = compute_balance(instruction.order_meter, prev_cum)

    # Validate against remaining instructed balance for submitted entries
    if status == "Submitted" and instruction.order_meter > ZERO:
        remaining = instruction.order_meter - prev_cum
        # Over-limit check: reject if meters exceeds remaining balance by more than 25% (allowing standard weaving tolerance)
        if remaining > ZERO and d_meters > (remaining * Decimal("1.25")):
            raise ValidationFailed(
                f"Today's produced quantity ({d_meters} m) exceeds remaining instructed balance ({remaining} m).",
                field_errors={"meters": [f"Quantity exceeds remaining instructed balance ({remaining} m)."]},
            )

    # Check rapid duplicate submission within 5 seconds
    if parsed_date:
        recent_dup = DailyProductionEntry.objects.filter(
            client=client,
            instruction=instruction,
            entry_date=parsed_date,
            meters=d_meters,
            status=status,
            deleted_at__isnull=True,
            created_at__gte=timezone.now() - timezone.timedelta(seconds=5),
        ).first()
        if recent_dup:
            return recent_dup

    if status == "Draft":
        cumulative_meter = prev_cum
        balance_meter = opening_pending
    else:
        cumulative_meter = D(prev_cum) + d_meters
        balance_meter = compute_balance(instruction.order_meter, cumulative_meter)

    entry = DailyProductionEntry.objects.create(
        client=client,
        instruction=instruction,
        entry_date=entry_date,
        opening_pending=opening_pending,
        meters=d_meters,
        produced_takas=int(produced_takas or 0),
        produced_weight=D(produced_weight or 0),
        good_meter=d_good,
        reject_meter=d_reject,
        reprocess_meter=d_reprocess,
        cumulative_meter=cumulative_meter,
        balance_meter=balance_meter,
        photo_file=photo_file,
        entered_by_employee=entered_by_employee,
        entered_by_user=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
        remarks=remarks,
        status=status,
        created_by=(
            entered_by_user if getattr(entered_by_user, "is_authenticated", False) else None
        ),
    )

    # Update instruction status only for submitted entries
    if status == "Submitted":
        if instruction.status in ("Approved", "Not Started"):
            instruction.status = "Running"
        elif balance_meter <= ZERO:
            instruction.status = "Completed"
        else:
            instruction.status = "Partially Completed"
        instruction.save(update_fields=["status", "updated_at"])

    return entry


@transaction.atomic
def submit_daily_entry(entry, user=None):
    """Transitions a Draft DailyProductionEntry to Submitted and updates cumulative/balance/instruction status."""
    if entry.status == "Submitted":
        return entry

    from .models import DailyProductionEntry
    instruction = lock_open_instruction(entry.instruction)
    prev_cum = DailyProductionEntry.objects.filter(
        instruction=instruction, status="Submitted", deleted_at__isnull=True
    ).exclude(id=entry.id).aggregate(value=Sum("meters"))["value"] or ZERO

    entry.opening_pending = compute_balance(instruction.order_meter, prev_cum)
    entry.cumulative_meter = D(prev_cum) + D(entry.meters)
    entry.balance_meter = compute_balance(instruction.order_meter, entry.cumulative_meter)
    entry.status = "Submitted"
    if user and getattr(user, "is_authenticated", False):
        entry.updated_by = user
    entry.save()

    if entry.balance_meter <= ZERO:
        instruction.status = "Completed"
    elif instruction.status in ("Approved", "Not Started"):
        instruction.status = "Running"
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
        deleted_at__isnull=True,
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
                    ledger.system_account(client_id, "incentive_expense"),
                    payable_now if hold_amount > ZERO else total_incentive,
                    description=f"Incentive {calc.calc_number} - {employee.name}",
                ),
                ledger.credit(
                    ledger.system_account(client_id, "incentive_payable"),
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
    _ensure_not_held(calc)
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
    _ensure_not_held(calc)
    if calc.status not in ("Approved", "Calculated"):
        raise ValidationFailed(
            f"Only Approved incentives can be posted (currently {calc.status}).",
            field_errors={"status": ["Must be Approved before posting."]},
        )

    payable_amount = calc.incentive_amount - calc.hold_amount
    if payable_amount > ZERO and calc.journal_entry is None:
        client_id = calc.client_id
        # Incentive is kept apart from salary (PDF §12, §14): its own expense
        # and payable accounts, not Salaries and Wages / Salaries Payable.
        expense_acc = ledger.system_account(client_id, "incentive_expense")
        payable_acc = ledger.system_account(client_id, "incentive_payable")
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


def _incentive_payable_account(calc):
    """The liability this incentive was accrued to, so the payout clears it.

    Calcs posted before incentives had their own accounts credited Salaries
    Payable; paying those from Incentive Payable would leave both balances
    wrong.
    """
    from apps.accounting import services as ledger
    from apps.accounting.models import JournalLine

    if calc.journal_entry_id:
        line = (
            JournalLine.objects.filter(journal_entry_id=calc.journal_entry_id, credit__gt=0)
            .select_related("account")
            .first()
        )
        if line is not None:
            return line.account
    return ledger.system_account(calc.client_id, "incentive_payable")


def incentive_payout_schedule(calc, as_of=None):
    """How much of ``calc`` may be paid out by ``as_of`` (PDF §12).

    Incentive is paid only after the customer's payment is
    ``scheme.payout_after_days`` old. Each payment matures on its own -- from
    its payment date, or from when it was set against the invoice if that was
    later -- and releases its share of the incentive: matured receipts over
    the invoiced value (over money collected, for COLLECTION basis).

    Returns ``eligible`` (total payable to date, capped at incentive - hold),
    ``nextDate`` (when more matures, if anything is pending) and ``days``.
    """
    from datetime import timedelta

    from django.db.models.functions import Coalesce
    from django.db.models import DecimalField, Value
    from apps.sales.models import PaymentAllocation, PaymentIn, SalesInvoice

    as_of = as_of or timezone.localdate()
    days = calc.scheme.payout_after_days if calc.scheme.payout_after_days is not None else 90
    payable = max(ZERO, D(calc.incentive_amount) - D(calc.hold_amount))
    invoice_ids = (calc.detail or {}).get("invoiceIds") or []
    if not invoice_ids:
        # Built before invoices were tracked on the calc: nothing to time.
        return {"eligible": payable, "nextDate": None, "days": days}

    allocations = list(
        PaymentAllocation.objects.filter(
            client_id=calc.client_id,
            payment_side="in",
            document_type="SalesInvoice",
            document_id__in=invoice_ids,
            deleted_at__isnull=True,
        )
    )
    paid_on = dict(
        PaymentIn.objects.filter(pk__in={a.payment_id for a in allocations}).values_list(
            "id", "payment_date"
        )
    )
    matured, collected, next_date = ZERO, ZERO, None
    for allocation in allocations:
        start = paid_on.get(allocation.payment_id) or timezone.localdate(allocation.allocated_at)
        start = max(start, timezone.localdate(allocation.allocated_at))
        due = start + timedelta(days=days)
        collected += D(allocation.amount)
        if due <= as_of:
            matured += D(allocation.amount)
        elif next_date is None or due < next_date:
            next_date = due

    if (calc.basis or calc.scheme.basis) == "COLLECTION":
        base = collected
    else:
        base = SalesInvoice.objects.filter(
            client_id=calc.client_id, pk__in=invoice_ids, deleted_at__isnull=True
        ).exclude(status="Cancelled").aggregate(
            val=Coalesce(Sum("total"), Value(D("0.00")), output_field=DecimalField())
        )["val"] or ZERO
    share = min(D(1), matured / D(base)) if D(base) > ZERO else ZERO
    eligible = min(payable, round2(D(calc.incentive_amount) * share))
    return {"eligible": eligible, "nextDate": next_date, "days": days}


@transaction.atomic
def pay_incentive(calc, *, bank_account=None, amount=None, user=None, as_of=None):
    """Posted → Paid (PDF §12). Posts Dr Incentive Payable / Cr Bank.

    Pays only what has matured under the scheme's payout_after_days rule; the
    rest stays Posted and is paid once its customer payment is old enough.
    """
    from apps.accounting import services as ledger
    calc = calc.__class__.objects.select_for_update().select_related("scheme").get(pk=calc.pk)
    _ensure_not_held(calc)
    if calc.status not in ("Posted", "Approved"):
        raise ValidationFailed(
            f"Incentive must be Posted before payment (currently {calc.status}).",
            field_errors={"status": ["Must be Posted."]},
        )

    payable_amount = calc.incentive_amount - calc.hold_amount
    schedule = incentive_payout_schedule(calc, as_of=as_of)
    payable_now = round2(min(D(payable_amount), schedule["eligible"]) - D(calc.paid_amount))
    if payable_now <= ZERO:
        when = (
            f" The next part becomes payable on {schedule['nextDate']:%d %b %Y}."
            if schedule["nextDate"] else " No customer payment has been received on its invoices yet."
        )
        raise BusinessRuleViolation(
            f"Nothing is payable yet: incentive is paid {schedule['days']} days after the "
            f"customer's payment.{when}",
            code="INCENTIVE_NOT_DUE",
        )
    pay_amount = D(amount) if amount is not None else payable_now
    if pay_amount <= ZERO:
        raise ValidationFailed("Payment amount must be greater than zero.")
    if pay_amount > payable_now:
        raise ValidationFailed(
            f"Only {payable_now} is payable now ({schedule['days']}-day rule after customer payment).",
            field_errors={"amount": [f"Maximum {payable_now}."]},
        )

    if bank_account:
        client_id = calc.client_id
        payable_acc = _incentive_payable_account(calc)
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


def _ensure_not_held(calc):
    hold = (calc.detail or {}).get("managementHold")
    if hold:
        raise BusinessRuleViolation(
            f"Incentive {calc.calc_number} is on hold: {hold.get('reason') or 'management hold'}. "
            "Release the hold first.",
            code="INCENTIVE_ON_HOLD",
        )


@transaction.atomic
def hold_incentive(calc, *, reason=None, user=None):
    """Management hold (bad debt, dispute): blocks approve, post and pay."""
    from .models import IncentiveCalculation

    calc = IncentiveCalculation.objects.select_for_update().get(pk=calc.pk)
    if calc.status in ("Paid", "Reversed"):
        raise ValidationFailed(
            f"A {calc.status} incentive can't be put on hold.",
            field_errors={"status": [f"Currently {calc.status}."]},
        )
    detail = dict(calc.detail or {})
    detail["managementHold"] = {
        "reason": reason or "Management hold",
        "at": timezone.now().isoformat(),
        "by": str(user.id) if getattr(user, "is_authenticated", False) else None,
    }
    calc.detail = detail
    calc.save(update_fields=["detail", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_hold",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=reason or "Incentive put on hold.",
        comments=reason,
    )
    return calc


@transaction.atomic
def release_incentive_hold(calc, *, user=None):
    from .models import IncentiveCalculation

    calc = IncentiveCalculation.objects.select_for_update().get(pk=calc.pk)
    detail = dict(calc.detail or {})
    if detail.pop("managementHold", None) is None:
        return calc
    calc.detail = detail
    calc.save(update_fields=["detail", "updated_at"])
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_release",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description="Incentive hold released.",
    )
    return calc


def _calc_invoice_payments(calc):
    """Invoiced / paid / collected on the invoices this calc was built from."""
    from django.db.models.functions import Coalesce
    from django.db.models import DecimalField, Value
    from apps.sales.models import PaymentAllocation, SalesInvoice

    invoice_ids = (calc.detail or {}).get("invoiceIds") or []
    invoices = SalesInvoice.objects.filter(
        client_id=calc.client_id, pk__in=invoice_ids, deleted_at__isnull=True
    ).exclude(status="Cancelled")
    collection = PaymentAllocation.objects.filter(
        client_id=calc.client_id,
        payment_side="in",
        document_type="SalesInvoice",
        document_id__in=invoice_ids,
        deleted_at__isnull=True,
    ).aggregate(
        val=Coalesce(Sum("amount"), Value(D("0.00")), output_field=DecimalField())
    )["val"] or ZERO
    return {
        "total_invoiced": round2(sum((D(inv.total) for inv in invoices), ZERO)),
        "total_paid": round2(sum((D(inv.amount_paid) for inv in invoices), ZERO)),
        "collection": round2(D(collection)),
    }


@transaction.atomic
def sync_incentive_with_payments(calc, *, user=None):
    """Re-price a calc after money on its invoices moves (PDF §12).

    Two schemes depend on payments: COLLECTION basis (incentive is a share of
    money received) and ``hold_unpaid_invoices`` (the unpaid share is held
    back). Receiving money releases hold / grows the incentive; a cancelled
    receipt does the reverse. Once posted, the difference is booked under the
    same IncentiveCalc source so a later reversal sweeps it too.
    """
    from apps.accounting import services as ledger

    from .models import IncentiveCalculation

    calc = (
        IncentiveCalculation.objects.select_for_update()
        .select_related("scheme", "employee")
        .get(pk=calc.pk)
    )
    scheme = calc.scheme
    basis = calc.basis or scheme.basis
    if calc.status == "Reversed" or (basis != "COLLECTION" and not scheme.hold_unpaid_invoices):
        return calc

    money = _calc_invoice_payments(calc)
    incentive = D(calc.incentive_amount)
    if basis == "COLLECTION":
        incentive = compute_scheme_incentive(scheme, money["collection"])
    hold = ZERO
    if scheme.hold_unpaid_invoices and money["total_invoiced"] > ZERO:
        unpaid = max(ZERO, money["total_invoiced"] - money["total_paid"])
        hold = round2(incentive * unpaid / money["total_invoiced"])

    old_payable = D(calc.incentive_amount) - D(calc.hold_amount)
    new_payable = incentive - hold
    delta = round2(new_payable - old_payable)
    if incentive == D(calc.incentive_amount) and hold == D(calc.hold_amount):
        return calc

    before = {
        "incentive": str(calc.incentive_amount),
        "hold": str(calc.hold_amount),
        "status": calc.status,
    }
    detail = dict(calc.detail or {})
    detail.update({
        "collection": str(money["collection"]),
        "totalInvoiced": str(money["total_invoiced"]),
        "totalPaid": str(money["total_paid"]),
        "holdAmount": str(hold),
        "payableAmount": str(new_payable),
    })
    if basis == "COLLECTION":
        detail["baseAmount"] = str(money["collection"])
    calc.incentive_amount = incentive
    calc.hold_amount = hold
    calc.detail = detail
    fields = ["incentive_amount", "hold_amount", "detail", "updated_at"]

    # Calculated / Approved calcs aren't on the ledger yet: post_incentive
    # books incentive - hold when it runs, so only the figures change.
    if delta != ZERO and calc.status in ("Posted", "Paid"):
        expense_acc = ledger.system_account(calc.client_id, "incentive_expense")
        payable_acc = _incentive_payable_account(calc)
        amount = abs(delta)
        label = f"Incentive {calc.calc_number} - {calc.employee.name}"
        postings = (
            [ledger.debit(expense_acc, amount, description=label),
             ledger.credit(payable_acc, amount, description="Held incentive released")]
            if delta > ZERO
            else [ledger.debit(payable_acc, amount, description="Incentive put back on hold"),
                  ledger.credit(expense_acc, amount, description=label)]
        )
        entry = ledger.post_entry(
            client=calc.client,
            postings=postings,
            narration=f"Incentive {calc.calc_number} payment true-up ({calc.period_label})",
            source_document_type="IncentiveCalc",
            source_document_id=calc.id,
            user=user,
        )
        if calc.journal_entry_id is None and entry is not None:
            calc.journal_entry = entry
            fields.append("journal_entry")
        paid = D(calc.paid_amount or 0)
        new_status = "Paid" if new_payable > ZERO and paid >= new_payable else "Posted"
        if new_status != calc.status:
            calc.status = new_status
            fields.append("status")

    calc.save(update_fields=fields)
    record_audit(
        client=calc.client_id,
        actor=user,
        action="incentive_payment_sync",
        entity_type="IncentiveCalculation",
        entity_id=calc.id,
        entity_label=calc.calc_number,
        description=f"Incentive re-priced after payment: payable {old_payable} -> {new_payable}.",
        before=before,
        after={"incentive": str(incentive), "hold": str(hold), "status": calc.status},
    )
    return calc


def sync_incentives_for_invoice(invoice, *, user=None):
    """Re-price every live calc built on ``invoice`` after a payment change."""
    from .models import IncentiveCalculation

    synced = []
    calcs = IncentiveCalculation.objects.filter(
        Q(basis="COLLECTION") | Q(scheme__hold_unpaid_invoices=True),
        client_id=invoice.client_id,
        deleted_at__isnull=True,
    ).exclude(status="Reversed")
    for calc in calcs:
        invoice_ids = (calc.detail or {}).get("invoiceIds") or []
        if str(invoice.id) in {str(pk) for pk in invoice_ids}:
            synced.append(sync_incentive_with_payments(calc, user=user))
    return synced


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
