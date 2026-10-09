"""
PMS services (api.md §10).

Everything ``stores/pmsStore.js`` computes locally and must stop computing:
the completion rollups, the handoff gate, overdue/at-risk detection, the
dashboard metrics, nav badges and the delay watchlist.

The handoff gate is the load-bearing piece. api.md §10.3 and db.md §10.5 both
insist that ``GET .../handoff-check/`` and ``POST .../handoff/`` run *exactly*
the same query -- implemented once here and called from both, or they drift.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D
from apps.core.permissions import require_permission

from .models import (
    CLOSED_STAGE_STATUSES,
    Approval,
    Delay,
    Document,
    Project,
    ProjectStage,
    Settings,
    Task,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_settings(client_id):
    """The ``/pms/settings/`` row, created with the api.md §10.1 defaults."""
    settings, created = Settings.objects.get_or_create(
        client_id=client_id,
        defaults={
            "notifications": Settings.DEFAULT_NOTIFICATIONS,
            "department_capacity": Settings.DEFAULT_DEPARTMENT_CAPACITY,
            "status_colors": Settings.DEFAULT_STATUS_COLORS,
            "delay_categories": Settings.DEFAULT_DELAY_CATEGORIES,
        },
    )
    return settings


def settings_payload(client_id):
    settings = get_settings(client_id)
    return {
        "atRiskThresholdPct": settings.at_risk_threshold_pct,
        "requireClientApprovalOnDesign": settings.require_client_approval_on_design,
        "requireQaCertificate": settings.require_qa_certificate,
        "notifications": settings.notifications or Settings.DEFAULT_NOTIFICATIONS,
        "defaultDepartmentCapacity": settings.default_department_capacity,
        "departmentCapacity": settings.department_capacity or {},
        "statusColors": settings.status_colors or Settings.DEFAULT_STATUS_COLORS,
        "delayCategories": settings.delay_categories or Settings.DEFAULT_DELAY_CATEGORIES,
    }


# ---------------------------------------------------------------------------
# Rollups (api.md §10.4)
# ---------------------------------------------------------------------------
@transaction.atomic
def recalculate_stage(stage):
    """A stage's ``completion_pct`` is recomputed from its tasks."""
    rows = Task.objects.filter(stage=stage, deleted_at__isnull=True).aggregate(
        count=Count("id"), average=Avg("completion_pct")
    )
    if rows["count"]:
        stage.completion_pct = int(round(rows["average"] or 0))
    # With no tasks the stage keeps whatever progress was set directly, because
    # `POST .../progress/` is a legitimate way to drive a stage that is not
    # task-managed (api.md §10.3).
    stage.save(update_fields=["completion_pct", "updated_at"])
    return stage


@transaction.atomic
def recalculate_project(project):
    """A project's ``overall_completion_pct`` is the rollup of its stages.

    Uses configured stage percentage weights (total 100%) when configured;
    falls back safely to unweighted average for projects without weights.
    """
    stages = list(
        ProjectStage.objects.filter(project=project, deleted_at__isnull=True)
    )
    if not stages:
        project.overall_completion_pct = 0
    else:
        total_weight = sum(s.weight_pct for s in stages)
        if total_weight > 0:
            weighted_sum = sum(
                (s.completion_pct or 0) * (s.weight_pct or 0) for s in stages
            )
            project.overall_completion_pct = int(
                round(float(weighted_sum) / float(total_weight))
            )
        else:
            project.overall_completion_pct = int(
                round(sum(s.completion_pct or 0 for s in stages) / len(stages))
            )

    project.status = derive_project_status(project)
    project.save(update_fields=["overall_completion_pct", "status", "updated_at"])
    return project


def recalculate_all(stage):
    """Both rollups, in order. api.md §10.4 asks for the recalculated parents
    to come back in the response so the UI does not have to refetch."""
    stage = recalculate_stage(stage)
    project = recalculate_project(stage.project)
    return stage, project


