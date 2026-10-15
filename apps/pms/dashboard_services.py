"""
PMS overview dashboard -- ``GET /pms/dashboard/overview/``.

One payload for the textile-flow dashboard: KPI tiles, the stage overview
(quantity in meters per configured stage), key alerts, stage-wise quantity,
the project status split and the most recent projects.

Every figure is read from live tables; nothing here is seeded or estimated.

  * Projects, stages and quantities  -- pms_projects / pms_project_stages
  * In production / pending / grey inward -- prod_instructions + daily entries
  * In processing                    -- jobwork_orders + jobwork_inwards
  * Finished stock                   -- stock_balances (fabric items)
  * Pending dispatch                 -- open sales order lines (qty - dispatched)
  * Outstanding / overdue payment    -- sales invoices (total - amount_paid)
  * QC hold                          -- goods receipts with qc_status "On Hold"

Filters (all optional): ``dateFrom`` / ``dateTo`` (ISO dates) narrow projects
by start date and documents by their document date; ``locationId`` narrows the
location-bound figures (finished stock, invoices, QC hold).
"""
from datetime import date

from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.core.exceptions import ValidationFailed
from apps.core.money import D, ZERO

from .models import (
    CLOSED_STAGE_STATUSES,
    DailyProductionEntry,
    Project,
    ProjectStage,
    ProductionInstruction,
    StageConfig,
)

DEC = DecimalField(max_digits=18, decimal_places=4)
#: Location types that hold sellable stock -- transit and scrap do not.
STOCK_LOCATION_EXCLUDE = ("Transit", "Scrap")
#: Sales order stages whose open lines still await dispatch.
DISPATCH_PENDING_STAGES = ("Confirmed", "Packing", "Dispatched")
RECENT_PROJECT_LIMIT = 5


def _num(value):
    """Decimal -> float for JSON, rounded to 2 places."""
    return float(round(D(value), 2))


def _sum(queryset, expression):
    return queryset.aggregate(
        value=Coalesce(Sum(expression, output_field=DEC), Value(ZERO, output_field=DEC))
    )["value"]


def parse_filters(params):
    def parse_date(key):
        raw = params.get(key)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValidationFailed(
                "Invalid date.", field_errors={key: ["Expected YYYY-MM-DD."]}
            )

    date_from = parse_date("dateFrom")
    date_to = parse_date("dateTo")
    if date_from and date_to and date_from > date_to:
        raise ValidationFailed(
            "Start date is after end date.",
            field_errors={"dateFrom": ["Must be on or before dateTo."]},
        )
    return {
        "date_from": date_from,
        "date_to": date_to,
        "location_id": params.get("locationId") or None,
    }


def _date_range(queryset, field, date_from, date_to):
    if date_from:
        queryset = queryset.filter(**{f"{field}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{field}__lte": date_to})
    return queryset


def _location_ids(client_id, location_id):
    """The chosen location plus its descendants (a warehouse holds its racks)."""
    from apps.masters.models import Location

    if not location_id:
        return None
    ids = {str(location_id)}
    frontier = [location_id]
    while frontier:
        children = list(
            Location.objects.filter(
                client_id=client_id, parent_id__in=frontier, deleted_at__isnull=True
            ).values_list("id", flat=True)
        )
        frontier = [c for c in children if str(c) not in ids]
        ids.update(str(c) for c in frontier)
    return list(ids)


# ---------------------------------------------------------------------------
# Projects and stages
# ---------------------------------------------------------------------------
def _projects(client_id, date_from, date_to):
    queryset = Project.objects.filter(client_id=client_id, deleted_at__isnull=True).annotate(
        effective_start=Coalesce(TruncDate("start_date"), TruncDate("created_at"))
    )
    return _date_range(queryset, "effective_start", date_from, date_to)


def _stage_columns(client_id, stages):
    """Columns for the stage overview: active stage templates in sequence,
    then any other stage names actually used on projects (stages added to a
    project by hand), so the overview always mirrors the real pipeline.
    """
    configs = list(
        StageConfig.objects.filter(
            client_id=client_id, deleted_at__isnull=True, is_active=True
        ).order_by("sequence", "name")
    )
    columns = [
        {"key": f"cfg:{c.id}", "configId": str(c.id), "name": c.name} for c in configs
    ]
    config_ids = {c["configId"] for c in columns}
    known_names = {c["name"].strip().lower() for c in columns}
    for stage in sorted(stages, key=lambda s: s.sequence):
        if stage.stage_config_id and str(stage.stage_config_id) in config_ids:
            continue
        key = stage.name.strip().lower()
        if key not in known_names:
            known_names.add(key)
            columns.append({"key": f"name:{key}", "configId": None, "name": stage.name})
    return columns


def _column_key_for(stage, by_config, by_name):
    if stage.stage_config_id and str(stage.stage_config_id) in by_config:
        return by_config[str(stage.stage_config_id)]
    return by_name.get(stage.name.strip().lower())


