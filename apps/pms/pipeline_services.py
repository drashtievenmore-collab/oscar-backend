"""
Project Stage Pipeline Services (Conditional Grey Stock Workflow).

Enforces the 11-stage end-to-end textile lifecycle defined in:
Oscar ERP - Grey Fabric Trading & Job Work Management Scope.

Route Branches:
- Branch A: Required grey fabric is available in Oscar inventory.
  Direct Job Work Outward to Vendor B (Dyeing/Processing).
  Stages 3, 4, 5 (Vendor A Purchase & Weaving) are marked "Not Required".
- Branch B: Grey fabric is unavailable or in shortage.
  Stages 3, 4, 5 activate for the shortage quantity (PO -> Daily Production -> Transfer to Oscar).
- Partial: In-stock quantity allocated; shortage quantity routed through Branch B.
  Both converge cleanly at Stage 6 (Vendor B Processing).
"""
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone

from apps.core.money import round2, round4, D
from apps.pms.models import Project, ProjectStage, Department, CLOSED_STAGE_STATUSES


STAGE_DEFINITIONS = [
    {
        "sequence": 1,
        "name": "Customer Requirement & Approval",
        "department_name": "Sales",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "ShoppingCart",
    },
    {
        "sequence": 2,
        "name": "Grey Stock Check & Allocation",
        "department_name": "Inventory",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "Boxes",
    },
    {
        "sequence": 3,
        "name": "Procurement (If Required)",
        "department_name": "Purchase",
        "branch": "branch_b",
        "default_weight": Decimal("10.00"),
        "icon": "ShoppingCart",
    },
    {
        "sequence": 4,
        "name": "Processing Plan & Vendor Assignment",
        "department_name": "Job Work",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "ClipboardList",
    },
    {
        "sequence": 5,
        "name": "Job Work Outward (Send to Vendor)",
        "department_name": "Job Work",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "Truck",
    },
    {
        "sequence": 6,
        "name": "Processing (Dyeing)",
        "department_name": "Job Work",
        "branch": "common",
        "default_weight": Decimal("15.00"),
        "icon": "Cog",
    },
    {
        "sequence": 7,
        "name": "Job Work Inward (Receive)",
        "department_name": "Job Work",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "ArrowDownToLine",
    },
    {
        "sequence": 8,
        "name": "Quality Check & Stock Allocation",
        "department_name": "Quality Control",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "ShieldCheck",
    },
    {
        "sequence": 9,
        "name": "Customer Dispatch",
        "department_name": "Dispatch",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "Boxes",
    },
    {
        "sequence": 10,
        "name": "Invoice & Payment Collection",
        "department_name": "Accounts",
        "branch": "common",
        "default_weight": Decimal("10.00"),
        "icon": "IndianRupee",
    },
    {
        "sequence": 11,
        "name": "Project Closure",
        "department_name": "Management",
        "branch": "common",
        "default_weight": Decimal("5.00"),
        "icon": "CheckCircle2",
    },
]


