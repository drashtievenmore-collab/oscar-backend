"""
Project Stage Pipeline Services (Conditional Grey Stock Workflow).

Enforces the 10-stage end-to-end textile lifecycle defined in:
Oscar ERP - Grey Fabric Trading & Job Work Management Scope.

Route Branches:
- Branch A: Required grey fabric is available in Oscar inventory.
  Direct Job Work Outward to Vendor B (Dyeing/Processing).
  Stage 2 (Procurement If Required) is marked "Not Required".
- Branch B: Grey fabric is unavailable or in shortage.
  Stage 2 activates for the shortage quantity (PO -> Daily Production -> Transfer to Oscar).
- Partial: In-stock quantity allocated; shortage quantity routed through Branch B.
  Both converge cleanly at Stage 5 (Vendor B Processing).
"""
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone

from apps.core.money import round2, round4, D
from apps.pms.models import Project, ProjectStage, Department, CLOSED_STAGE_STATUSES


STAGE_1_DEF = {
    "name": "Grey Stock Check & Allocation",
    "department_name": "Inventory",
    "branch": "common",
    "default_weight": Decimal("10.00"),
    "icon": "Boxes",
}

STAGE_2_PROCUREMENT_DEF = {
    "name": "Procurement (If Required)",
    "department_name": "Purchase",
    "branch": "branch_b",
    "default_weight": Decimal("10.00"),
    "icon": "ShoppingCart",
}

PURCHASE_PO_STAGES = [
    {"name": "Create Grey Purchase PO", "department_name": "Purchase", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "FileText"},
    {"name": "Supplier Delivery", "department_name": "Purchase", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "Truck"},
    {"name": "GRN / Inward", "department_name": "Inventory", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "Boxes"},
    {"name": "Lot & Taka Creation", "department_name": "Inventory", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "Layers"},
    {"name": "Grey QC", "department_name": "Quality Control", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "ShieldCheck"},
    {"name": "Grey Stock Updated", "department_name": "Inventory", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "Building2"},
    {"name": "Supplier Bill Matching", "department_name": "Accounts", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "FileSpreadsheet"},
    {"name": "Payment", "department_name": "Accounts", "branch": "branch_purchase", "default_weight": Decimal("3.75"), "icon": "IndianRupee"},
]

VENDOR_PRODUCTION_STAGES = [
    # Purchase Order (Vendor A Production), then the client requirement PDF
    # section 05 flow -- External Agency Grey Production.
    {"name": "Purchase Order", "department_name": "Purchase", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "ShoppingCart"},
    {"name": "Production Work Instruction", "department_name": "Production", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "FileText"},
    {"name": "External Agency Assignment", "department_name": "Production", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "Users"},
    {"name": "Daily Production Entry", "department_name": "Production", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "ClipboardList"},
    {"name": "Production Completion", "department_name": "Production", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "CheckCircle2"},
    {"name": "Final Quantity Verification", "department_name": "Quality Control", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "ShieldCheck"},
    {"name": "Final Job Work PO", "department_name": "Production", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "FileText"},
    {"name": "Agency Bill Match", "department_name": "Accounts", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "FileSpreadsheet"},
    {"name": "Approval & Payable", "department_name": "Accounts", "branch": "branch_production", "default_weight": Decimal("3.00"), "icon": "IndianRupee"},
]

#: PMS production-branch stage -> Production Lifecycle stage key
#: (lifecycle_services.STAGE_KEYS). The lifecycle is the single source of truth.
PRODUCTION_LIFECYCLE_STAGE_MAP = {
    "Purchase Order": "purchase_order",
    "Production Work Instruction": "production_work_instruction",
    "External Agency Assignment": "external_agency_assignment",
    "Daily Production Entry": "daily_production_entry",
    "Production Completion": "production_completion",
    "Final Quantity Verification": "final_quantity_verification",
    "Final Job Work PO": "final_job_work_po",
    "Agency Bill Match": "agency_bill_match",
    "Approval & Payable": "approval_payable",
}

#: Old pipeline stage names -> the stage that replaced them (rows are renamed or merged, never dropped).
LEGACY_PRODUCTION_STAGE_NAMES = {
    "Assign Vendor A + Employee": "External Agency Assignment",
    "Generate Final Job Work PO": "Final Job Work PO",
    "Production / Job Work PO": "Final Job Work PO",
    "Agency Bill Match (PO + verified qty + invoice)": "Agency Bill Match",
    "Approval / Hold / Payment": "Approval & Payable",
    # Interim names used briefly before the PDF flow was applied.
    "Production Instruction": "Production Work Instruction",
    "Quality Control (QC)": "Final Quantity Verification",
    "Completion Verification": "Final Quantity Verification",
    "Disbursement Payment": "Approval & Payable",
    # Downstream stage renamed (the word "Dyeing" dropped).
    "Processing (Dyeing)": "Processing",
}