def derive_project_status(project, now=None):
    """Overdue and at-risk are computed, never stored (db.md §12)."""
    if project.status in ("Draft", "On Hold"):
        return project.status
    if project.actual_completion_date is not None:
        return "Completed"

    now = now or timezone.now()
    stages = list(project.stages.filter(deleted_at__isnull=True))
    if not stages:
        return project.status

    if Delay.objects.filter(
        project=project, resolved_at__isnull=True, deleted_at__isnull=True
    ).exists():
        return "Delayed"

    if (
        project.expected_completion_date
        and project.expected_completion_date < now
    ):
        return "Delayed"

    threshold = get_settings(project.client_id).at_risk_threshold_pct
    if any(is_stage_at_risk(stage, threshold, now) for stage in stages):
        return "At Risk"

    return "In Progress"


def is_stage_at_risk(stage, threshold_pct, now=None):
    """api.md §10.1 -- ``atRiskThresholdPct`` of the planned window has elapsed
    **and** completion is under 50%. Both halves are required."""
    now = now or timezone.now()
    if stage.status in CLOSED_STAGE_STATUSES:
        return False
    if not stage.start_datetime or not stage.expected_completion_datetime:
        return False
    if stage.completion_pct >= 50:
        return False

    window = (stage.expected_completion_datetime - stage.start_datetime).total_seconds()
    if window <= 0:
        return False
    elapsed = (now - stage.start_datetime).total_seconds()
    return (elapsed / window) * 100 >= float(threshold_pct)


def is_stage_overdue(stage, now=None):
    now = now or timezone.now()
    return bool(
        stage.expected_completion_datetime
        and stage.expected_completion_datetime < now
        and stage.status not in CLOSED_STAGE_STATUSES
    )


def stage_expected_completion(start, duration, unit):
    if start is None or duration is None:
        return None
    hours = float(duration) * (1 if unit == "Hours" else 24)
    return start + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# The handoff gate (api.md §10.3)
# ---------------------------------------------------------------------------
def handoff_blockers(stage, *, settings=None):
    """Returns ``[{ code, label, hard }]``.

    ``hard: true`` blocks the action; ``hard: false`` is a warning the user may
    proceed past. api.md Appendix C notes these are returned **as data** from
    ``/handoff-check/``, not as errors.
    """
    settings = settings or get_settings(stage.client_id)
    blockers = []

    if stage.status == "Not Started" or stage.start_datetime is None:
        blockers.append(
            {"code": Codes.NOT_STARTED, "label": "This stage has not been started.", "hard": True}
        )

    if stage.completion_pct < 100:
        blockers.append(
            {
                "code": Codes.INCOMPLETE,
                "label": f"Stage is {stage.completion_pct}% complete.",
                "hard": False,
            }
        )

    if Task.objects.filter(
        stage=stage, status="Blocked", deleted_at__isnull=True
    ).exists():
        blockers.append(
            {"code": Codes.BLOCKED_TASKS, "label": "A task on this stage is blocked.", "hard": True}
        )

    if Delay.objects.filter(
        stage=stage, resolved_at__isnull=True, deleted_at__isnull=True
    ).exists():
        blockers.append(
            {"code": Codes.OPEN_DELAY, "label": "An open delay must be resolved.", "hard": True}
        )

    has_document = Document.objects.filter(stage=stage, deleted_at__isnull=True).exists()
    if stage.required_document and not has_document:
        blockers.append(
            {
                "code": Codes.NEEDS_DOCUMENT,
                "label": "This stage requires a document.",
                "hard": True,
            }
        )

    has_approval = Approval.objects.filter(
        stage=stage, status="Approved", deleted_at__isnull=True
    ).exists()
    if stage.required_approval and not has_approval:
        blockers.append(
            {
                "code": Codes.NEEDS_APPROVAL,
                "label": "This stage requires an approval.",
                "hard": True,
            }
        )

    # The two POLICY_* codes are global settings floors that apply on top of
    # each template's own flags -- a template may demand more, but these cannot
    # be bypassed by leaving a box unticked (api.md §10.3).
    department_name = stage.department.name if stage.department_id else ""
    if (
        settings.require_client_approval_on_design
        and "design" in (department_name or stage.name or "").lower()
        and Document.objects.filter(stage=stage, is_proof=True, deleted_at__isnull=True).exists()
        and not has_approval
    ):
        blockers.append(
            {
                "code": Codes.POLICY_DESIGN_APPROVAL,
                "label": "Company policy requires client approval on a design proof.",
                "hard": True,
            }
        )

    if (
        settings.require_qa_certificate
        and "quality" in (department_name or stage.name or "").lower()
        and not has_document
    ):
        blockers.append(
            {
                "code": Codes.POLICY_QA_CERTIFICATE,
                "label": "Company policy requires a QA certificate on the Quality stage.",
                "hard": True,
            }
        )

    return blockers


