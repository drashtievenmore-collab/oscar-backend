"""Production Lifecycle read model (Vendor A Production PO, 8 stages).

One derivation shared by the Purchase PO stepper and the PMS project view so
both always show the same state. Read only: it never writes, and every value
comes from the canonical records --

  1. Purchase Order          purchase.PurchaseOrder (Vendor A Production)
  2. Production Instruction  pms.ProductionInstruction (order_reference = po_number)
  3. Daily Production Entry  pms.DailyProductionEntry (submitted entries)
  4. Quality Control         inventory.QCInspection
  5. Completion Verification ProductionInstruction.verified_at
  6. Final Job Work PO       ProductionInstruction.final_po
  7. Agency Bill Match       purchase.VendorBill (original or final PO)
  8. Disbursement Payment    PurchaseBill payable + PaymentOut allocations

Stage state is ``completed`` only when the record proves it, ``active`` when
the stage has started or is the next one to act on, otherwise ``pending``.
"""
from django.db.models import Q

from apps.core.money import ZERO, D

from .models import DailyProductionEntry, ProductionInstruction

STAGE_KEYS = [
    ("purchase_order", "Purchase Order"),
    ("production_instruction", "Production Instruction"),
    ("daily_production_entry", "Daily Production Entry"),
    ("grey_fabric_qc", "Quality Control (QC)"),
    ("completion_verification", "Completion Verification"),
    ("final_job_work_po", "Final Job Work PO"),
    ("agency_bill_match", "Agency Bill Match"),
    ("disbursement_payment", "Disbursement Payment"),
]

PO_RELEASED_STATUSES = (
    "Approved", "Issued", "In Production", "Partially Received", "Fully Received", "Received",
)
PO_BLOCKED_STATUSES = ("Rejected", "Cancelled")
FINAL_PO_OPEN_STATUSES = ("Draft", "Pending Approval")
VERIFIED_STATUSES = ("Verified", "Closed")
QC_PASSED_STATUSES = ("Approved", "Partially Approved")


def _num(value):
    return float(D(value or 0))


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def source_purchase_order(instruction):
    """The Vendor A PO an instruction was raised from (linked by number)."""
    from apps.purchase.models import PurchaseOrder

    if not instruction or not instruction.order_reference:
        return None
    return (
        PurchaseOrder.objects.filter(
            client_id=instruction.client_id,
            po_number=instruction.order_reference,
            deleted_at__isnull=True,
        )
        .exclude(purchase_type="Final Job Work")
        .first()
    )


def latest_instruction_for_po(po):
    """Same pick as the PO production-instruction endpoint: the newest one."""
    return (
        ProductionInstruction.objects.filter(
            client_id=po.client_id, order_reference=po.po_number, deleted_at__isnull=True
        )
        .select_related("final_po", "verified_by", "agency", "employee")
        .order_by("-created_at")
        .first()
    )


def _po_qty(po):
    return sum((D(line.qty) for line in po.line_items.all()), ZERO)


def _vendor_bill(po, final_po):
    from apps.purchase.models import VendorBill

    po_ids = [po.id] + ([final_po.id] if final_po else [])
    bills = list(
        VendorBill.objects.filter(
            client_id=po.client_id, purchase_order_id__in=po_ids, deleted_at__isnull=True
        )
        .select_related("purchase_bill")
        .order_by("-created_at")
    )
    approved = next((b for b in bills if b.status == "Approved"), None)
    return approved or (bills[0] if bills else None)


def _payable(po, final_po, vendor_bill):
    """The PurchaseBill that carries the agency payable, if any."""
    from apps.purchase.models import PurchaseBill

    if vendor_bill is not None and vendor_bill.purchase_bill_id:
        bill = vendor_bill.purchase_bill
        if bill and bill.deleted_at is None and bill.status != "Cancelled":
            return bill
    for target in (final_po, po):
        if target is None:
            continue
        bill = (
            PurchaseBill.objects.filter(
                client_id=po.client_id, purchase_order=target, deleted_at__isnull=True
            )
            .exclude(status="Cancelled")
            .order_by("-created_at")
            .first()
        )
        if bill:
            return bill
    return None


