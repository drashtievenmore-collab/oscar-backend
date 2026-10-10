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

#: Deal pipeline stages driven by the quotation lifecycle: a new deal sits
#: in Draft; sending the quotation moves it to Sent; the customer opening
#: the link moves it to Open; approval moves it to Won and rejection (or an
#: explicit mark-lost) moves it to Lost. Won/Lost are terminal — neither
#: overwrites the other, and every move is idempotent.
QUOTATION_DEAL_TRANSITIONS = {
    "Sent": ("Draft",),
    "Open": ("Draft", "Sent"),
    "Won": ("Draft", "Sent", "Open", "Revised", "Declined"),
    "Lost": ("Draft", "Sent", "Open", "Revised", "Declined"),
}


def advance_deal_for_quotation(quotation, stage, *, actor=None, description=None):
    """Move the deal linked to ``quotation`` to ``stage`` (or no-op).

    Resolves the deal through ``quotation.crm_deal`` — quotations created
    from a deal carry the link (``POST /crm/deals/{id}/create-quotation/``
    or ``crmDeal`` on quotation create). Returns the deal, or ``None`` when
    there is no linked deal or no move was needed.
    """
    from .models import Deal, DealActivity

    deal_id = getattr(quotation, "crm_deal_id", None)
    if not deal_id or stage not in QUOTATION_DEAL_TRANSITIONS:
        return None
    with transaction.atomic():
        try:
            deal = Deal.objects.select_for_update().get(pk=deal_id)
        except Deal.DoesNotExist:
            return None
        if deal.stage == stage:
            return deal
        if deal.stage in ("Won", "Lost"):
            return deal
        if deal.stage not in QUOTATION_DEAL_TRANSITIONS[stage]:
            return deal
        previous = deal.stage
        deal.stage = stage
        if stage in ("Won", "Lost") and deal.closed_at is None:
            deal.closed_at = timezone.now()
        deal.save(update_fields=["stage", "closed_at", "updated_at"])
        DealActivity.objects.create(
            deal=deal,
            type="stage_change",
            description=(
                description
                or f"Deal moved from {previous} to {stage} "
                f"by quotation {getattr(quotation, 'quotation_number', '')}."
            ),
            actor=actor if getattr(actor, "is_authenticated", False) else None,
        )
        return deal