def hard_blockers(blockers):
    return [blocker for blocker in blockers if blocker["hard"]]


@transaction.atomic
def handoff_stage(stage, *, user=None, force=False, comments=None):
    """``POST .../handoff/`` -- runs exactly the gate above, then advances."""
    stage = ProjectStage.objects.select_for_update().get(pk=stage.pk)
    blockers = handoff_blockers(stage)
    blocking = hard_blockers(blockers)

    if blocking and not force:
        raise BusinessRuleViolation(
            blocking[0]["label"],
            code=blocking[0]["code"],
            detail="; ".join(b["label"] for b in blocking),
            payload={"blockers": blockers},
        )
    if blocking and force:
        require_permission(user, "handoff_stage", "Forcing a handoff needs permission.")

    previous_status = stage.status
    stage.status = "Completed"
    stage.actual_completion_datetime = timezone.now()
    stage.completion_pct = 100
    stage.save(
        update_fields=["status", "actual_completion_datetime", "completion_pct", "updated_at"]
    )

    next_stage = (
        ProjectStage.objects.filter(
            project=stage.project, sequence__gt=stage.sequence, deleted_at__isnull=True
        )
        .order_by("sequence")
        .first()
    )

    project = stage.project
    if next_stage is not None:
        project.current_stage = next_stage
        project.current_department = next_stage.department
        if next_stage.status == "Not Started" and next_stage.assigned_user_id:
            next_stage.status = "Assigned"
            next_stage.save(update_fields=["status", "updated_at"])
        project.save(update_fields=["current_stage", "current_department", "updated_at"])

    recalculate_project(project)

    record_audit(
        client=stage.client_id,
        actor=user,
        action="STAGE_HANDOFF",
        entity_type="PmsStage",
        entity_id=stage.id,
        entity_label=stage.name,
        description=f"Handed off {stage.name}"
        + (f" to {next_stage.name}" if next_stage else " (final stage)"),
        from_value=previous_status,
        to_value="Completed",
        comments=comments,
    )

    if next_stage is not None and next_stage.assigned_user_id:
        notify(
            client=stage.client_id,
            recipients=[next_stage.assigned_user_id],
            type="pms.stage_assigned",
            category="pms",
            title=f"{next_stage.name} is ready to start",
            body=f"{project.code} has been handed off to your stage.",
            entity_type="PmsStage",
            entity_id=next_stage.id,
            actor=user,
        )

    return {"stage": stage, "nextStage": next_stage, "project": project, "blockers": blockers}


def completion_blockers(project):
    """api.md §10.3 -- project completion has its own gates."""
    blockers = []
    stages = list(project.stages.filter(deleted_at__isnull=True))
    if not stages:
        blockers.append(
            {"code": Codes.NO_STAGES, "label": "No stages are configured.", "hard": True}
        )
    open_stages = [s for s in stages if s.status not in CLOSED_STAGE_STATUSES]
    if open_stages:
        blockers.append(
            {
                "code": Codes.STAGE_OPEN,
                "label": f"{len(open_stages)} stage(s) are still open.",
                "hard": True,
            }
        )
    return blockers


