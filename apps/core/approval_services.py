"""Approval Engine service logic (PDF §16).

Handles approval rule evaluation, approval request creation, pending state validation,
and authorization decisions (Approve / Reject) with notifications and audit trails.
"""
import logging
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import autodiscover_modules

from apps.core.audit import record_audit
from apps.core.models import ApprovalRequest, ApprovalRule, Notification
from apps.core.money import ZERO, round2, D

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision handlers
# ---------------------------------------------------------------------------
#: ``document_type -> fn(request, decision)``. A handler moves the document on
#: once its request is decided. It runs inside the decision's transaction, so
#: a handler that raises undoes the decision too.
APPROVAL_HANDLERS = {}
_handlers_loaded = False


def approval_handler(*document_types):
    """Register the function that applies a decision for these document types.

    Put handlers in ``apps/<app>/approval_handlers.py``; they are discovered
    the first time a request is decided::

        @approval_handler("stock_adjustment")
        def on_stock_adjustment(req, decision):
            ...
    """
    def register(fn):
        for document_type in document_types:
            APPROVAL_HANDLERS[document_type] = fn
        return fn
    return register


def document_uuid(req):
    """The request's document id as a UUID, or None for a free-text id
    (hand-made requests may carry a document number instead)."""
    import uuid

    try:
        return uuid.UUID(str(req.document_id))
    except (TypeError, ValueError):
        return None


def load_approval_handlers():
    global _handlers_loaded
    if not _handlers_loaded:
        autodiscover_modules("approval_handlers")
        _handlers_loaded = True


def system_rule(client_id, document_type, approver_role_code, level=1):
    """The rule a system-raised request points at, so the inbox and
    ``assert_can_decide`` know whose decision it is."""
    rule, _ = ApprovalRule.objects.get_or_create(
        client_id=client_id,
        document_type=document_type,
        approver_role_code=approver_role_code,
        min_level=level,
        defaults={
            "name": f"{document_type.replace('_', ' ').title()} ({approver_role_code})",
            "description": "Created by the system for requests it raises.",
            "condition_type": "always",
        },
    )
    return rule


@transaction.atomic
def raise_approval(
    client_id,
    document_type,
    document_id,
    document_number,
    requested_by,
    reason,
    approver_role_code,
    level=1,
    allowed_role_codes=None,
    new_values=None,
    old_values=None,
    variance_amount=None,
    variance_pct=None,
    urgency="Normal",
):
    """Open (or refresh) the pending request for a document that a business
    rule says needs approval. Unlike ``create_approval_request`` the caller
    has already decided approval is needed and who must give it.

    A second call for the same document refreshes the pending request; if the
    new reason needs a higher approver, the request moves to that approver.

    ``allowed_role_codes`` (default: just ``approver_role_code``) are the only
    roles that may decide it -- ``manage_approvals`` alone is not enough, so a
    manager cannot clear a request that is addressed to the owner.
    """
    rule = system_rule(client_id, document_type, approver_role_code, level)
    existing = ApprovalRequest.objects.select_for_update().filter(
        client_id=client_id,
        document_type=document_type,
        document_id=str(document_id),
        status="Pending",
    ).first()
    values = dict(new_values or {})
    values["approverRoleCode"] = approver_role_code
    values["approverRoleCodes"] = list(allowed_role_codes or [approver_role_code])
    values["level"] = level
    values["system"] = True
    if existing:
        existing.rule = rule
        existing.reason = reason
        existing.new_values = values
        existing.old_values = old_values or existing.old_values
        existing.variance_amount = round2(variance_amount) if variance_amount is not None else None
        existing.variance_pct = round2(variance_pct) if variance_pct is not None else None
        existing.urgency = urgency
        existing.save()
        return existing

    req = ApprovalRequest.objects.create(
        client_id=client_id,
        rule=rule,
        document_type=document_type,
        document_id=str(document_id),
        document_number=str(document_number or ""),
        requested_by=requested_by,
        reason=reason,
        old_values=old_values or {},
        new_values=values,
        variance_amount=round2(variance_amount) if variance_amount is not None else None,
        variance_pct=round2(variance_pct) if variance_pct is not None else None,
        status="Pending",
        urgency=urgency,
    )
    _notify_approvers(req)
    return req