def _current_stage(project, project_stages):
    if project.current_stage_id:
        for stage in project_stages:
            if stage.id == project.current_stage_id:
                return stage
    for stage in project_stages:
        if stage.status not in CLOSED_STAGE_STATUSES:
            return stage
    return None


def _project_section(client_id, projects):
    project_list = list(projects.order_by("-created_at"))
    project_ids = [p.id for p in project_list]
    stages = list(
        ProjectStage.objects.filter(
            client_id=client_id, project_id__in=project_ids, deleted_at__isnull=True
        ).order_by("project_id", "sequence")
    )
    stages_by_project = {}
    for stage in stages:
        stages_by_project.setdefault(stage.project_id, []).append(stage)

    columns = _stage_columns(client_id, stages)
    by_config = {c["configId"]: c["key"] for c in columns if c["configId"]}
    by_name = {c["name"].strip().lower(): c["key"] for c in columns}
    tally = {
        c["key"]: {"qty": ZERO, "projects": 0, "completed": ZERO, "inProgress": ZERO, "pending": ZERO}
        for c in columns
    }

    total = len(project_list)
    status_counts = {}
    total_qty = ZERO
    current_by_project = {}

    for project in project_list:
        qty = D(project.quantity)
        total_qty += qty
        status_counts[project.status] = status_counts.get(project.status, 0) + 1

        project_stages = stages_by_project.get(project.id, [])
        current = _current_stage(project, project_stages)
        current_by_project[project.id] = current

        if current is not None:
            key = _column_key_for(current, by_config, by_name)
            if key:
                tally[key]["qty"] += qty
                tally[key]["projects"] += 1

        for stage in project_stages:
            key = _column_key_for(stage, by_config, by_name)
            if not key:
                continue
            if stage.status in CLOSED_STAGE_STATUSES:
                tally[key]["completed"] += qty
            elif current is not None and stage.id == current.id:
                tally[key]["inProgress"] += qty
            else:
                tally[key]["pending"] += qty

    stage_overview = [
        {
            "key": c["key"],
            "name": c["name"],
            "sequence": index + 1,
            "quantity": _num(tally[c["key"]]["qty"]),
            "projectCount": tally[c["key"]]["projects"],
            "totalProjects": total,
        }
        for index, c in enumerate(columns)
    ]
    stage_quantity = [
        {
            "key": c["key"],
            "name": c["name"],
            "completed": _num(tally[c["key"]]["completed"]),
            "inProgress": _num(tally[c["key"]]["inProgress"]),
            "pending": _num(tally[c["key"]]["pending"]),
        }
        for c in columns
    ]

    recent = []
    for project in project_list[:RECENT_PROJECT_LIMIT]:
        current = current_by_project.get(project.id)
        recent.append(
            {
                "id": str(project.id),
                "code": project.code,
                "customerName": project.customer_name,
                "product": project.product_name or project.specifications,
                "quantity": _num(project.quantity),
                "currentStage": current.name if current else None,
                "completionPct": project.overall_completion_pct,
                "expectedCompletionDate": project.expected_completion_date,
                "status": project.status,
            }
        )

    return {
        "total": total,
        "inProgress": status_counts.get("In Progress", 0),
        "totalQuantity": total_qty,
        "statusCounts": status_counts,
        "stageOverview": stage_overview,
        "stageQuantity": stage_quantity,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# Production (grey fabric) -- prod_instructions
# ---------------------------------------------------------------------------
def _production_section(client_id, date_from, date_to):
    instructions = ProductionInstruction.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).annotate(doc_date=Coalesce("pi_date", TruncDate("created_at")))
    instructions = _date_range(instructions, "doc_date", date_from, date_to)

    produced = {
        row["instruction_id"]: row["value"]
        for row in DailyProductionEntry.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            instruction__in=instructions,
        )
        .values("instruction_id")
        .annotate(value=Sum("meters"))
    }

    in_production = ZERO
    pending_qty = ZERO
    pending_count = 0
    grey_qty = ZERO
    grey_count = 0
    for row in instructions.values("id", "status", "order_meter"):
        made = D(produced.get(row["id"]))
        if row["status"] == "In Progress":
            in_production += D(row["order_meter"])
        if row["status"] in ("Draft", "In Progress"):
            balance = D(row["order_meter"]) - made
            if balance > ZERO:
                pending_qty += balance
                pending_count += 1
        elif row["status"] == "Completed":
            # Produced but not yet verified -- awaiting grey inward / QC.
            grey_qty += made
            grey_count += 1

    return {
        "inProduction": in_production,
        "pendingProduction": {"quantity": _num(pending_qty), "count": pending_count},
        "greyInwardPending": {"quantity": _num(grey_qty), "count": grey_count},
    }