@transaction.atomic
def complete_project(project, *, user=None, force=False):
    project = Project.objects.select_for_update().get(pk=project.pk)
    if project.status == "Completed":
        raise Conflict(
            "This project is already completed.", code=Codes.ALREADY_COMPLETED
        )

    blockers = completion_blockers(project)
    blocking = hard_blockers(blockers)
    if blocking and not force:
        raise BusinessRuleViolation(
            blocking[0]["label"],
            code=blocking[0]["code"],
            payload={"blockers": blockers},
        )
    if blocking and force:
        require_permission(user, "complete_project", "Forcing completion needs permission.")

    project.status = "Completed"
    project.actual_completion_date = timezone.now()
    project.overall_completion_pct = 100
    project.save(
        update_fields=["status", "actual_completion_date", "overall_completion_pct", "updated_at"]
    )

    record_audit(
        client=project.client_id,
        actor=user,
        action="PROJECT_COMPLETED",
        entity_type="PmsProject",
        entity_id=project.id,
        entity_label=project.code,
        description=f"Project {project.code} completed",
    )
    return project


# ---------------------------------------------------------------------------
# Dashboards (api.md §10.8)
# ---------------------------------------------------------------------------
def dashboard_kpis(client_id):
    """The seven KPI tiles -- ``computeDashboardMetrics`` moved server-side."""
    now = timezone.now()
    projects = Project.objects.filter(client_id=client_id, deleted_at__isnull=True)

    total = projects.count()
    in_progress = projects.filter(status="In Progress").count()
    completed = projects.filter(status="Completed").count()
    delayed = projects.filter(status="Delayed").count()
    at_risk = projects.filter(status="At Risk").count()
    on_hold = projects.filter(status="On Hold").count()

    overdue = projects.filter(
        expected_completion_date__lt=now, actual_completion_date__isnull=True
    ).exclude(status="Completed").count()

    average = projects.exclude(status="Draft").aggregate(
        value=Avg("overall_completion_pct")
    )["value"]

    return {
        "totalProjects": total,
        "inProgress": in_progress,
        "completed": completed,
        "delayed": delayed,
        "atRisk": at_risk,
        "onHold": on_hold,
        "overdue": overdue,
        "averageCompletionPct": int(round(average or 0)),
    }


def pipeline_by_department(client_id):
    rows = (
        ProjectStage.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=CLOSED_STAGE_STATUSES)
        .values("department__id", "department__name", "department__color")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [
        {
            "departmentId": str(row["department__id"]) if row["department__id"] else None,
            "department": row["department__name"] or "Unassigned",
            "color": row["department__color"],
            "count": row["count"],
        }
        for row in rows
    ]


def department_workload(client_id):
    """Open stages per department vs ``departmentCapacity`` (api.md §10.1)."""
    settings = get_settings(client_id)
    capacity_map = settings.department_capacity or {}
    default_capacity = settings.default_department_capacity or 20

    rows = (
        ProjectStage.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=CLOSED_STAGE_STATUSES)
        .values("department__id", "department__name", "department__capacity")
        .annotate(count=Count("id"))
    )

    results = []
    for row in rows:
        name = row["department__name"] or "Unassigned"
        capacity = row["department__capacity"] or capacity_map.get(name) or default_capacity
        load = row["count"]
        results.append(
            {
                "departmentId": str(row["department__id"]) if row["department__id"] else None,
                "department": name,
                "load": load,
                "capacity": capacity,
                "utilisationPct": int(round(load / capacity * 100)) if capacity else 0,
            }
        )
    return sorted(results, key=lambda row: -row["utilisationPct"])