def _carry_over_legacy_production_stages(project, allowed_names):
    """Rename old production stage rows (or merge them into the new one) so
    their tasks, documents, approvals and delays survive -- the sync deletes
    any stage whose name is no longer in the pipeline, and those cascade."""
    from .models import Approval, Delay, Document, Task

    for old_name, new_name in LEGACY_PRODUCTION_STAGE_NAMES.items():
        if new_name not in allowed_names:
            continue
        for old in ProjectStage.objects.filter(project=project, name=old_name):
            target = ProjectStage.objects.filter(project=project, name=new_name).first()
            if target is None:
                old.name = new_name
                old.save(update_fields=["name"])
                continue
            for model in (Task, Document, Approval, Delay):
                model.objects.filter(stage=old).update(stage=target)
            if project.current_stage_id == old.id:
                project.current_stage = target
                project.save(update_fields=["current_stage"])
            old.delete()


def lifecycle_stage_status(name, lifecycle):
    """(status, pct, remarks, document) for a production-branch project stage."""
    key = PRODUCTION_LIFECYCLE_STAGE_MAP[name]
    stage = next(s for s in lifecycle["stages"] if s["key"] == key)
    state = stage["state"]
    if state == "completed":
        status, pct = "Completed", 100
    elif state == "active":
        status, pct = "In Progress", 50
        if key == "daily_production_entry" and stage.get("orderQuantity"):
            pct = int(min(99, max(1, stage["quantity"] / stage["orderQuantity"] * 100)))
    else:
        status, pct = "Pending", 0
    doc_number = stage.get("docNumber")
    remarks = " · ".join(str(v) for v in (doc_number, stage.get("status")) if v) or f"{name} pending"
    document = {
        "type": key,
        "number": doc_number or lifecycle["poNumber"] or lifecycle["instructionNumber"],
        "url": None,  # worked on inside the PMS project slide
        "label": f"{stage['label']}: {doc_number or lifecycle['poNumber'] or lifecycle['instructionNumber']}",
    }
    return status, pct, remarks, document

DOWNSTREAM_STAGES = [
    {"name": "Processing Plan & Vendor Assignment", "department_name": "Job Work", "branch": "common", "default_weight": Decimal("10.00"), "icon": "ClipboardList"},
    {"name": "Job Work Outward (Send to Vendor)", "department_name": "Job Work", "branch": "common", "default_weight": Decimal("10.00"), "icon": "Truck"},
    {"name": "Processing", "department_name": "Job Work", "branch": "common", "default_weight": Decimal("10.00"), "icon": "Cog"},
    {"name": "Job Work Inward (Receive)", "department_name": "Job Work", "branch": "common", "default_weight": Decimal("10.00"), "icon": "ArrowDownToLine"},
    {"name": "Quality Check & Stock Allocation", "department_name": "Quality Control", "branch": "common", "default_weight": Decimal("10.00"), "icon": "ShieldCheck"},
    {"name": "Customer Dispatch", "department_name": "Dispatch", "branch": "common", "default_weight": Decimal("10.00"), "icon": "Boxes"},
    {"name": "Invoice & Payment Collection", "department_name": "Accounts", "branch": "common", "default_weight": Decimal("10.00"), "icon": "IndianRupee"},
    {"name": "Project Closure", "department_name": "Management", "branch": "common", "default_weight": Decimal("10.00"), "icon": "CheckCircle2"},
]

def get_project_cfv(project):
    if not project:
        return {}
    val = getattr(project, "custom_field_values", None)
    if isinstance(val, dict):
        return val
    spec = getattr(project, "specifications", None) or ""
    if isinstance(spec, str) and spec.strip().startswith("{") and spec.strip().endswith("}"):
        try:
            import json
            return json.loads(spec)
        except Exception:
            return {}
    return {}