def _notify_approvers(req):
    try:
        from apps.accounts.models import User
        rule = req.rule
        approver_users = User.objects.filter(client_id=req.client_id, status="Active")
        if rule and rule.approver_role_id:
            approver_users = approver_users.filter(role=rule.approver_role)
        elif rule and rule.approver_role_code:
            approver_users = approver_users.filter(role__code=rule.approver_role_code)
        else:
            approver_users = approver_users.filter(is_superuser=True)
        requester = req.requested_by
        for u in approver_users[:10]:
            Notification.objects.create(
                client_id=req.client_id,
                recipient=u,
                type="approval_required",
                category="system",
                title=f"Approval Required: {req.document_number or req.document_type}",
                body=f"{req.reason} (Requested by {requester.name if requester else 'System'})",
                entity_type=req.document_type,
                entity_id=req.id,
            )
    except Exception:
        # The request stands; approvers still see it in the inbox.
        logger.exception("Approval %s: notifying approvers failed", req.id)


def latest_decision(client_id, document_types, document_ids):
    """The newest decided request for any of these documents, or None."""
    return (
        ApprovalRequest.objects.filter(
            client_id=client_id,
            document_type__in=list(document_types),
            document_id__in=[str(d) for d in document_ids if d],
        )
        .exclude(status="Pending")
        .order_by("-decided_at", "-requested_at")
        .first()
    )


def get_matching_rule(client_id, document_type, variance_amount=None, variance_pct=None, value=None):
    """Find the first matching active ApprovalRule for this document type and threshold."""
    rules = ApprovalRule.objects.filter(
        client_id=client_id,
        document_type=document_type,
        is_active=True,
    ).order_by("min_level")

    for rule in rules:
        if rule.condition_type == "always":
            return rule

        threshold = D(rule.threshold_value or 0)
        if rule.condition_type == "threshold_amount":
            amt = D(value if value is not None else (variance_amount or 0))
            if amt >= threshold:
                return rule

        elif rule.condition_type in ("threshold_percent", "variance_percent"):
            pct = D(variance_pct if variance_pct is not None else 0)
            if pct >= threshold:
                return rule

    return None


@transaction.atomic
def create_approval_request(
    client_id,
    document_type,
    document_id,
    document_number,
    requested_by,
    reason,
    old_values=None,
    new_values=None,
    variance_amount=None,
    variance_pct=None,
    urgency="Normal",
    force=False,
):
    """Evaluate rules and generate an ApprovalRequest if authorization is required.

    If `force=True`, creates an approval request regardless of rule thresholds (e.g. manual trigger).
    """
    rule = get_matching_rule(
        client_id=client_id,
        document_type=document_type,
        variance_amount=variance_amount,
        variance_pct=variance_pct,
    )

    if not rule and not force:
        return None

    # Check if a pending request already exists for this exact document & type
    existing = ApprovalRequest.objects.filter(
        client_id=client_id,
        document_type=document_type,
        document_id=str(document_id),
        status="Pending",
    ).first()
    if existing:
        return existing

    req = ApprovalRequest.objects.create(
        client_id=client_id,
        rule=rule,
        document_type=document_type,
        document_id=str(document_id),
        document_number=str(document_number or ""),
        requested_by=requested_by,
        reason=reason,
        old_values=old_values or {},
        new_values=new_values or {},
        variance_amount=round2(variance_amount) if variance_amount is not None else None,
        variance_pct=round2(variance_pct) if variance_pct is not None else None,
        status="Pending",
        urgency=urgency,
    )

    _notify_approvers(req)
    return req


def is_pending_approval(client_id, document_type, document_id):
    """Check whether a document is currently locked in 'Pending Approval' status."""
    return ApprovalRequest.objects.filter(
        client_id=client_id,
        document_type=document_type,
        document_id=str(document_id),
        status="Pending",
    ).exists()