def upcoming_deadlines(client_id, days=7):
    now = timezone.now()
    try:
        days = int(days or 7)
    except (TypeError, ValueError):
        from apps.core.exceptions import ValidationFailed

        raise ValidationFailed(
            "Days must be a number.",
            field_errors={"days": ["Expected a number of days."]},
        )
    horizon = now + timedelta(days=days)
    stages = (
        ProjectStage.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            expected_completion_datetime__gte=now,
            expected_completion_datetime__lte=horizon,
        )
        .exclude(status__in=CLOSED_STAGE_STATUSES)
        .select_related("project", "department", "assigned_user")
        .order_by("expected_completion_datetime")
    )
    return [
        {
            "stageId": str(stage.id),
            "stageName": stage.name,
            "projectId": str(stage.project_id),
            "projectCode": stage.project.code,
            "customerName": stage.project.customer_name,
            "department": stage.department.name if stage.department_id else None,
            "assignee": stage.assigned_user.name if stage.assigned_user_id else None,
            "dueAt": stage.expected_completion_datetime,
            "completionPct": stage.completion_pct,
            "remainingHours": int(
                (stage.expected_completion_datetime - now).total_seconds() // 3600
            ),
        }
        for stage in stages
    ]


def delay_watchlist(client_id):
    """``GET /pms/delays/watchlist/`` -- stages at risk or overdue."""
    settings = get_settings(client_id)
    now = timezone.now()
    stages = (
        ProjectStage.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=CLOSED_STAGE_STATUSES)
        .select_related("project", "department", "assigned_user")
    )

    rows = []
    for stage in stages:
        overdue = is_stage_overdue(stage, now)
        at_risk = is_stage_at_risk(stage, settings.at_risk_threshold_pct, now)
        has_open_delay = Delay.objects.filter(
            stage=stage, resolved_at__isnull=True, deleted_at__isnull=True
        ).exists()
        if not (overdue or at_risk or has_open_delay):
            continue

        rows.append(
            {
                "stageId": str(stage.id),
                "stageName": stage.name,
                "projectId": str(stage.project_id),
                "projectCode": stage.project.code,
                "customerName": stage.project.customer_name,
                "department": stage.department.name if stage.department_id else None,
                "assignee": stage.assigned_user.name if stage.assigned_user_id else None,
                "status": stage.status,
                "completionPct": stage.completion_pct,
                "isOverdue": overdue,
                "isAtRisk": at_risk,
                "hasOpenDelay": has_open_delay,
                "expectedCompletionDateTime": stage.expected_completion_datetime,
                "overdueDays": (
                    (now - stage.expected_completion_datetime).days
                    if overdue and stage.expected_completion_datetime
                    else 0
                ),
            }
        )
    return sorted(rows, key=lambda row: (-row["overdueDays"], row["stageName"]))


def nav_badges(client_id, user):
    """``GET /pms/nav-badges/`` -- my tasks, pending approvals, delays."""
    my_tasks = Task.objects.filter(
        client_id=client_id,
        assigned_user=user,
        deleted_at__isnull=True,
    ).exclude(status="Completed").count()

    pending_approvals = Approval.objects.filter(
        client_id=client_id, status="Pending", deleted_at__isnull=True
    ).count()

    open_delays = Delay.objects.filter(
        client_id=client_id, resolved_at__isnull=True, deleted_at__isnull=True
    ).count()

    overdue_projects = Project.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        expected_completion_date__lt=timezone.now(),
        actual_completion_date__isnull=True,
    ).exclude(status="Completed").count()

    return {
        "myTasks": my_tasks,
        "pendingApprovals": pending_approvals,
        "openDelays": open_delays,
        "overdueProjects": overdue_projects,
    }