# ---------------------------------------------------------------------------
# Processing -- jobwork_orders
# ---------------------------------------------------------------------------
def _processing_qty(client_id, date_from, date_to):
    from apps.jobwork.models import JobWorkOrder, JWOInward

    orders = _date_range(
        JobWorkOrder.objects.filter(
            client_id=client_id, deleted_at__isnull=True, status="In-Process"
        ),
        "order_date",
        date_from,
        date_to,
    )
    received = {
        row["job_work_order_id"]: row["value"]
        for row in JWOInward.objects.filter(
            client_id=client_id, deleted_at__isnull=True, job_work_order__in=orders
        )
        .values("job_work_order_id")
        .annotate(value=Sum("qty"))
    }
    total = ZERO
    for row in orders.values("id", "planned_qty"):
        open_qty = D(row["planned_qty"]) - D(received.get(row["id"]))
        if open_qty > ZERO:
            total += open_qty
    return total


# ---------------------------------------------------------------------------
# Stock, dispatch, payments and QC
# ---------------------------------------------------------------------------
def _finished_stock(client_id, location_ids):
    from apps.inventory.models import StockBalance

    balances = StockBalance.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        item__item_kind="Fabric",
        item__deleted_at__isnull=True,
        on_hand__gt=0,
    ).exclude(location__type__in=STOCK_LOCATION_EXCLUDE)
    if location_ids is not None:
        balances = balances.filter(location_id__in=location_ids)
    return _sum(balances, "on_hand")


def _pending_dispatch(client_id, date_from, date_to):
    from apps.sales.models import SalesOrderLine

    lines = SalesOrderLine.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        sales_order__deleted_at__isnull=True,
        sales_order__stage__in=DISPATCH_PENDING_STAGES,
        qty__gt=F("dispatched_qty"),
    )
    lines = _date_range(lines, "sales_order__doc_date", date_from, date_to)
    return _sum(lines, F("qty") - F("dispatched_qty"))


def _payments(client_id, date_from, date_to, location_ids):
    from apps.sales.models import SalesInvoice

    invoices = SalesInvoice.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        status__in=("Unpaid", "Partially Paid"),
        total__gt=F("amount_paid"),
    )
    invoices = _date_range(invoices, "doc_date", date_from, date_to)
    if location_ids is not None:
        invoices = invoices.filter(location_id__in=location_ids)

    outstanding = _sum(invoices, F("total") - F("amount_paid"))
    overdue = invoices.filter(due_date__lt=timezone.localdate())
    return {
        "outstanding": outstanding,
        "overdue": {
            "amount": _num(_sum(overdue, F("total") - F("amount_paid"))),
            "count": overdue.count(),
        },
    }


def _qc_hold(client_id, date_from, date_to, location_ids):
    from apps.purchase.models import GoodsReceipt, GoodsReceiptLine

    receipts = GoodsReceipt.objects.filter(
        client_id=client_id, deleted_at__isnull=True, qc_status="On Hold"
    )
    receipts = _date_range(receipts, "receipt_date", date_from, date_to)
    if location_ids is not None:
        receipts = receipts.filter(location_id__in=location_ids)
    lines = GoodsReceiptLine.objects.filter(
        client_id=client_id, deleted_at__isnull=True, goods_receipt__in=receipts
    )
    return {"quantity": _num(_sum(lines, "received_qty")), "count": receipts.count()}


def _warehouses(client_id):
    from apps.masters.models import Location

    return [
        {"id": str(row["id"]), "name": row["name"], "code": row["code"]}
        for row in Location.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            type__in=("Warehouse", "Godown"),
        )
        .order_by("name")
        .values("id", "name", "code")
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def dashboard_overview(client_id, params):
    filters = parse_filters(params)
    date_from, date_to = filters["date_from"], filters["date_to"]
    location_ids = _location_ids(client_id, filters["location_id"])

    project = _project_section(client_id, _projects(client_id, date_from, date_to))
    production = _production_section(client_id, date_from, date_to)
    payments = _payments(client_id, date_from, date_to, location_ids)

    return {
        "filters": {
            "dateFrom": date_from,
            "dateTo": date_to,
            "locationId": filters["location_id"],
        },
        "warehouses": _warehouses(client_id),
        "kpis": {
            "totalProjects": project["total"],
            "inProgressProjects": project["inProgress"],
            "totalQuantity": _num(project["totalQuantity"]),
            "inProduction": _num(production["inProduction"]),
            "inProcessing": _num(_processing_qty(client_id, date_from, date_to)),
            "finishedStock": _num(_finished_stock(client_id, location_ids)),
            "pendingDispatch": _num(_pending_dispatch(client_id, date_from, date_to)),
            "outstandingPayment": _num(payments["outstanding"]),
        },
        "stageOverview": project["stageOverview"],
        "alerts": {
            "pendingProduction": production["pendingProduction"],
            "greyInwardPending": production["greyInwardPending"],
            "qcHold": _qc_hold(client_id, date_from, date_to, location_ids),
            "overduePayment": payments["overdue"],
        },
        "stageQuantity": project["stageQuantity"],
        "projectStatus": [
            {"status": status, "count": count}
            for status, count in sorted(
                project["statusCounts"].items(), key=lambda item: -item[1]
            )
        ],
        "recentProjects": project["recent"],
    }