NEXT_ACTION_FALLBACKS = {
    "follow-up": {
        "title": "Follow-up",
        "role": "BDE",
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
        roles = user.crm_roles or []
        if not roles and role_code != "PM":
            roles = ["General"]
        for role in roles:
            roster.setdefault(role, []).append(dict(person))
        if role_code == "PM" and "Project Manager" not in (user.crm_roles or []):
            roster.setdefault("Project Manager", []).append(dict(person))
    return roster


def resolve_assignee(client_id, role=None, department=None):
    """Role + department, round-robin on open-task count (db.md §9.3).

    The round-robin keeps one keen team member from collecting every generated
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

    from apps.sales.models import DeliveryChallan, SalesInvoice

    from .models import LeadFile, LeadProduct, LeadSource

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
    open_tasks = counts(Task, extra={"status__in": ["Open", "In Progress", "Waiting"]})
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
        lead.open_tasks_count = open_tasks.get(lead.id, 0)
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

    for name in ("Website", "Referral", "Exhibition", "Walk-in", "Campaign"):
        Source.objects.get_or_create(client=client, name=name)

    return created


# ---------------------------------------------------------------------------
# Lead -> Customer conversion (quotation approval)
# ---------------------------------------------------------------------------
def find_conversion_stage(client_id):
    """The stage a converted lead moves to: won stage, else "Won"/"Converted"."""
    stage = (
        Stage.objects.filter(
            client_id=client_id, is_won=True, deleted_at__isnull=True
        )
        .order_by("sequence")
        .first()
    )
    if stage is not None:
        return stage
    return (
        Stage.objects.filter(
            client_id=client_id,
            name__iexact="Converted",
            deleted_at__isnull=True,
        ).first()
        or Stage.objects.filter(
            client_id=client_id, name__iexact="Won", deleted_at__isnull=True
        ).first()
    )


def find_matching_party(client_id, lead):
    """Link, don't duplicate: existing live Customer/Both with the same name
    (or email) wins over a new row."""
    from apps.masters.models import Party

    party_name = (lead.company or lead.name or "").strip()
    if party_name:
        match = (
            Party.objects.filter(
                client_id=client_id,
                type__in=["Customer", "Both"],
                name__iexact=party_name,
                deleted_at__isnull=True,
            )
            .order_by("created_at")
            .first()
        )
        if match is not None:
            return match, "name"
    email = (lead.email or "").strip()
    if email:
        match = (
            Party.objects.filter(
                client_id=client_id,
                type__in=["Customer", "Both"],
                email__iexact=email,
                deleted_at__isnull=True,
            )
            .order_by("created_at")
            .first()
        )
        if match is not None:
            return match, "email"
    return None, None


@transaction.atomic
def convert_lead_to_customer(lead, *, user=None, source="Quotation Approval", reference=None):
    """Convert one lead into a Customer/Party, transactionally and idempotently.

    - Reuses ``lead.party`` when already linked (no duplicate).
    - Otherwise links a matching live Customer/Both (same name, then email).
    - Otherwise creates the Party, preserving lead name/company/contact,
      phone, email, address, source/owner (as audit + primary contact), and
      links fabric requirements/notes via the lead itself (they stay on the
      lead rows, reachable through ``lead.party``).
    - Stamps converted_at/converted_by/source/reference, moves the lead to
      the Won/Converted stage so it leaves the active pipeline but stays in
      history.
    - Returns ``{"party": party, "created": bool, "lead": lead}``.
    """
    from apps.masters.models import Party, PartyContact

    lead = Lead.objects.select_for_update().get(pk=lead.pk)

    # Idempotency: already converted and linked -> link, create nothing.
    if lead.party_id is not None:
        party = Party.objects.filter(
            pk=lead.party_id, client_id=lead.client_id, deleted_at__isnull=True
        ).first()
        if party is not None:
            if lead.converted_at is None:
                lead.converted_at = timezone.now()
                if getattr(user, "is_authenticated", False):
                    lead.converted_by = user
                lead.conversion_source = lead.conversion_source or source
                if reference and not lead.conversion_reference:
                    lead.conversion_reference = reference
                lead.save(
                    update_fields=[
                        "converted_at", "converted_by", "conversion_source",
                        "conversion_reference", "updated_at",
                    ]
                )
            return {"party": party, "created": False, "lead": lead, "matched_on": "linked"}

    party, matched_on = find_matching_party(lead.client_id, lead)
    created = False
    if party is None:
        party_name = (lead.company or lead.name or "Untitled Customer").strip()
        billing = {
            k: v
            for k, v in {
                "city": lead.city,
                "state": lead.state,
                "country": lead.country,
            }.items()
            if v
        }
        party = Party.objects.create(
            client_id=lead.client_id,
            code=allocate_number(lead.client, "CUST"),
            type="Customer",
            name=party_name,
            phone=lead.phone,
            email=lead.email,
            place_of_supply=lead.state,
            billing_address=billing,
            shipping_address=dict(billing),
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        created = True
        # Keep the person's name when the company differs: the party is the
        # company, the contact is the human who approved the quotation.
        contact_name = (lead.name or "").strip()
        if contact_name and contact_name.lower() != party_name.lower():
            PartyContact.objects.create(
                client_id=lead.client_id,
                party=party,
                name=contact_name,
                role=lead.job_title,
                phone=lead.phone,
                email=lead.email,
                is_primary=True,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )
        record_audit(
            client=lead.client_id,
            actor=user,
            action="create",
            entity_type="Party",
            entity_id=party.id,
            entity_label=party.code,
            description=(
                f"Created from lead {lead.lead_number}"
                + (f" on quotation approval ({reference})" if reference else "")
            ),
        )
    else:
        record_audit(
            client=lead.client_id,
            actor=user,
            action="link",
            entity_type="Party",
            entity_id=party.id,
            entity_label=party.code,
            description=(
                f"Lead {lead.lead_number} linked to existing customer"
                + (f" on quotation approval ({reference})" if reference else "")
            ),
        )

    lead.party = party
    lead.converted_at = lead.converted_at or timezone.now()
    if getattr(user, "is_authenticated", False):
        lead.converted_by = lead.converted_by or user
    lead.conversion_source = lead.conversion_source or source
    if reference and not lead.conversion_reference:
        lead.conversion_reference = reference

    target_stage = find_conversion_stage(lead.client_id)
    previous_stage = lead.stage.name if lead.stage_id else None
    if target_stage is not None and str(target_stage.id) != str(lead.stage_id):
        lead.stage = target_stage
        lead.save(
            update_fields=[
                "party", "stage", "converted_at", "converted_by",
                "conversion_source", "conversion_reference", "updated_at",
            ]
        )
        record_audit(
            client=lead.client_id,
            actor=user,
            action="stage_change",
            entity_type="CrmLead",
            entity_id=lead.id,
            entity_label=lead.lead_number,
            description=f"Converted to customer {party.code}",
            from_value=previous_stage,
            to_value=target_stage.name,
        )
    else:
        lead.save(
            update_fields=[
                "party", "converted_at", "converted_by",
                "conversion_source", "conversion_reference", "updated_at",
            ]
        )

    record_audit(
        client=lead.client_id,
        actor=user,
        action="convert",
        entity_type="CrmLead",
        entity_id=lead.id,
        entity_label=lead.lead_number,
        description=(
            f"Converted to customer {party.code}"
            + (f" via {reference}" if reference else "")
        ),
    )
    return {"party": party, "created": created, "lead": lead, "matched_on": matched_on}