# ---------------------------------------------------------------------------
# Reports (api.md §10.8)
# ---------------------------------------------------------------------------
def report_on_time_velocity(client_id, date_from=None, date_to=None):
    """Planned vs actual completion over time."""
    queryset = Project.objects.filter(
        client_id=client_id, deleted_at__isnull=True, actual_completion_date__isnull=False
    )
    if date_from:
        queryset = queryset.filter(actual_completion_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(actual_completion_date__lte=date_to)

    rows = []
    on_time = late = 0
    for project in queryset.order_by("actual_completion_date"):
        planned = project.expected_completion_date
        actual = project.actual_completion_date
        variance_days = (actual - planned).days if planned and actual else None
        if variance_days is not None:
            if variance_days <= 0:
                on_time += 1
            else:
                late += 1
        rows.append(
            {
                "projectId": str(project.id),
                "projectCode": project.code,
                "customerName": project.customer_name,
                "plannedCompletion": planned,
                "actualCompletion": actual,
                "varianceDays": variance_days,
                "onTime": variance_days is not None and variance_days <= 0,
            }
        )
    total = on_time + late
    return {
        "rows": rows,
        "summary": {
            "completed": total,
            "onTime": on_time,
            "late": late,
            "onTimeRatePct": int(round(on_time / total * 100)) if total else 0,
        },
    }


def report_stage_bottleneck(client_id):
    """Average dwell time per stage."""
    stages = ProjectStage.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        actual_start_datetime__isnull=False,
        actual_completion_datetime__isnull=False,
    ).values("name", "department__name", "actual_start_datetime", "actual_completion_datetime")

    buckets = {}
    for stage in stages:
        key = stage["name"]
        hours = (
            stage["actual_completion_datetime"] - stage["actual_start_datetime"]
        ).total_seconds() / 3600
        bucket = buckets.setdefault(
            key, {"stage": key, "department": stage["department__name"], "count": 0, "hours": 0.0}
        )
        bucket["count"] += 1
        bucket["hours"] += hours

    rows = [
        {
            "stage": bucket["stage"],
            "department": bucket["department"],
            "completedCount": bucket["count"],
            "averageDwellHours": round(bucket["hours"] / bucket["count"], 2),
            "averageDwellDays": round(bucket["hours"] / bucket["count"] / 24, 2),
        }
        for bucket in buckets.values()
        if bucket["count"]
    ]
    return sorted(rows, key=lambda row: -row["averageDwellHours"])


def report_delay_reason_pareto(client_id, date_from=None, date_to=None):
    """Delay counts by category, descending -- why ``category`` is free text
    exposed from settings rather than a hardcoded enum (api.md §10.7)."""
    queryset = Delay.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if date_from:
        queryset = queryset.filter(created_at__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__lte=date_to)

    rows = (
        queryset.values("category")
        .annotate(count=Count("id"), totalDays=Sum("delay_days"))
        .order_by("-count")
    )
    total = sum(row["count"] for row in rows) or 1
    cumulative = 0
    results = []
    for row in rows:
        cumulative += row["count"]
        results.append(
            {
                "category": row["category"] or "Uncategorised",
                "count": row["count"],
                "totalDelayDays": row["totalDays"] or 0,
                "sharePct": round(row["count"] / total * 100, 1),
                "cumulativePct": round(cumulative / total * 100, 1),
            }
        )
    return results


def report_department_efficiency(client_id):
    """Throughput and on-time rate per department."""
    stages = ProjectStage.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).select_related("department")

    buckets = {}
    for stage in stages:
        name = stage.department.name if stage.department_id else "Unassigned"
        bucket = buckets.setdefault(
            name, {"department": name, "total": 0, "completed": 0, "onTime": 0, "overdue": 0}
        )
        bucket["total"] += 1
        if stage.status in CLOSED_STAGE_STATUSES:
            bucket["completed"] += 1
            if (
                stage.actual_completion_datetime
                and stage.expected_completion_datetime
                and stage.actual_completion_datetime <= stage.expected_completion_datetime
            ):
                bucket["onTime"] += 1
        elif is_stage_overdue(stage):
            bucket["overdue"] += 1

    return sorted(
        [
            {
                **bucket,
                "onTimeRatePct": (
                    int(round(bucket["onTime"] / bucket["completed"] * 100))
                    if bucket["completed"]
                    else 0
                ),
            }
            for bucket in buckets.values()
        ],
        key=lambda row: -row["total"],
    )


PMS_REPORTS = {
    "on-time-velocity": report_on_time_velocity,
    "stage-bottleneck": report_stage_bottleneck,
    "delay-reason-pareto": report_delay_reason_pareto,
    "department-efficiency": report_department_efficiency,
}