def _payments(bill):
    from apps.purchase.models import PaymentOut
    from apps.sales.models import PaymentAllocation

    allocations = list(
        PaymentAllocation.objects.filter(
            client_id=bill.client_id,
            payment_side="out",
            document_type="PurchaseBill",
            document_id=bill.id,
        )
    )
    if not allocations:
        return []
    payments = {
        p.id: p
        for p in PaymentOut.objects.filter(
            client_id=bill.client_id,
            id__in=[a.payment_id for a in allocations],
            deleted_at__isnull=True,
        )
    }
    rows = []
    for alloc in allocations:
        payment = payments.get(alloc.payment_id)
        if payment is None:
            continue
        rows.append(
            {
                "id": str(payment.id),
                "paymentNumber": payment.payment_number,
                "date": _date(payment.payment_date),
                "amount": _num(alloc.amount),
                "mode": payment.mode,
                "status": payment.status,
            }
        )
    rows.sort(key=lambda r: r["date"] or "")
    return rows


def _stage(key, label, *, done, started, status=None, **fields):
    return {"key": key, "label": label, "done": bool(done), "started": bool(started), "status": status, **fields}


def production_lifecycle(po):
    """The 8-stage lifecycle for one Vendor A Production purchase order."""
    from apps.inventory.models import QCInspection

    from .production_services import instruction_completion

    instruction = latest_instruction_for_po(po)
    completion = instruction_completion(instruction) if instruction else None
    final_po = instruction.final_po if instruction and instruction.final_po_id else None
    if final_po is not None and final_po.deleted_at is not None:
        final_po = None

    # 1. Purchase Order
    stages = [
        _stage(
            "purchase_order", "Purchase Order",
            done=po.status in PO_RELEASED_STATUSES,
            started=True,
            status=po.status,
            blocked=po.status in PO_BLOCKED_STATUSES,
            recordId=str(po.id),
            docNumber=po.po_number,
            date=_date(po.doc_date),
            amount=_num(po.total),
            quantity=_num(_po_qty(po)),
            party=po.party_name,
        )
    ]

    # 2. Production Instruction
    stages.append(
        _stage(
            "production_instruction", "Production Instruction",
            done=bool(instruction and instruction.status != "Draft"),
            started=instruction is not None,
            status=instruction.status if instruction else None,
            recordId=str(instruction.id) if instruction else None,
            docNumber=instruction.instruction_number if instruction else None,
            date=_date(instruction.pi_date or instruction.created_at) if instruction else None,
            quantity=_num(instruction.order_meter) if instruction else None,
            rate=_num(completion["jobRate"]) if completion else None,
            party=instruction.agency_name if instruction else None,
            employee=getattr(instruction.employee, "name", None) if instruction else None,
        )
    )

    # 3. Daily Production Entry
    verified = bool(
        instruction
        and (
            instruction.verified_at is not None
            or instruction.status in VERIFIED_STATUSES
            or instruction.status in ("PO Pending", "PO Created")
            or instruction.final_po_id is not None
            or bool(final_po)
        )
    )
    entries = (
        list(DailyProductionEntry.objects.filter(instruction=instruction, deleted_at__isnull=True).values_list("status", flat=True))
        if instruction else []
    )
    produced = D(completion["totalProduced"]) if completion else ZERO
    balance = D(completion["balanceQty"]) if completion else ZERO
    production_done = bool(
        instruction
        and (
            (produced > ZERO and balance <= ZERO)
            or instruction.status == "Completed"
            or verified
            or bool(final_po)
        )
    )
    stages.append(
        _stage(
            "daily_production_entry", "Daily Production Entry",
            done=production_done,
            started=bool(entries),
            status=(
                "Completed" if production_done
                else ("In Progress" if entries else None)
            ),
            recordId=str(instruction.id) if instruction else None,
            quantity=_num(produced),
            orderQuantity=_num(completion["orderQty"]) if completion else None,
            balanceQuantity=_num(balance) if completion else None,
            acceptedQuantity=_num(completion["acceptedQty"]) if completion else None,
            entryCount=len(entries),
            draftEntryCount=sum(1 for s in entries if s == "Draft"),
        )
    )

    # 4. Quality Control
    qc_rows = []
    if instruction is not None:
        qc_rows = list(
            QCInspection.objects.filter(client_id=po.client_id, deleted_at__isnull=True)
            .filter(Q(production_instruction=instruction) | Q(purchase_order=po))
            .order_by("-created_at")
        )
    qc_final = [q for q in qc_rows if q.status != "Draft"]
    qc_inspected = sum((D(q.total_inspected_qty) for q in qc_final), ZERO)
    qc_passed = sum((D(q.passed_qty) for q in qc_final), ZERO)
    qc_rejected = sum((D(q.rejected_qty) for q in qc_final), ZERO)
    qc_done = bool(
        (
            produced > ZERO
            and any(q.status in QC_PASSED_STATUSES for q in qc_final)
            and qc_inspected >= produced
        )
        or any(q.status in ("Approved", "Passed") for q in qc_final)
        or verified
        or bool(final_po)
    )
    latest_qc = qc_rows[0] if qc_rows else None
    stages.append(
        _stage(
            "grey_fabric_qc", "Quality Control (QC)",
            done=qc_done,
            started=bool(qc_rows),
            status=latest_qc.status if latest_qc else None,
            recordId=str(latest_qc.id) if latest_qc else None,
            docNumber=latest_qc.inspection_number if latest_qc else None,
            date=_date(latest_qc.inspection_date) if latest_qc else None,
            quantity=_num(qc_inspected),
            passedQuantity=_num(qc_passed),
            rejectedQuantity=_num(qc_rejected),
            pendingQuantity=_num(max(ZERO, produced - qc_inspected)),
            inspectionCount=len(qc_rows),
        )
    )

    # 5. Completion Verification
    verification_status = None
    if verified:
        verification_status = "Verified"
    elif instruction is not None and instruction.status == "Verification Pending":
        verification_status = "Verification Pending"
    stages.append(
        _stage(
            "completion_verification", "Completion Verification",
            done=verified,
            started=verification_status is not None or bool(final_po),
            status=verification_status,
            recordId=str(instruction.id) if instruction else None,
            docNumber=instruction.instruction_number if instruction else None,
            date=_date(instruction.verified_at) if verified else None,
            quantity=_num(completion["physicallyVerifiedQty"]) if completion and verified else None,
            verifiedBy=(completion or {}).get("verifiedBy") if verified else None,
        )
    )

    # 6. Final Job Work PO
    stages.append(
        _stage(
            "final_job_work_po", "Final Job Work PO",
            done=bool(final_po and final_po.status not in FINAL_PO_OPEN_STATUSES + PO_BLOCKED_STATUSES),
            started=final_po is not None,
            status=final_po.status if final_po else None,
            blocked=bool(final_po and final_po.status in PO_BLOCKED_STATUSES),
            recordId=str(final_po.id) if final_po else None,
            docNumber=final_po.po_number if final_po else None,
            date=_date(final_po.doc_date) if final_po else None,
            amount=_num(final_po.total) if final_po else None,
            quantity=_num(_po_qty(final_po)) if final_po else None,
            party=final_po.party_name if final_po else None,
        )
    )

    # 7. Agency Bill Match
    vendor_bill = _vendor_bill(po, final_po)
    match_result = (vendor_bill.match_result or {}) if vendor_bill else {}
    stages.append(
        _stage(
            "agency_bill_match", "Agency Bill Match",
            done=bool(vendor_bill and vendor_bill.status == "Approved"),
            started=vendor_bill is not None,
            status=vendor_bill.status if vendor_bill else None,
            blocked=bool(vendor_bill and vendor_bill.status == "Rejected"),
            recordId=str(vendor_bill.id) if vendor_bill else None,
            docNumber=vendor_bill.vendor_bill_number if vendor_bill else None,
            date=_date(vendor_bill.bill_date) if vendor_bill else None,
            amount=_num(vendor_bill.total) if vendor_bill else None,
            matchStatus=vendor_bill.match_status if vendor_bill else None,
            matchResult=match_result.get("overallStatus") if isinstance(match_result, dict) else None,
        )
    )

    # 8. Disbursement Payment -- completed only when the payable is Paid.
    payable = _payable(po, final_po, vendor_bill)
    payments = _payments(payable) if payable else []
    paid = bool(payable and payable.status == "Paid")
    payment_status = None
    if payable is not None:
        payment_status = payable.status
    elif vendor_bill is not None and vendor_bill.status == "Approved":
        payment_status = "Awaiting Payable"
    stages.append(
        _stage(
            "disbursement_payment", "Disbursement Payment",
            done=paid,
            started=payable is not None,
            status=payment_status,
            recordId=str(payable.id) if payable else None,
            docNumber=payable.bill_number if payable else None,
            date=payments[-1]["date"] if payments else None,
            amount=_num(payable.total) if payable else None,
            approvalStatus=vendor_bill.status if vendor_bill else None,
            payableStatus=payable.status if payable else None,
            amountPaid=_num(payable.amount_paid) if payable else 0.0,
            balanceDue=_num(D(payable.total) - D(payable.amount_paid)) if payable else None,
            payments=payments,
        )
    )

    # Stage state: completed / active / pending. The first open stage is the
    # active one; any later stage that already has records is active too.
    # When a downstream milestone is completed (such as final_job_work_po),
    # all preceding manufacturing & verification stages are completed as well.
    last_done_idx = max((i for i, s in enumerate(stages) if s["done"]), default=-1)
    first_open = next((i for i, s in enumerate(stages) if not s["done"] and (last_done_idx < 0 or i > last_done_idx)), None)
    for idx, stage in enumerate(stages):
        if stage["done"] or (last_done_idx >= 0 and idx < last_done_idx):
            stage["state"] = "completed"
            if not stage["status"] and idx < last_done_idx:
                stage["status"] = "Approved" if "qc" in stage["key"] else ("Verified" if "verification" in stage["key"] else "Completed")
        elif stage["started"] or idx == first_open:
            stage["state"] = "active"
        else:
            stage["state"] = "pending"
        stage.pop("done")
        stage.pop("started")
        stage.setdefault("blocked", False)
        stage["sequence"] = idx + 1

    completed = sum(1 for s in stages if s["state"] == "completed")
    return {
        "purchaseOrderId": str(po.id),
        "poNumber": po.po_number,
        "projectId": str(po.project_id) if po.project_id else None,
        "instructionId": str(instruction.id) if instruction else None,
        "finalPoId": str(final_po.id) if final_po else None,
        "currentStage": stages[first_open]["key"] if first_open is not None else None,
        "completedCount": completed,
        "isComplete": completed == len(stages),
        "stages": stages,
    }


def project_production_purchase_orders(project):
    from apps.purchase.models import PURCHASE_TYPE_VENDOR_A, PurchaseOrder

    return (
        PurchaseOrder.objects.filter(
            client_id=project.client_id,
            deleted_at__isnull=True,
            purchase_type=PURCHASE_TYPE_VENDOR_A,
        )
        .filter(Q(project=project) | Q(reference_number=project.code))
        .prefetch_related("line_items")
        .order_by("-created_at")
    )


def project_production_lifecycles(project):
    """Every Vendor A Production PO of a project, each with its 8 stages."""
    return [production_lifecycle(po) for po in project_production_purchase_orders(project)]