def get_project_stage_definitions(project=None, stock_eval=None):
    cfv = get_project_cfv(project)
    shortage_action = cfv.get("shortage_action")
    shortage_meters = stock_eval.get("shortage_meters", Decimal("0")) if stock_eval else Decimal("0")
    route_branch = stock_eval.get("route_branch") if stock_eval else "BRANCH_A"

    if (shortage_meters <= Decimal("0") and shortage_action is None) or route_branch == "BRANCH_A":
        raw_list = [STAGE_1_DEF] + DOWNSTREAM_STAGES
    elif shortage_action == "PURCHASE_PO":
        raw_list = [STAGE_1_DEF] + PURCHASE_PO_STAGES + DOWNSTREAM_STAGES
    elif shortage_action == "PRODUCTION_ORDER":
        raw_list = [STAGE_1_DEF] + VENDOR_PRODUCTION_STAGES + DOWNSTREAM_STAGES
    else:
        raw_list = [STAGE_1_DEF, STAGE_2_PROCUREMENT_DEF] + DOWNSTREAM_STAGES

    stages = []
    for idx, item in enumerate(raw_list):
        stages.append({
            **item,
            "sequence": idx + 1,
        })
    return stages

STAGE_DEFINITIONS = get_project_stage_definitions()


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
        line_items = getattr(order, "line_items", None)
        if line_items:
            line_sum = line_items.aggregate(s=Sum("qty"))["s"] or 0
            req_qty = Decimal(str(line_sum))

    if req_qty == Decimal("0"):
        req_qty = Decimal("0.00")

    from apps.inventory.services import calculate_combined_grey_stock, calculate_item_stock

    item = None
    if order:
        line_items = getattr(order, "line_items", None)
        first_line = line_items.first() if line_items else None
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

    cfv = get_project_cfv(project)
    from apps.inventory.models import GreyStockAllocation
    allocs = GreyStockAllocation.objects.filter(client_id=client_id, project=project, status="Active")
    cust_alloc = Decimal("0")
    own_alloc = Decimal("0")
    if allocs.exists():
        cust_alloc = sum((Decimal(str(a.quantity or 0)) for a in allocs if getattr(a, "allocation_type", "Customer Project") != "Own Project"), Decimal("0"))
        own_alloc = sum((Decimal(str(a.quantity or 0)) for a in allocs if getattr(a, "allocation_type", "Customer Project") == "Own Project"), Decimal("0"))
        allocated = cust_alloc + own_alloc
        shortage = max(Decimal("0"), req_qty - cust_alloc)
        if shortage <= Decimal("0"):
            branch = "BRANCH_A"
            label = "Branch A (100% Grey Stock Available in Warehouse)"
        elif cust_alloc > Decimal("0"):
            branch = "PARTIAL"
            label = f"Partial Availability ({cust_alloc} M Available, {shortage} M Shortage)"
        else:
            branch = "BRANCH_B"
            label = "Branch B (Stock Unavailable — Procurement Required)"
        return {
            "required_meters": round2(req_qty),
            "available_meters": round2(available_stock),
            "allocated_meters": round2(allocated),
            "customer_allocated_meters": round2(cust_alloc),
            "own_allocated_meters": round2(own_alloc),
            "shortage_meters": round2(shortage),
            "route_branch": branch,
            "route_label": label,
            "shortage_action": cfv.get("shortage_action"),
        }

    if "grey_stock_allocated_meters" in cfv:
        allocated = Decimal(str(cfv.get("grey_stock_allocated_meters") or 0))
        shortage = max(Decimal("0"), req_qty - allocated)
        if shortage <= Decimal("0"):
            branch = "BRANCH_A"
            label = "Branch A (100% Grey Stock Available in Warehouse)"
        elif allocated > Decimal("0"):
            branch = "PARTIAL"
            label = f"Partial Availability ({allocated} M Available, {shortage} M Shortage)"
        else:
            branch = "BRANCH_B"
            label = "Branch B (Stock Unavailable — Procurement Required)"
        return {
            "required_meters": round2(req_qty),
            "available_meters": round2(available_stock),
            "allocated_meters": round2(allocated),
            "customer_allocated_meters": round2(allocated),
            "own_allocated_meters": round2(Decimal("0")),
            "shortage_meters": round2(shortage),
            "route_branch": branch,
            "route_label": label,
            "shortage_action": cfv.get("shortage_action"),
        }

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
        "shortage_action": cfv.get("shortage_action"),
    }


