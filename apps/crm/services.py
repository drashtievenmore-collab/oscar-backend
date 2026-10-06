"""
CRM automation (api.md §9.3).

``leadStageAutomation.js`` and ``taskCompletionService.js`` move here in full.
The client stops generating follow-up tasks; it reads ``createdTasks`` from the
stage-change response and toasts them (api-integration.md §9.3).

Two things the frontend hardcodes and this module reads from data instead:

  - ``CRM_TEAM_MEMBERS`` -- the role -> user roster, now ``users.crm_roles``,
    exposed at ``GET /crm/team-roster/``
  - the stage list -- now ``crm_stages``, tenant-configurable
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import ValidationFailed
from apps.core.numbering import allocate_number

from .models import AUTOMATION_SOURCE, Lead, Stage, StageTask, Task

#: api.md §9.3 -- the fallback roles and offsets when the target stage has no
#: matching template. The server prefers a template from the lead's current
#: stage; these are what it falls back to.
NEXT_ACTION_FALLBACKS = {
    "call-again": {
        "title": "Follow-up Call",
        "role": "Tele Caller Executive",
        "due_in_days": 1,
    },
    "schedule-demo": {
        "title": "Schedule Demo",
        "role": "Area Sales Manager",
        "due_in_days": 2,
    },
    "send-quotation": {
        "title": "Send Quotation",
        "role": "BDE",
        "due_in_days": 1,
    },
}

#: api.md §9.3 -- priority nudges the computed due date forward.
PRIORITY_ADJUSTMENT = {"Urgent": -2, "High": -1, "Medium": 0, "Low": 1}


# ---------------------------------------------------------------------------
# Assignee resolution
# ---------------------------------------------------------------------------
def team_roster(client_id):
    """``GET /crm/team-roster/`` -- role -> users, from ``users.crm_roles``.

    Each person also carries ``isProjectManager`` (system role code ``PM``),
    which the PMS "Project Manager" dropdowns filter on. Users whose system
    role is ``PM`` always appear, even with empty ``crm_roles``.
    """
    from apps.accounts.models import User

    roster = {}
    users = (
        User.objects.filter(
            client_id=client_id, deleted_at__isnull=True, status="Active"
        )
        .select_related("role")
        .only(
            "id",
            "name",
            "email",
            "department",
            "crm_roles",
            "role",
            "role__code",
        )
    )

    for user in users:
        role_code = user.role.code if user.role_id and user.role else None
        person = {
            "id": str(user.id),
            "name": user.name,
            "email": user.email,
            "department": user.department,
            "isProjectManager": role_code == "PM",
        }
        for role in user.crm_roles or []:
            roster.setdefault(role, []).append(dict(person))
        if role_code == "PM" and "Project Manager" not in (user.crm_roles or []):
            roster.setdefault("Project Manager", []).append(dict(person))
    return roster


def resolve_assignee(client_id, role=None, department=None):
    """Role + department, round-robin on open-task count (db.md §9.3).

    The round-robin keeps one keen tele-caller from collecting every generated
    task; ``ix_crm_tasks_assignee`` is what keeps the count cheap.
    """
    from apps.accounts.models import User

    queryset = User.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="Active"
    )
    if role:
        queryset = queryset.filter(crm_roles__contains=[role])
    if department:
        queryset = queryset.filter(department__iexact=department)

    candidates = list(queryset.only("id"))
    if not candidates and department:
        # Fall back to role alone before giving up -- a mis-typed department
        # should not silently leave the task unassigned.
        candidates = list(
            User.objects.filter(
                client_id=client_id,
                deleted_at__isnull=True,
                status="Active",
                crm_roles__contains=[role] if role else [],
            ).only("id")
        )
    if not candidates:
        return None

    open_counts = dict(
        Task.objects.filter(
            client_id=client_id,
            assignee__in=candidates,
            status__in=["Open", "In Progress", "Waiting"],
            deleted_at__isnull=True,
        )
        .values_list("assignee_id")
        .annotate(count=Count("id"))
    )
    return min(candidates, key=lambda user: open_counts.get(user.id, 0))


def compute_due_date(offset_days, priority="Medium", from_date=None):
    """api.md §9.3 step 3 -- ``dueIn`` offset, adjusted by priority."""
    base = from_date or timezone.localdate()
    days = int(offset_days or 0) + PRIORITY_ADJUSTMENT.get(priority, 0)
    return base + timedelta(days=max(days, 0))


# ---------------------------------------------------------------------------
# Stage automation (api.md §9.3)
# ---------------------------------------------------------------------------
@transaction.atomic
def run_stage_automation(lead, stage, *, user=None):
    """Create the target stage's auto-create tasks and notify the assignees.

    Returns the created tasks so ``PATCH /crm/leads/{id}/`` can return
    ``{ lead, createdTasks: [] }`` for the UI to toast.
    """
    templates = StageTask.objects.filter(
        client_id=lead.client_id, stage=stage, auto_create=True, deleted_at__isnull=True
    ).order_by("sort_order")

    created = []
    for template in templates:
        # ``repeats`` caps how many times the template fires per lead: 1 (or
        # empty) creates the task once, so moving back and forth between
        # stages does not pile up duplicates; N allows up to N copies.
        limit = template.repeats if (template.repeats or 0) > 0 else 1
        if Task.objects.filter(
            client_id=lead.client_id, lead=lead, stage_task=template, deleted_at__isnull=True
        ).count() >= limit:
            continue

        assignee = resolve_assignee(
            lead.client_id, template.assignee_role, template.department
        )
        task = Task.objects.create(
            client_id=lead.client_id,
            task_number=allocate_number(lead.client, "TSK"),
            lead=lead,
            stage_task=template,
            title=template.title,
            description=template.description,
            assignee=assignee,
            assignee_role=template.assignee_role,
            department=template.department,
            due_date=compute_due_date(template.offset_days, template.priority),
            priority=template.priority,
            status="Open",
            source=AUTOMATION_SOURCE,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        created.append(task)

        if assignee is not None:
            notify(
                client=lead.client_id,
                recipients=[assignee],
                type="crm.task_assigned",
                category="crm",
                title=f"New task: {task.title}",
                body=f"{lead.name} moved to {stage.name}.",
                entity_type="CrmTask",
                entity_id=task.id,
                actor=user,
            )
    return created


@transaction.atomic
def change_lead_stage(lead, target_stage, *, user=None):
    """The stage change and its automation, in one transaction."""
    previous = lead.stage
    if previous_id_equals(previous, target_stage):
        return lead, []

    lead.stage = target_stage
    if target_stage.is_won or target_stage.is_lost:
        pass  # closing a lead is a separate, explicit action
    lead.save(update_fields=["stage", "updated_at"])

    record_audit(
        client=lead.client_id,
        actor=user,
        action="stage_change",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description=f"Stage changed to {target_stage.name}",
        from_value=previous.name if previous else None,
        to_value=target_stage.name,
    )

    created = run_stage_automation(lead, target_stage, user=user)
    return lead, created


def previous_id_equals(previous, target):
    return previous is not None and str(previous.id) == str(target.id)


def next_stage_after(client_id, stage):
    return (
        Stage.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            is_active=True,
            sequence__gt=stage.sequence,
        )
        .order_by("sequence")
        .first()
    )


# ---------------------------------------------------------------------------
# Task completion (api.md §9.3)
# ---------------------------------------------------------------------------
@transaction.atomic
def complete_task(task, *, outcome=None, next_action=None, note=None, user=None):
    """``POST /crm/tasks/{id}/complete/``.

    The ``nextAction`` table from api.md §9.3, with the server preferring a
    matching template from the lead's current stage over the hardcoded
    fallbacks.
    """
    if task.status == "Completed":
        from apps.core.exceptions import Conflict, Codes

        raise Conflict("This task is already completed.", code=Codes.ALREADY_DONE)

    task.status = "Completed"
    task.outcome = outcome
    task.next_action = next_action
    task.completion_note = note
    task.completed_at = timezone.now()
    task.completed_by = user if getattr(user, "is_authenticated", False) else None
    task.save()

    follow_up = None
    stage_changed = False
    created_tasks = []

    if next_action == "move-next-stage" and task.lead_id:
        lead = task.lead
        target = next_stage_after(lead.client_id, lead.stage)
        if target is not None:
            # Advancing runs the target stage's own automation, per api.md §9.3.
            lead, created_tasks = change_lead_stage(lead, target, user=user)
            stage_changed = True

    elif next_action in NEXT_ACTION_FALLBACKS and task.lead_id:
        follow_up = _create_follow_up(task, next_action, user=user)
        if follow_up is not None:
            created_tasks = [follow_up]
            task.follow_up_task = follow_up
            task.save(update_fields=["follow_up_task", "updated_at"])

    record_audit(
        client=task.client_id,
        actor=user,
        action="task_completed",
        entity_type="CrmTask",
        entity_id=task.id,
        entity_label=task.title,
        description=f"Task completed ({outcome or 'no outcome'})",
        comments=note,
    )

    return {
        "task": task,
        "followUpTask": follow_up,
        "createdTasks": created_tasks,
        "stageChanged": stage_changed,
    }


def _create_follow_up(task, next_action, *, user=None):
    fallback = NEXT_ACTION_FALLBACKS[next_action]
    lead = task.lead

    # Prefer a matching template from the lead's current stage (api.md §9.3).
    template = (
        StageTask.objects.filter(
            client_id=lead.client_id,
            stage=lead.stage,
            title__iexact=fallback["title"],
            deleted_at__isnull=True,
        ).first()
        or StageTask.objects.filter(
            client_id=lead.client_id,
            stage=lead.stage,
            assignee_role=fallback["role"],
            deleted_at__isnull=True,
        ).first()
    )

    role = template.assignee_role if template else fallback["role"]
    department = template.department if template else None
    offset = template.offset_days if template else fallback["due_in_days"]
    priority = template.priority if template else "Medium"
    title = template.title if template else fallback["title"]

    assignee = resolve_assignee(lead.client_id, role, department)
    follow_up = Task.objects.create(
        client_id=lead.client_id,
        task_number=allocate_number(lead.client, "TSK"),
        lead=lead,
        stage_task=template,
        title=title,
        description=f"Follow-up from: {task.title}",
        assignee=assignee,
        assignee_role=role,
        department=department,
        due_date=compute_due_date(offset, priority),
        priority=priority,
        status="Open",
        source=AUTOMATION_SOURCE,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    if assignee is not None:
        notify(
            client=lead.client_id,
            recipients=[assignee],
            type="crm.task_assigned",
            category="crm",
            title=f"New task: {follow_up.title}",
            body=f"Follow-up for {lead.name}.",
            entity_type="CrmTask",
            entity_id=follow_up.id,
            actor=user,
        )
    return follow_up


# ---------------------------------------------------------------------------
# Lead counters (api.md §9.1)
# ---------------------------------------------------------------------------
def annotate_lead_counters(client_id, leads):
    """The eight counters the list row renders, computed server-side.

    db.md §9.1 and §15 both say: paginate first, then count only the page's
    leads. That is what this does -- one query per counter family over the 25
    ids on screen, not a lateral subquery per row over the whole table.
    """
    leads = list(leads)
    if not leads:
        return leads
    lead_ids = [lead.id for lead in leads]

    from apps.sales.models import DeliveryChallan, Estimate, SalesInvoice

    from .models import LeadCall, LeadFile, LeadProduct, LeadSource

    def counts(model, field="lead_id", extra=None):
        queryset = model.objects.filter(
            client_id=client_id, deleted_at__isnull=True, **{f"{field}__in": lead_ids}
        )
        if extra:
            queryset = queryset.filter(**extra)
        return dict(
            queryset.values_list(field).annotate(count=Count("id"))
        )

    products = counts(LeadProduct)
    sources = counts(LeadSource)
    files = counts(LeadFile)
    calls = counts(LeadCall)
    open_tasks = counts(Task, extra={"status__in": ["Open", "In Progress", "Waiting"]})
    estimates = counts(Estimate, field="crm_lead_id")
    invoices = dict(
        SalesInvoice.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            sales_order__quotation__crm_lead_id__in=lead_ids,
        )
        .values_list("sales_order__quotation__crm_lead_id")
        .annotate(count=Count("id"))
    )
    challans = dict(
        DeliveryChallan.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            quotation__crm_lead_id__in=lead_ids,
        )
        .values_list("quotation__crm_lead_id")
        .annotate(count=Count("id"))
    )

    for lead in leads:
        lead.products_count = products.get(lead.id, 0)
        lead.sources_count = sources.get(lead.id, 0)
        lead.files_count = files.get(lead.id, 0)
        lead.calls_count = calls.get(lead.id, 0)
        lead.open_tasks_count = open_tasks.get(lead.id, 0)
        lead.estimates_count = estimates.get(lead.id, 0)
        lead.sales_invoices_count = invoices.get(lead.id, 0)
        lead.delivery_challans_count = challans.get(lead.id, 0)
    return leads


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
#: api.md §9.3 -- the shipped stage defaults, including the inactive `Future`.
DEFAULT_STAGES = [
    ("New Lead", 1, "#2563eb", "UserPlus", "#eff6ff", "#1d4ed8", False, False, True),
    ("Details Collected", 2, "#7c3aed", "ClipboardList", "#f5f3ff", "#6d28d9", False, False, True),
    ("Quotation Shared", 3, "#0891b2", "FileText", "#ecfeff", "#0e7490", False, False, True),
    ("Demo Pending", 4, "#c2410c", "Clock", "#fff7ed", "#9a3412", False, False, True),
    ("Demo Done", 5, "#4338ca", "CheckSquare", "#eef2ff", "#3730a3", False, False, True),
    ("Negotiation", 6, "#a16207", "Handshake", "#fefce8", "#854d0e", False, False, True),
    ("Won", 7, "#15803d", "Trophy", "#f0fdf4", "#166534", True, False, True),
    ("Lost", 8, "#b91c1c", "XCircle", "#fef2f2", "#991b1b", False, True, True),
    ("Future", 9, "#64748b", "CalendarClock", "#f8fafc", "#475569", False, False, False),
]

#: api.md §9.3 -- the deal catalogue is separate and independent.
DEFAULT_DEAL_STAGES = [
    ("Draft", 1, False, False),
    ("Sent", 2, False, False),
    ("Open", 3, False, False),
    ("Revised", 4, False, False),
    ("Declined", 5, False, True),
    ("Won", 6, True, False),
    ("Lost", 7, False, True),
]


def seed_crm_configuration(client):
    """Create the shipped stage catalogues for a new tenant. Idempotent."""
    from .models import DealStage, Source

    created = []
    for name, sequence, color, icon, bg, fg, is_won, is_lost, is_active in DEFAULT_STAGES:
        stage, was_created = Stage.objects.get_or_create(
            client=client,
            name=name,
            defaults={
                "sequence": sequence,
                "color": color,
                "icon": icon,
                "bg": bg,
                "fg": fg,
                "is_won": is_won,
                "is_lost": is_lost,
                "is_active": is_active,
            },
        )
        if was_created:
            created.append(stage)

    for name, sequence, is_won, is_lost in DEFAULT_DEAL_STAGES:
        DealStage.objects.get_or_create(
            client=client,
            name=name,
            defaults={"sequence": sequence, "is_won": is_won, "is_lost": is_lost},
        )

    for name in ("Website", "Referral", "Cold Call", "Exhibition", "Walk-in", "Campaign"):
        Source.objects.get_or_create(client=client, name=name)

    return created