def evaluate_stock_and_route(project):
    """
    Evaluates available grey fabric stock against the project's requirement.
    Returns:
        dict: {
            "required_meters": Decimal,
            "available_meters": Decimal,
            "allocated_meters": Decimal,
            "shortage_meters": Decimal,
            "route_branch": "BRANCH_A" | "BRANCH_B" | "PARTIAL",
            "route_label": str,
        }
    """
    client_id = project.client_id
    req_qty = Decimal(str(project.requirement_meters or project.quantity or 0))

    order = project.sales_order
    if req_qty == Decimal("0") and order:
        line_sum = order.lines.aggregate(s=Sum("ordered_quantity"))["s"] or 0
        req_qty = Decimal(str(line_sum))

    if req_qty == Decimal("0"):
        req_qty = Decimal("0.00")

    # Check actual eligible stock in Oscar Inventory
    from apps.inventory.services import calculate_combined_grey_stock, calculate_item_stock

    item = None
    if order:
        first_line = order.lines.first()
        if first_line and first_line.item:
            item = first_line.item

    available_stock = Decimal("0")
    if item:
        stock_calc = calculate_item_stock(client_id, item.id)
        available_stock = max(Decimal("0"), Decimal(str(stock_calc.get("available", 0))))
    else:
        try:
            combined = calculate_combined_grey_stock(client_id)
            available_stock = max(Decimal("0"), Decimal(str(combined.get("available_grey_stock", 0))))
        except Exception:
            available_stock = Decimal("0")

    if req_qty == Decimal("0"):
        branch = "BRANCH_A"
        label = "Branch A (Direct Route)"
        allocated = Decimal("0")
        shortage = Decimal("0")
    elif available_stock >= req_qty:
        branch = "BRANCH_A"
        label = "Branch A (100% Grey Stock Available in Warehouse)"
        allocated = req_qty
        shortage = Decimal("0")
    elif available_stock > Decimal("0"):
        branch = "PARTIAL"
        label = f"Partial Availability ({available_stock} M Available, {req_qty - available_stock} M Shortage)"
        allocated = available_stock
        shortage = req_qty - available_stock
    else:
        branch = "BRANCH_B"
        label = "Branch B (Stock Unavailable — Vendor A Weaving Required)"
        allocated = Decimal("0")
        shortage = req_qty

    return {
        "required_meters": round2(req_qty),
        "available_meters": round2(available_stock),
        "allocated_meters": round2(allocated),
        "shortage_meters": round2(shortage),
        "route_branch": branch,
        "route_label": label,
    }