@transaction.atomic
def sync_project_pipeline_stages(project):
    stock_eval = evaluate_stock_and_route(project)
    branch = stock_eval["route_branch"]
    req_qty = stock_eval["required_meters"]
    allocated_qty = stock_eval["allocated_meters"]
    shortage_qty = stock_eval["shortage_meters"]

    client_id = project.client_id
    order = project.sales_order
    cfv = get_project_cfv(project)

    stage_defs = get_project_stage_definitions(project, stock_eval)
    allowed_names = [s["name"] for s in stage_defs]

    _carry_over_legacy_production_stages(project, allowed_names)

    ProjectStage.objects.filter(project=project).filter(
        Q(name__iexact="dyeing")
        | Q(name__iexact="vbvb")
        | Q(name__icontains="customer requirement")
        | ~Q(name__in=allowed_names)
    ).delete()

    for st in ProjectStage.objects.filter(project=project):
        st.sequence = st.sequence + 1000
        st.save(update_fields=["sequence"])

    from apps.purchase.models import PurchaseOrder
    from apps.jobwork.models import JobWorkOrder, ProcessPlan
    from apps.inventory.models import QCInspection
    from apps.sales.models import DeliveryChallan, SalesInvoice

    linked_po_ids = cfv.get("linked_purchase_order_ids", [])
    po = PurchaseOrder.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
        Q(project=project) | Q(reference_number=project.code) | Q(reference_number=getattr(order, "order_number", "")) | Q(id__in=linked_po_ids) | Q(notes__icontains=project.code)
    ).exclude(purchase_type="Final Job Work").order_by("-created_at").first()

    if not po and cfv.get("grey_stock_po"):
        gpo = cfv.get("grey_stock_po")
        if gpo.get("id"):
            po = PurchaseOrder.objects.filter(client_id=client_id, id=gpo["id"], deleted_at__isnull=True).first()
        if not po and gpo.get("orderNumber"):
            po = PurchaseOrder.objects.filter(client_id=client_id, po_number=gpo["orderNumber"], deleted_at__isnull=True).first()

    plans_qs = ProcessPlan.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
        Q(project=project) | Q(remarks__icontains=project.code) | Q(plan_no__icontains=project.code)
    ).order_by("-created_at")
    # An approved plan (Active / Completed) wins over a draft.
    plan = plans_qs.filter(status__in=("Active", "Completed")).first() or plans_qs.first()

    jwos_qs = JobWorkOrder.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
        Q(project=project) | Q(remarks__icontains=project.code) | Q(jwo_no__icontains=project.code)
    ).order_by("-created_at")
    jwo = jwos_qs.first()

    jwo_sent = Decimal(str(project.dyeing_sent_meters or 0))
    jwo_received = Decimal(str(project.customer_project_received_meters or project.own_project_received_meters or 0))
    outward_challan = None
    all_outwards = []
    all_inwards = []

    if jwos_qs.exists():
        for order_obj in jwos_qs:
            outwards = getattr(order_obj, "outwards", None)
            if outwards and outwards.exists():
                all_outwards.extend(list(outwards.filter(deleted_at__isnull=True)))
            inwards = getattr(order_obj, "inwards", None)
            if inwards and inwards.exists():
                all_inwards.extend(list(inwards.filter(deleted_at__isnull=True)))

        if all_outwards:
            outward_challan = all_outwards[0]
            jwo_sent = sum((Decimal(str(getattr(o, "qty", 0) or getattr(o, "meter", 0) or 0)) for o in all_outwards), Decimal("0"))
        if all_inwards:
            jwo_received = sum((Decimal(str(getattr(i, "qty", 0) or getattr(i, "meter", 0) or 0)) for i in all_inwards), Decimal("0"))

    # Authoritative QC inspection lookup: match project's JWO inward receipts first, then project's PO or notes.
    qc = None
    qc_qs = QCInspection.objects.filter(client_id=client_id, deleted_at__isnull=True)
    inward_ids = [i.id for i in all_inwards]
    inward_nos = [i.no for i in all_inwards if getattr(i, "no", None)]
    if inward_ids or inward_nos:
        qc = qc_qs.filter(
            Q(source_document_type="JWO_INWARD", source_document_id__in=inward_ids)
            | Q(source_document_type="JWO_INWARD", source_document_number__in=inward_nos)
            | Q(notes__icontains=project.code)
        ).order_by("-inspection_date", "-created_at").first()
    if not qc and po:
        qc = qc_qs.filter(
            Q(purchase_order=po) | Q(source_document_id=po.id) | Q(notes__icontains=project.code)
        ).order_by("-inspection_date", "-created_at").first()
    if not qc:
        qc = qc_qs.filter(notes__icontains=project.code).order_by("-inspection_date", "-created_at").first()

    dispatched_qty = Decimal("0")
    dc_doc = None
    if order:
        challans = DeliveryChallan.objects.filter(sales_order=order, status__in=["Dispatched", "Delivered"])
        for ch in challans:
            line_items = getattr(ch, "line_items", None)
            if line_items:
                line_sum = line_items.aggregate(s=Sum("qty"))["s"] or 0
                dispatched_qty += Decimal(str(line_sum))
        dc_doc = challans.first()

    invoiced_amount = Decimal("0")
    paid_amount = Decimal("0")
    inv_doc = None
    if order:
        invoices = SalesInvoice.objects.filter(sales_order=order)
        for inv in invoices:
            invoiced_amount += Decimal(str(getattr(inv, "total", 0) or 0))
            paid_amount += Decimal(str(getattr(inv, "amount_paid", 0) or 0))
        inv_doc = invoices.first()

    production_lifecycle = None
    if any(d.get("branch") == "branch_production" for d in stage_defs):
        from .lifecycle_services import project_flow_lifecycles

        lifecycles = project_flow_lifecycles(project)
        production_lifecycle = lifecycles[0] if lifecycles else None

    departments_by_name = {d.name: d for d in Department.objects.filter(client_id=client_id)}

    stages_data = []

    for defn in stage_defs:
        seq = defn["sequence"]
        name = defn["name"]
        dept = departments_by_name.get(defn["department_name"])
        if not dept and defn.get("department_name"):
            dept, _ = Department.objects.get_or_create(
                client_id=client_id,
                name=defn["department_name"],
            )
            departments_by_name[defn["department_name"]] = dept

        stage_branch = defn["branch"]
        weight = defn["default_weight"]

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
        remarks = ""

        if name == "Grey Stock Check & Allocation":
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
            if allocated_qty > Decimal("0"):
                remarks = f"{int(allocated_qty):,} m allocated"
            else:
                remarks = "Stock check pending"

        elif name == "Create Grey Purchase PO":
            quantities["input"] = float(shortage_qty)
            po_data = cfv.get("grey_stock_po")
            if po and po.status != "Cancelled":
                status = "Completed"
                pct = 100
                po_num = po.po_number or f"PO-{po.id}"
                fl = po.line_items.first()
                po_qty = float(fl.qty) if fl else float(shortage_qty)
                quantities["good"] = po_qty
                remarks = f"PO {po_num} created"
                document = {
                    "type": "PurchaseOrder",
                    "number": po_num,
                    "url": f"/purchase/orders?id={po.id}",
                    "label": f"PO: {po_num}",
                }
            elif po_data and po_data.get("orderNumber"):
                status = "Completed"
                pct = 100
                po_num = po_data.get("orderNumber")
                po_qty = float(po_data.get("quantity") or shortage_qty)
                quantities["good"] = po_qty
                remarks = f"PO {po_num} created"
                document = {
                    "type": "PurchaseOrder",
                    "number": po_num,
                    "url": "/purchase/orders",
                    "label": f"PO: {po_num}",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "PO pending"

        elif name == "Supplier Bill Matching":
            quantities["input"] = float(shortage_qty)
            from apps.purchase.models import VendorBill
            vbill = None
            if po:
                vbill = VendorBill.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).first()
            if not vbill:
                vbill = VendorBill.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                    Q(remarks__icontains=project.code) | Q(purchase_order__reference_number=project.code)
                ).first()

            if vbill and getattr(vbill, "status", "") in ("Approved", "Paid", "Matched"):
                status = "Completed"
                pct = 100
                bill_no = getattr(vbill, "vendor_bill_number", None) or str(vbill.id)
                remarks = f"Bill {bill_no} matched"
                document = {
                    "type": "VendorBill",
                    "number": bill_no,
                    "url": f"/purchase/bill-matching?bill={vbill.id}",
                    "label": f"Bill: {bill_no}",
                }
            elif vbill:
                status = "In Progress"
                pct = 50
                bill_no = getattr(vbill, "vendor_bill_number", None) or str(vbill.id)
                remarks = f"Bill {bill_no} pending match"
                document = {
                    "type": "VendorBill",
                    "number": bill_no,
                    "url": f"/purchase/bill-matching?bill={vbill.id}",
                    "label": f"Bill: {bill_no}",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "Supplier bill matching pending"
                document = {
                    "type": "VendorBill",
                    "number": "Bill Match",
                    "url": "/purchase/bill-matching",
                    "label": "Bill Matching",
                }

        elif name == "Supplier Delivery":
            from apps.purchase.models import SupplierDelivery
            quantities["input"] = float(shortage_qty)
            deliv = None
            if po:
                deliv = SupplierDelivery.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).order_by("-dispatch_date").first()
            if not deliv:
                deliv = SupplierDelivery.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                    Q(supplier_challan_no__icontains=project.code) | Q(remarks__icontains=project.code)
                ).first()
            if deliv and deliv.status == "Delivered" and (shortage_qty <= Decimal("0") or Decimal(str(deliv.dispatched_qty or 0)) >= shortage_qty):
                status = "Completed"
                pct = 100
                quantities["good"] = float(deliv.dispatched_qty or 0)
                remarks = f"{deliv.delivery_number} Delivered ({float(deliv.dispatched_qty or 0):,} M)"
                document = {
                    "type": "SupplierDelivery",
                    "number": deliv.delivery_number,
                    "url": f"/purchase/supplier-delivery?po={po.id if po else ''}",
                    "label": f"Delivery: {deliv.delivery_number}",
                }
            elif deliv:
                status = "In Progress"
                pct = min(99, int(round((float(deliv.dispatched_qty or 0) / float(shortage_qty)) * 100))) if shortage_qty > Decimal("0") else 50
                quantities["good"] = float(deliv.dispatched_qty or 0)
                remarks = f"{deliv.delivery_number} {deliv.status} ({float(deliv.dispatched_qty or 0):,} M)"
                document = {
                    "type": "SupplierDelivery",
                    "number": deliv.delivery_number,
                    "url": f"/purchase/supplier-delivery?po={po.id if po else ''}",
                    "label": f"Delivery: {deliv.delivery_number}",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "Supplier delivery pending"
                document = {
                    "type": "SupplierDelivery",
                    "number": "Delivery Pending",
                    "url": f"/purchase/supplier-delivery?po={po.id if po else ''}",
                    "label": "Supplier Delivery",
                }

        elif name == "GRN / Inward":
            from apps.purchase.models import GoodsReceipt
            quantities["input"] = float(shortage_qty)
            grn = None
            if po:
                grn = GoodsReceipt.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).order_by("-receipt_date").first()
            if not grn:
                grn = GoodsReceipt.objects.filter(client_id=client_id, deleted_at__isnull=True).filter(
                    Q(supplier_challan_no__icontains=project.code) | Q(party_id=po.party_id if po else None)
                ).order_by("-receipt_date").first()
            if grn:
                rec_lines = grn.lines.all()
                total_rec = sum((Decimal(str(l.received_qty or 0)) for l in rec_lines), Decimal("0"))
                quantities["good"] = float(total_rec)
                if shortage_qty > Decimal("0") and total_rec >= shortage_qty:
                    status = "Completed"
                    pct = 100
                    remarks = f"GRN {grn.grn_number} fully received ({float(total_rec):,} M)"
                elif total_rec > Decimal("0"):
                    status = "In Progress"
                    pct = min(99, int(round((float(total_rec) / float(shortage_qty)) * 100))) if shortage_qty > Decimal("0") else 50
                    remarks = f"GRN {grn.grn_number} partially received ({float(total_rec):,} M / {float(shortage_qty):,} M)"
                else:
                    status = "In Progress"
                    pct = 20
                    remarks = f"GRN {grn.grn_number} pending intake"
                document = {
                    "type": "GoodsReceipt",
                    "number": grn.grn_number,
                    "url": f"/purchase/receipts?id={grn.id}",
                    "label": f"GRN: {grn.grn_number}",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "GRN inward pending"
                document = {
                    "type": "GoodsReceipt",
                    "number": "GRN Pending",
                    "url": "/purchase/receipts",
                    "label": "GRN Inward",
                }

        elif name in ("Lot & Taka Creation", "Lot / Taka Creation"):
            from apps.inventory.models import LotTaka
            from apps.purchase.models import GoodsReceipt
            quantities["input"] = float(shortage_qty)
            grn = None
            if po:
                grn = GoodsReceipt.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).first()
            takas = LotTaka.objects.filter(client_id=client_id, deleted_at__isnull=True)
            if grn:
                takas = takas.filter(Q(source_document_id=grn.id) | Q(source_document_number=grn.grn_number))
            else:
                takas = takas.filter(Q(lot_number__icontains=project.code) | Q(remarks__icontains=project.code))
            taka_count = takas.count()
            if taka_count > 0:
                taka_qty = sum((Decimal(str(t.received_quantity or 0)) for t in takas), Decimal("0"))
                quantities["good"] = float(taka_qty)
                if shortage_qty > Decimal("0") and taka_qty >= shortage_qty:
                    status = "Completed"
                    pct = 100
                    remarks = f"{taka_count} takas created ({float(taka_qty):,} M)"
                else:
                    status = "In Progress"
                    pct = min(99, int(round((float(taka_qty) / float(shortage_qty)) * 100))) if shortage_qty > Decimal("0") else 50
                    remarks = f"{taka_count} takas partially created ({float(taka_qty):,} M / {float(shortage_qty):,} M)"
                document = {
                    "type": "LotTaka",
                    "number": f"{taka_count} Takas",
                    "url": "/inventory/lots-takas",
                    "label": f"{taka_count} Takas",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "Lot / taka creation pending"
                document = {
                    "type": "LotTaka",
                    "number": "Takas Pending",
                    "url": "/inventory/lots-takas",
                    "label": "Lot & Taka",
                }

        elif name in ("Grey QC", "Grey Fabric QC"):
            from apps.purchase.models import GoodsReceipt
            quantities["input"] = float(shortage_qty)
            grns = GoodsReceipt.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True) if po else GoodsReceipt.objects.none()
            if grns.exists():
                all_approved = not grns.exclude(qc_status__in=("Approved", "Partially Approved")).exists()
                any_pending = grns.filter(qc_status__in=("Pending", "On Hold")).exists()
                if all_approved:
                    status = "Completed"
                    pct = 100
                    remarks = "QC completed for all receipts"
                    document = {
                        "type": "QCInspection",
                        "number": "Approved",
                        "url": "/inventory/qc",
                        "label": "QC: Approved",
                    }
                elif any_pending:
                    status = "In Progress"
                    pct = 50
                    remarks = "QC in progress"
                    document = {
                        "type": "QCInspection",
                        "number": "In Progress",
                        "url": "/inventory/qc",
                        "label": "QC: In Progress",
                    }
                else:
                    status = "Delayed"
                    pct = 30
                    remarks = "QC Rejected / Rework"
                    document = {
                        "type": "QCInspection",
                        "number": "Rejected",
                        "url": "/inventory/qc",
                        "label": "QC: Rejected",
                    }
            else:
                status = "Pending"
                pct = 0
                remarks = "Grey fabric QC pending"
                document = {
                    "type": "QCInspection",
                    "number": "QC Pending",
                    "url": "/inventory/qc",
                    "label": "Grey Fabric QC",
                }

        elif name == "Grey Stock Updated":
            from apps.purchase.models import GoodsReceipt
            quantities["input"] = float(shortage_qty)
            grns = GoodsReceipt.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True) if po else GoodsReceipt.objects.none()
            if grns.exists() and not grns.exclude(qc_status__in=("Approved", "Partially Approved")).exists():
                status = "Completed"
                pct = 100
                remarks = "Warehouse grey stock updated"
                document = {
                    "type": "StockPosition",
                    "number": "Stock Updated",
                    "url": "/inventory/stock-position",
                    "label": "Warehouse Stock",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "Grey stock update pending"
                document = {
                    "type": "StockPosition",
                    "number": "Stock Pending",
                    "url": "/inventory/stock-position",
                    "label": "Stock Position",
                }

        elif name in ("Payment", "Supplier Payment"):
            quantities["input"] = float(shortage_qty)
            from apps.purchase.models import VendorBill, PurchaseBill, PaymentOut
            vbill = None
            if po:
                vbill = VendorBill.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).first()
            pb = None
            if po:
                pb = PurchaseBill.objects.filter(client_id=client_id, purchase_order=po, deleted_at__isnull=True).first()
            is_paid = (vbill and vbill.status == "Paid") or (pb and pb.status == "Paid")
            if is_paid:
                status = "Completed"
                pct = 100
                remarks = "Supplier payment settled"
                document = {
                    "type": "PaymentOut",
                    "number": "Paid",
                    "url": "/purchase/payments",
                    "label": "Payment: Settled",
                }
            elif (vbill and vbill.status == "Approved") or (pb and pb.status in ("Approved", "Partially Paid")):
                status = "In Progress"
                pct = 50
                remarks = "Payment approved / pending disbursement"
                document = {
                    "type": "PaymentOut",
                    "number": "Approved",
                    "url": "/purchase/payments",
                    "label": "Payment Pending",
                }
            else:
                status = "Pending"
                pct = 0
                remarks = "Payment pending"
                document = {
                    "type": "PaymentOut",
                    "number": "Pending",
                    "url": "/purchase/payments",
                    "label": "Supplier Payment",
                }


        elif name in PRODUCTION_LIFECYCLE_STAGE_MAP and production_lifecycle:
            if name == "Purchase Order":
                quantities["input"] = float(shortage_qty)
            status, pct, remarks, document = lifecycle_stage_status(name, production_lifecycle)

        elif name == "Procurement (If Required)":
            if branch == "BRANCH_A":
                status = "Not Required"
                pct = 100
                weight = Decimal("0.00")
                remarks = "Stock available"
            else:
                quantities["input"] = float(shortage_qty)
                if po and po.status in ("Received", "Fully Received", "Closed"):
                    status = "Completed"
                    pct = 100
                    quantities["good"] = float(shortage_qty)
                    remarks = f"Received PO {po.po_number}"
                elif po:
                    status = "In Progress"
                    pct = 50
                    remarks = f"PO {po.po_number} in progress"
                else:
                    status = "Pending"
                    pct = 0
                    remarks = "Procurement required"
                if po:
                    document = {
                        "type": "PurchaseOrder",
                        "number": po.po_number or str(po.id),
                        "url": f"/purchase/orders?id={po.id}",
                        "label": f"PO: {po.po_number}",
                    }

        elif name == "Processing Plan & Vendor Assignment":
            quantities["input"] = float(req_qty)
            if plan or jwo:
                # Requirement PDF section 07: a plan needs its approver; a
                # draft plan is in progress, an approved one (or a job order)
                # completes the stage.
                if jwo or plan.status in ("Active", "Completed"):
                    status = "Completed"
                    pct = 100
                else:
                    status = "In Progress"
                    pct = 50
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
            if jwo and getattr(jwo, "vendor_name", None):
                remarks = f"{jwo.vendor_name} assigned"
            elif plan and getattr(plan, "vendor_name", None):
                remarks = f"{plan.vendor_name} assigned"
            elif plan or jwo:
                remarks = "Vendor B assigned"
            else:
                remarks = "Processing plan pending"

        elif name == "Job Work Outward (Send to Vendor)":
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
            if outward_challan and getattr(outward_challan, "challan_no", None):
                remarks = f"Challan {outward_challan.challan_no}"
            elif jwo and getattr(jwo, "jwo_no", None):
                remarks = f"JWO {jwo.jwo_no}"
            else:
                remarks = "Outward pending"

        elif name == "Processing":
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
            vendor = (jwo and getattr(jwo, "vendor_name", None)) or "Vendor B"
            if jwo_received >= jwo_sent and jwo_sent > Decimal("0"):
                remarks = f"Dyeing completed at {vendor}"
            elif jwo_sent > Decimal("0"):
                remarks = f"Dyeing in process at {vendor}"
            else:
                remarks = "Processing pending"

        elif name == "Job Work Inward (Receive)":
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
            if jwo_received >= req_qty and req_qty > Decimal("0"):
                remarks = f"Received full {int(jwo_received):,} m"
            elif jwo_received > Decimal("0"):
                remarks = f"Received {int(jwo_received):,} m"
            else:
                remarks = "Inward pending"

        elif name == "Quality Check & Stock Allocation":
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
            if qc and qc.status in ("Approved", "Completed", "Passed"):
                remarks = f"QC Passed {int(qc.passed_qty or jwo_received):,} m"
            elif qc:
                remarks = "QC inspection in progress"
            else:
                remarks = "QC pending"

        elif name == "Customer Dispatch":
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
            if dispatched_qty >= req_qty and req_qty > Decimal("0"):
                remarks = f"Dispatched {int(dispatched_qty):,} m"
            elif dispatched_qty > Decimal("0"):
                remarks = f"Dispatched {int(dispatched_qty):,} m"
            else:
                remarks = "Dispatch pending"

        elif name == "Invoice & Payment Collection":
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
            if paid_amount >= invoiced_amount and invoiced_amount > Decimal("0"):
                remarks = "Payment received"
            elif invoiced_amount > Decimal("0"):
                remarks = f"Invoiced ₹{int(invoiced_amount):,}"
            else:
                remarks = "Invoice pending"

        elif name == "Project Closure":
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
            if project.status == "Completed":
                remarks = "Project closed"
            else:
                remarks = "Closure pending"

        stage_start = project.start_date if status != "Pending" else None
        stage_expected = project.expected_completion_date if status != "Pending" else None

        stage_obj = ProjectStage.objects.filter(project=project, name=name).first()
        if not stage_obj:
            stage_obj = ProjectStage.objects.create(
                project=project,
                sequence=seq,
                client_id=client_id,
                name=name,
                department=dept,
                weight_pct=weight,
                status=status,
                completion_pct=pct,
                start_datetime=stage_start,
                expected_completion_datetime=stage_expected,
            )
        else:
            stage_obj.sequence = seq
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
            stage_obj.save(update_fields=["sequence", "name", "weight_pct", "status", "completion_pct", "department", "start_datetime", "expected_completion_datetime", "updated_at"])

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

    ProjectStage.objects.filter(project=project, sequence__gte=1000).delete()

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
