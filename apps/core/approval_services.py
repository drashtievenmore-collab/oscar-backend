"""Approval Engine service logic (PDF §16).

Handles approval rule evaluation, approval request creation, pending state validation,
and authorization decisions (Approve / Reject) with notifications and audit trails.
"""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.models import ApprovalRequest, ApprovalRule, Notification
from apps.core.money import ZERO, round2, D


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

    # In-app notification for approvers
    try:
        from apps.accounts.models import User
        approver_users = User.objects.filter(client_id=client_id, status="Active")
        if rule and rule.approver_role:
            approver_users = approver_users.filter(role=rule.approver_role)
        elif rule and rule.approver_role_code:
            approver_users = approver_users.filter(role__code=rule.approver_role_code)
        else:
            # Default to superusers / owners
            approver_users = approver_users.filter(is_superuser=True)

        for u in approver_users[:10]:
            Notification.objects.create(
                client_id=client_id,
                recipient=u,
                type="approval_required",
                category="system",
                title=f"Approval Required: {document_number or document_type}",
                body=f"{reason} (Requested by {requested_by.name if requested_by else 'System'})",
                entity_type=document_type,
                entity_id=req.id,
            )
    except Exception:
        pass

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


@transaction.atomic
def decide_approval_request(request_id, user, decision, comments=""):
    """Approve or Reject an existing ApprovalRequest."""
    req = ApprovalRequest.objects.select_for_update().get(id=request_id)
    if req.status != "Pending":
        raise ValidationError(f"Approval request is already in '{req.status}' state.")

    if decision not in ("Approved", "Rejected"):
        raise ValidationError("Decision must be either 'Approved' or 'Rejected'.")

    req.status = decision
    req.decided_by = user
    req.decided_at = timezone.now()
    req.comments = comments
    req.save()

    # Record in audit log
    record_audit(
        client=req.client,
        actor=user,
        action=f"APPROVAL_{decision.upper()}",
        entity_type="ApprovalRequest",
        entity_id=req.id,
        entity_label=f"{req.document_type} {req.document_number}",
        comments=comments,
    )

    # Notify requester
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