@transaction.atomic
def sync_project_pipeline_stages(project):
    """
    Ensures the project has all 11 stages populated and synced with real
    business documents and conditional grey stock branch status.
    """
    stock_eval = evaluate_stock_and_route(project)
    branch = stock_eval["route_branch"]
    req_qty = stock_eval["required_meters"]
    allocated_qty = stock_eval["allocated_meters"]
    shortage_qty = stock_eval["shortage_meters"]

    client_id = project.client_id
    order = project.sales_order

    # Remove any obsolete/extra stages (e.g., dyeing, vbvb, or non-standard sequences)
    ProjectStage.objects.filter(project=project).filter(
        Q(name__iexact="dyeing") | Q(name__iexact="vbvb") | ~Q(sequence__in=[s["sequence"] for s in STAGE_DEFINITIONS])
    ).delete()

    # Query operational documents
    from apps.purchase.models import PurchaseOrder
    from apps.jobwork.models import JobWorkOrder, ProcessPlan
    from apps.inventory.models import QCInspection
    from apps.sales.models import DeliveryChallan, SalesInvoice

    # Purchase PO
    po = PurchaseOrder.objects.filter(client_id=client_id).filter(
        Q(reference_number=project.code) | Q(reference_number=getattr(order, "order_number", ""))
    ).first()

    # Process Plan
    plan = ProcessPlan.objects.filter(client_id=client_id).filter(
        Q(remarks__icontains=project.code) | Q(plan_no__icontains=project.code)
    ).first()

    # Job Work Order
    jwo = JobWorkOrder.objects.filter(client_id=client_id).filter(
        Q(remarks__icontains=project.code) | Q(jwo_no__icontains=project.code)
    ).first()

    jwo_sent = Decimal(str(project.dyeing_sent_meters or 0))
    jwo_received = Decimal(str(project.customer_project_received_meters or project.own_project_received_meters or 0))
    outward_challan = None

    if jwo:
        outwards = getattr(jwo, "outwards", None)
        if outwards and outwards.exists():
            outward_challan = outwards.first()
            jwo_sent = sum((Decimal(str(o.meter or 0)) for o in outwards.all()), Decimal("0"))
        inwards = getattr(jwo, "inwards", None)
        if inwards and inwards.exists():
            jwo_received = sum((Decimal(str(i.meter or 0)) for i in inwards.all()), Decimal("0"))

    # QC Inspection
    qc = QCInspection.objects.filter(client_id=client_id).first()

    # Customer Dispatch Challan
    dispatched_qty = Decimal("0")
    dc_doc = None
    if order:
        challans = DeliveryChallan.objects.filter(sales_order=order, status__in=["Dispatched", "Delivered"])
        for ch in challans:
            line_sum = ch.lines.aggregate(s=Sum("dispatched_quantity"))["s"] or 0
            dispatched_qty += Decimal(str(line_sum))
        dc_doc = challans.first()

    # Sales Invoices
    invoiced_amount = Decimal("0")
    paid_amount = Decimal("0")
    inv_doc = None
    if order:
        invoices = SalesInvoice.objects.filter(sales_order=order)
        for inv in invoices:
            invoiced_amount += Decimal(str(inv.total_amount or 0))
            paid_amount += Decimal(str(inv.amount_paid or 0))
        inv_doc = invoices.first()

    departments_by_name = {d.name: d for d in Department.objects.filter(client_id=client_id)}

    # Build stage state list
    stages_data = []

    for defn in STAGE_DEFINITIONS:
        seq = defn["sequence"]
        name = defn["name"]
        dept = departments_by_name.get(defn["department_name"])
        stage_branch = defn["branch"]
        weight = defn["default_weight"]

        # Default values
        status = "Pending"
        pct = 0
        quantities = {
            "requirement": float(req_qty),
            "input": float(req_qty),
            "produced": None,
            "good": None,
            "accepted": None,
            "reserved": None,
            "dispatched": None,
        }
        document = None

        # -------------------------------------------------------------
        # Stage 1: Customer Requirement & Approval
        # -------------------------------------------------------------
        if seq == 1:
            if order and order.stage in ("Confirmed", "Approved", "In Production", "Closed", "Completed"):
                status = "Completed"
                pct = 100
            elif order and order.stage != "Draft":
                status = "Completed"
                pct = 100
            elif order:
                status = "In Progress"
                pct = 50
            elif project.status in ("In Progress", "Completed"):
                status = "Completed"
                pct = 100
            quantities["accepted"] = float(req_qty)
            if order:
                document = {
                    "type": "SalesOrder",
                    "number": order.order_number or str(order.id),
                    "url": f"/sales/orders?id={order.id}",
                    "label": f"SO: {order.order_number}",
                }

        # -------------------------------------------------------------
        # Stage 2: Grey Stock Check & Allocation
        # -------------------------------------------------------------
        elif seq == 2:
            quantities["reserved"] = float(allocated_qty)
            if allocated_qty > Decimal("0") or stock_eval["available_meters"] > Decimal("0"):
                status = "Completed"
                pct = 100
                document = {
                    "type": "StockPosition",
                    "number": f"{stock_eval['available_meters']} M Avail",
                    "url": "/inventory/stock-position",
                    "label": f"Allocated: {allocated_qty} M ({branch})",
                }
            elif req_qty > Decimal("0"):
                status = "In Progress"
                pct = 0
            else:
                status = "Pending"
                pct = 0

        # -------------------------------------------------------------
        # Stage 3: Procurement (If Required)
        # -------------------------------------------------------------
        elif seq == 3:
            if branch == "BRANCH_A":
                status = "Not Required"
                pct = 100
                weight = Decimal("0.00")
            else:
                quantities["input"] = float(shortage_qty)
                if po and po.status in ("Received", "Fully Received", "Closed"):
                    status = "Completed"
                    pct = 100
                    quantities["good"] = float(shortage_qty)
                elif po:
                    status = "In Progress"
                    pct = 50
                if po:
                    document = {
                        "type": "PurchaseOrder",
                        "number": po.po_number or str(po.id),
                        "url": f"/purchase/orders?id={po.id}",
                        "label": f"PO: {po.po_number}",
                    }

        # -------------------------------------------------------------
        # Stage 4: Processing Plan & Vendor Assignment
        # -------------------------------------------------------------
        elif seq == 4:
            quantities["input"] = float(req_qty)
            if plan or jwo:
                status = "Completed"
                pct = 100
                doc_num = plan.plan_no if plan else jwo.jwo_no
                doc_url = f"/job-work/plans?id={plan.id}" if plan else f"/job-work/orders?id={jwo.id}"
                document = {
                    "type": "ProcessPlan" if plan else "JobWorkOrder",
                    "number": doc_num or "Plan",
                    "url": doc_url,
                    "label": f"Plan: {doc_num}",
                }
            elif jwo_sent > Decimal("0"):
                status = "Completed"
                pct = 100
            else:
                status = "Pending"
                pct = 0

        # -------------------------------------------------------------
        # Stage 5: Job Work Outward (Send to Vendor)
        # -------------------------------------------------------------
        elif seq == 5:
            quantities["input"] = float(req_qty)
            quantities["produced"] = float(jwo_sent)
            if jwo_sent >= req_qty and req_qty > Decimal("0"):
                status = "Completed"
                pct = 100
            elif jwo_sent > Decimal("0"):
                status = "In Progress"
                pct = min(99, int(round((float(jwo_sent) / float(req_qty)) * 100))) if req_qty > 0 else 50
            else:
                status = "Pending"
                pct = 0
            if outward_challan:
                document = {
                    "type": "JWOOutward",
                    "number": getattr(outward_challan, "challan_no", None) or str(outward_challan.id),
                    "url": "/job-work/outward",
                    "label": f"Challan: {getattr(outward_challan, 'challan_no', outward_challan.id)}",
                }
            elif jwo:
                document = {
                    "type": "JobWorkOrder",
                    "number": jwo.jwo_no or str(jwo.id),
                    "url": f"/job-work/orders?id={jwo.id}",
                    "label": f"JWO: {jwo.jwo_no}",
                }

        # -------------------------------------------------------------
        # Stage 6: Processing (Dyeing)
        # -------------------------------------------------------------
        elif seq == 6:
            quantities["input"] = float(jwo_sent)
            quantities["produced"] = float(jwo_received)
            if jwo_received >= jwo_sent and jwo_sent > Decimal("0"):
                status = "Completed"
                pct = 100
            elif jwo_received > Decimal("0"):
                status = "In Progress"
                pct = min(99, int(round((float(jwo_received) / float(jwo_sent)) * 100))) if jwo_sent > 0 else 50
            elif jwo_sent > Decimal("0"):
                status = "In Progress"
                pct = 10
            else:
                status = "Pending"
                pct = 0

        # -------------------------------------------------------------
        # Stage 7: Job Work Inward (Receive)
        # -------------------------------------------------------------
        elif seq == 7:
            quantities["input"] = float(jwo_sent or req_qty)
            quantities["good"] = float(jwo_received)
            if jwo_received >= req_qty and req_qty > Decimal("0"):
                status = "Completed"
                pct = 100
            elif jwo_received > Decimal("0"):
                status = "In Progress"
                pct = min(99, int(round((float(jwo_received) / float(req_qty)) * 100))) if req_qty > 0 else 50
            else:
                status = "Pending"
                pct = 0

        # -------------------------------------------------------------
        # Stage 8: Quality Check & Stock Allocation
        # -------------------------------------------------------------
        elif seq == 8:
            if qc and qc.status in ("Approved", "Completed", "Passed"):
                status = "Completed"
                pct = 100
                quantities["accepted"] = float(qc.passed_qty or jwo_received or req_qty)
            elif qc:
                status = "In Progress"
                pct = 50
                quantities["accepted"] = float(qc.passed_qty or 0)
            elif jwo_received > Decimal("0"):
                status = "In Progress"
                pct = 20
            else:
                status = "Pending"
                pct = 0
            if qc:
                document = {
                    "type": "QCInspection",
                    "number": qc.inspection_number,
                    "url": f"/inventory/qc/register?id={qc.id}",
                    "label": f"QC: {qc.inspection_number}",
                }

        # -------------------------------------------------------------
        # Stage 9: Customer Dispatch
        # -------------------------------------------------------------
        elif seq == 9:
            quantities["dispatched"] = float(dispatched_qty)
            if dispatched_qty >= req_qty and req_qty > Decimal("0"):
                status = "Completed"
                pct = 100
            elif dispatched_qty > Decimal("0"):
                status = "In Progress"
                pct = min(99, int(round((float(dispatched_qty) / float(req_qty)) * 100)))
            else:
                status = "Pending"
                pct = 0
            if dc_doc:
                document = {
                    "type": "DeliveryChallan",
                    "number": dc_doc.challan_number,
                    "url": f"/sales/delivery-challans?id={dc_doc.id}",
                    "label": f"DC: {dc_doc.challan_number}",
                }

        # -------------------------------------------------------------
        # Stage 10: Invoice & Payment Collection
        # -------------------------------------------------------------
        elif seq == 10:
            quantities["invoicedAmount"] = float(invoiced_amount)
            quantities["paidAmount"] = float(paid_amount)
            if paid_amount >= invoiced_amount and invoiced_amount > Decimal("0"):
                status = "Completed"
                pct = 100
            elif invoiced_amount > Decimal("0"):
                status = "In Progress"
                pct = min(99, int(round((float(paid_amount) / float(invoiced_amount)) * 100)))
            else:
                status = "Pending"
                pct = 0
            if inv_doc:
                document = {
                    "type": "SalesInvoice",
                    "number": inv_doc.invoice_number,
                    "url": f"/sales/invoices?id={inv_doc.id}",
                    "label": f"Inv: {inv_doc.invoice_number}",
                }

        # -------------------------------------------------------------
        # Stage 11: Project Closure
        # -------------------------------------------------------------
        elif seq == 11:
            if project.status == "Completed":
                status = "Completed"
                pct = 100
            elif dispatched_qty >= req_qty and req_qty > Decimal("0") and paid_amount >= invoiced_amount and invoiced_amount > Decimal("0"):
                status = "Completed"
                pct = 100
            elif dispatched_qty > Decimal("0"):
                status = "In Progress"
                pct = 50
            else:
                status = "Pending"
                pct = 0

        # Operational remarks
        remarks = ""
        if seq == 1:
            if order and order.stage in ("Confirmed", "Approved", "In Production", "Closed", "Completed"):
                remarks = "Quotation approved"
            elif order and order.stage != "Draft":
                remarks = "Order confirmed"
            elif order:
                remarks = "Quotation under review"
            else:
                remarks = "Requirement submitted"
        elif seq == 2:
            if allocated_qty > Decimal("0"):
                remarks = f"{int(allocated_qty):,} m allocated"
            else:
                remarks = "Stock check pending"
        elif seq == 3:
            if branch == "BRANCH_A":
                remarks = "Stock available"
            elif po and po.status in ("Received", "Fully Received", "Closed"):
                remarks = f"Received PO {po.po_number}"
            elif po:
                remarks = f"PO {po.po_number} in progress"
            else:
                remarks = "Procurement required"
        elif seq == 4:
            if jwo and getattr(jwo, "vendor_name", None):
                remarks = f"{jwo.vendor_name} assigned"
            elif plan and getattr(plan, "vendor_name", None):
                remarks = f"{plan.vendor_name} assigned"
            elif plan or jwo:
                remarks = "Vendor B assigned"
            else:
                remarks = "Processing plan pending"
        elif seq == 5:
            if outward_challan and getattr(outward_challan, "challan_no", None):
                remarks = f"Challan {outward_challan.challan_no}"
            elif jwo and getattr(jwo, "jwo_no", None):
                remarks = f"JWO {jwo.jwo_no}"
            else:
                remarks = "Outward pending"
        elif seq == 6:
            vendor = (jwo and getattr(jwo, "vendor_name", None)) or "Vendor B"
            if jwo_received >= jwo_sent and jwo_sent > Decimal("0"):
                remarks = f"Dyeing completed at {vendor}"
            elif jwo_sent > Decimal("0"):
                remarks = f"Dyeing in process at {vendor}"
            else:
                remarks = "Processing pending"
        elif seq == 7:
            if jwo_received >= req_qty and req_qty > Decimal("0"):
                remarks = f"Received full {int(jwo_received):,} m"
            elif jwo_received > Decimal("0"):
                remarks = f"Received {int(jwo_received):,} m"
            else:
                remarks = "Inward pending"
        elif seq == 8:
            if qc and qc.status in ("Approved", "Completed", "Passed"):
                remarks = f"QC Passed {int(qc.passed_qty or jwo_received):,} m"
            elif qc:
                remarks = "QC inspection in progress"
            else:
                remarks = "QC pending"
        elif seq == 9:
            if dispatched_qty >= req_qty and req_qty > Decimal("0"):
                remarks = f"Dispatched {int(dispatched_qty):,} m"
            elif dispatched_qty > Decimal("0"):
                remarks = f"Dispatched {int(dispatched_qty):,} m"
            else:
                remarks = "Dispatch pending"
        elif seq == 10:
            if paid_amount >= invoiced_amount and invoiced_amount > Decimal("0"):
                remarks = "Payment received"
            elif invoiced_amount > Decimal("0"):
                remarks = f"Invoiced ₹{int(invoiced_amount):,}"
            else:
                remarks = "Invoice pending"
        elif seq == 11:
            if project.status == "Completed":
                remarks = "Project closed"
            else:
                remarks = "Closure pending"

        # Sync or Create ProjectStage in database
        stage_start = project.start_date if status != "Pending" else None
        stage_expected = project.expected_completion_date if status != "Pending" else None

        stage_obj, _ = ProjectStage.objects.get_or_create(
            project=project,
            sequence=seq,
            defaults={
                "client_id": client_id,
                "name": name,
                "department": dept,
                "weight_pct": weight,
                "status": status,
                "completion_pct": pct,
                "start_datetime": stage_start,
                "expected_completion_datetime": stage_expected,
            },
        )

        stage_obj.name = name
        stage_obj.weight_pct = weight
        stage_obj.status = status
        stage_obj.completion_pct = pct
        if dept:
            stage_obj.department = dept
        if status == "Pending" and not stage_obj.actual_start_datetime:
            stage_obj.start_datetime = None
            stage_obj.expected_completion_datetime = None
        elif not stage_obj.start_datetime and status != "Pending":
            stage_obj.start_datetime = stage_start

        stage_obj.save(update_fields=["name", "weight_pct", "status", "completion_pct", "department", "start_datetime", "expected_completion_datetime", "updated_at"])

        stages_data.append({
            "id": str(stage_obj.id),
            "sequence": seq,
            "name": name,
            "department": dept.name if dept else defn["department_name"],
            "branch": stage_branch,
            "isRequired": status != "Not Required",
            "weightPct": float(weight),
            "status": status,
            "completionPct": pct,
            "quantities": quantities,
            "document": document,
            "remarks": remarks,
            "startDateTime": stage_obj.start_datetime.isoformat() if stage_obj.start_datetime else None,
            "expectedCompletionDateTime": stage_obj.expected_completion_datetime.isoformat() if stage_obj.expected_completion_datetime else None,
            "actualCompletionDateTime": stage_obj.actual_completion_datetime.isoformat() if stage_obj.actual_completion_datetime else None,
        })

    # Recalculate project overall completion
    active_stages = [s for s in stages_data if s["status"] != "Not Required"]
    total_active_weight = sum(s["weightPct"] for s in active_stages) or 100
    weighted_progress = sum(s["completionPct"] * s["weightPct"] for s in active_stages)
    overall_pct = int(round(weighted_progress / total_active_weight)) if total_active_weight > 0 else 0

    project.overall_completion_pct = min(100, max(0, overall_pct))
    if overall_pct >= 100 and project.status != "Completed":
        project.status = "Completed"
    elif overall_pct > 0 and project.status in ("Draft", "Pending"):
        project.status = "In Progress"
    project.save(update_fields=["overall_completion_pct", "status", "updated_at"])

    return {
        "stockEvaluation": stock_eval,
        "stages": stages_data,
        "overallCompletionPct": project.overall_completion_pct,
        "status": project.status,
    }