def assert_not_pending_approval(client_id, document_type, document_id, action_description="proceed"):
    """Raise ValidationError if the document is blocked waiting for approval."""
    if is_pending_approval(client_id, document_type, document_id):
        raise ValidationError(
            f"Document {document_id} has a pending approval request. It cannot {action_description} until authorized."
        )


#: Document-specific permissions that already let a role approve that document
#: outside the approval inbox; holding one also allows deciding its request.
DOCUMENT_APPROVE_PERMISSIONS = {
    "purchase_order": "approve_purchase_order",
}


def assert_can_decide(req, user, decision):
    """Only an approver may decide: ``manage_approvals`` (or ``return_approval``
    for a return), the rule's approver role, or the document's own approve
    permission."""
    from apps.core.permissions import has_permission
    from rest_framework.exceptions import PermissionDenied

    if getattr(user, "is_superuser", False):
        return
    # A request the system addressed to specific roles (raise_approval) may
    # only be decided by those roles.
    allowed_codes = (req.new_values or {}).get("approverRoleCodes")
    if allowed_codes:
        if getattr(getattr(user, "role", None), "code", None) in allowed_codes:
            return
        raise PermissionDenied(
            f"This request needs approval by {', '.join(allowed_codes)}.",
            code="manage_approvals",
        )
    if has_permission(user, "manage_approvals"):
        return
    if decision == "Returned" and has_permission(user, "return_approval"):
        return
    role = getattr(user, "role", None)
    rule = req.rule
    if role is not None and rule is not None and (
        (rule.approver_role_id and rule.approver_role_id == role.id)
        or (rule.approver_role_code and rule.approver_role_code == getattr(role, "code", None))
    ):
        return
    doc_perm = DOCUMENT_APPROVE_PERMISSIONS.get(req.document_type)
    if doc_perm and has_permission(user, doc_perm):
        return
    raise PermissionDenied("You are not an approver for this request.", code="manage_approvals")


@transaction.atomic
def decide_approval_request(request_id, user, decision, comments="", client_id=None):
    qs = ApprovalRequest.objects.select_for_update()
    if client_id is not None:
        qs = qs.filter(client_id=client_id)
    req = qs.get(id=request_id)
    assert_can_decide(req, user, decision)
    if req.status != "Pending":
        raise ValidationError(f"Approval request is already in '{req.status}' state.")

    if decision not in ("Approved", "Rejected", "Returned"):
        raise ValidationError("Decision must be either 'Approved', 'Rejected', or 'Returned'.")

    if req.requested_by_id and req.requested_by_id == getattr(user, "id", None) and not getattr(user, "is_superuser", False):
        raise ValidationError("Separation of duties violation: You cannot decide an approval request you initiated.")

    req.status = decision
    req.decided_by = user
    req.decided_at = timezone.now()
    req.comments = comments
    req.save()

    load_approval_handlers()
    handler = APPROVAL_HANDLERS.get(req.document_type)
    if handler is not None:
        handler(req, decision)

    record_audit(
        client=req.client,
        actor=user,
        action=f"APPROVAL_{decision.upper()}",
        entity_type="ApprovalRequest",
        entity_id=req.id,
        entity_label=f"{req.document_type} {req.document_number}",
        comments=comments,
    )

    if req.requested_by:
        Notification.objects.create(
            client_id=req.client_id,
            recipient=req.requested_by,
            type=f"approval_{decision.lower()}",
            category="system",
            title=f"Approval {decision}: {req.document_number or req.document_type}",
            body=f"Your request for {req.document_type} was {decision.lower()} by {user.name if user else 'an approver'}. {comments}",
            entity_type=req.document_type,
            entity_id=req.id,
        )

    return req


def approve_request(req_or_id, approver, comments=""):
    req_id = getattr(req_or_id, "id", req_or_id)
    return decide_approval_request(req_id, approver, "Approved", comments=comments)


def reject_request(req_or_id, approver, comments=""):
    req_id = getattr(req_or_id, "id", req_or_id)
    return decide_approval_request(req_id, approver, "Rejected", comments=comments)


def return_request(req_or_id, approver, comments=""):
    req_id = getattr(req_or_id, "id", req_or_id)
    return decide_approval_request(req_id, approver, "Returned", comments=comments)

