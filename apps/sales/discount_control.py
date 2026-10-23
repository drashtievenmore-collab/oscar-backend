"""Discount authority and margin control on sales documents (PDF §11, §12, §16).

Who may give how much discount lives in ``core.DiscountPolicy``. Every gate
(sales order confirm, quotation send/accept/convert, invoice finalize) calls
``gate_document``: it works out the highest approver any line needs, and when
that is above the user's own authority and no earlier approval covers it, it
opens an approval request addressed to that approver.
"""
from decimal import Decimal

from apps.core.approval_services import raise_approval
from apps.core.models import ApprovalRequest, DiscountPolicy
from apps.core.money import ZERO, D, round2

LEVELS = ("none", "salesperson", "manager", "owner")
RANK = {name: rank for rank, name in enumerate(LEVELS)}

#: Request types this module raises: discount alone, or a line below cost/margin.
DISCOUNT_TYPE = "discount_limit"
MARGIN_TYPE = "below_margin_sale"
APPROVAL_TYPES = (DISCOUNT_TYPE, MARGIN_TYPE)

#: Older settings screens saved lower-case role names.
ROLE_ALIASES = {"sales_manager": "SALES_MGR", "owner": "OWNER", "manager": "SALES_MGR"}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def get_policy(client_id):
    policy, _ = DiscountPolicy.objects.get_or_create(client_id=client_id)
    return policy


def normalise_role_code(code):
    code = (code or "").strip()
    return ROLE_ALIASES.get(code.lower(), code)


def party_limit(policy, party_id):
    """The salesperson limit for this party (its override, else the default)."""
    for row in policy.party_overrides or []:
        if str(row.get("partyId")) == str(party_id) and row.get("maxPct") not in (None, ""):
            return D(str(row["maxPct"]))
    return D(policy.salesperson_max_pct)


def user_level(user, policy):
    if user is None:
        return "salesperson"
    if getattr(user, "is_superuser", False):
        return "owner"
    code = getattr(getattr(user, "role", None), "code", None)
    if code and code == policy.owner_role_code:
        return "owner"
    if code and code == policy.manager_role_code:
        return "manager"
    return "salesperson"


def level_limit(level, policy, party_id=None):
    """The highest discount % this level may give without asking anyone."""
    if level == "owner":
        return Decimal("100")
    if level == "manager":
        return D(policy.manager_max_pct)
    return party_limit(policy, party_id)


def approver_role(level, policy):
    return policy.owner_role_code if level == "owner" else policy.manager_role_code


def serialize_policy(policy, user=None):
    level = user_level(user, policy)
    return {
        "autoAllowPct": str(round2(policy.auto_allow_pct)),
        "salespersonMaxPct": str(round2(policy.salesperson_max_pct)),
        "managerMaxPct": str(round2(policy.manager_max_pct)),
        "minMarginPct": str(round2(policy.min_margin_pct)),
        "managerRoleCode": policy.manager_role_code,
        "ownerRoleCode": policy.owner_role_code,
        "partyOverrides": policy.party_overrides or [],
        # What the current user may give on their own.
        "myLevel": level,
        "myMaxDiscountPct": str(round2(level_limit(level, policy))),
        # The single-threshold shape the older settings screen reads.
        "max_discount_pct": str(round2(policy.salesperson_max_pct)),
        "approver_role_code": policy.manager_role_code,
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _required_for_pct(pct, policy, party_id):
    if pct <= D(policy.auto_allow_pct):
        return "none"
    if pct <= party_limit(policy, party_id):
        return "salesperson"
    if pct <= D(policy.manager_max_pct):
        return "manager"
    return "owner"


def _max_level(*levels):
    return max(levels, key=lambda name: RANK[name])


def evaluate_document(document, user=None, policy=None):
    """The approval this document needs, line by line. Pure: no writes."""
    policy = policy or get_policy(document.client_id)
    party_id = document.party_id
    rows = []
    required = "none"
    max_pct = ZERO
    any_below_cost = any_below_margin = False

    for line in document.line_items.filter(deleted_at__isnull=True).order_by("line_no"):
        pct = round2(D(line.discount_pct or 0))
        level = _required_for_pct(pct, policy, party_id)
        reasons = []
        if level in ("manager", "owner"):
            reasons.append(f"Discount {pct}% is above the salesperson limit.")
        if line.below_cost:
            level = "owner"
            reasons.append(f"Rate {round2(line.rate)} is below landed cost {round2(line.landed_cost)}.")
            any_below_cost = True
        elif line.below_margin:
            level = "owner"
            reasons.append(f"Margin {round2(line.margin_pct)}% is below the minimum {round2(policy.min_margin_pct)}%.")
            any_below_margin = True
        max_pct = max(max_pct, pct)
        required = _max_level(required, level)
        rows.append({
            "lineId": str(line.id),
            "lineNo": line.line_no,
            "itemName": line.item_name,
            "discountPct": str(pct),
            "belowCost": bool(line.below_cost),
            "belowMargin": bool(line.below_margin),
            "requiredLevel": level,
            "approverRoleCode": approver_role(level, policy) if RANK[level] >= RANK["manager"] else None,
            "reasons": reasons,
        })

    # A header-level discount (discount override) counts too.
    subtotal = D(document.subtotal or 0)
    doc_pct = round2(D(document.total_discount or 0) / subtotal * 100) if subtotal > ZERO else ZERO
    doc_level = _required_for_pct(doc_pct, policy, party_id)
    required = _max_level(required, doc_level)
    max_pct = max(max_pct, doc_pct)

    mine = user_level(user, policy)
    needs = RANK[required] > RANK[mine] and RANK[required] >= RANK["manager"]
    reasons = [reason for row in rows for reason in row["reasons"]]
    if RANK[doc_level] >= RANK["manager"]:
        reasons.append(f"Overall discount {doc_pct}% is above the salesperson limit.")
    return {
        "requiredLevel": required,
        "approverRoleCode": approver_role(required, policy) if needs else None,
        "userLevel": mine,
        "needsApproval": needs,
        "maxDiscountPct": str(max_pct),
        "documentDiscountPct": str(doc_pct),
        "belowCost": any_below_cost,
        "belowMargin": any_below_margin,
        "lines": rows,
        "reasons": reasons,
    }


def _related_document_ids(document):
    """The document itself plus the documents it was raised from, so an
    approval given on a quotation carries to its order and invoice."""
    ids = [document.id]
    for attr in ("quotation_id", "sales_order_id"):
        value = getattr(document, attr, None)
        if value:
            ids.append(value)
    order = getattr(document, "sales_order", None) if getattr(document, "sales_order_id", None) else None
    if order is not None and order.quotation_id:
        ids.append(order.quotation_id)
    challan = getattr(document, "delivery_challan", None) if getattr(document, "delivery_challan_id", None) else None
    if challan is not None and challan.sales_order_id:
        ids.append(challan.sales_order_id)
    return [str(i) for i in ids]


def _covers(req, evaluation):
    values = req.new_values or {}
    approved_pct = values.get("maxDiscountPct")
    if D(str(approved_pct or 0)) < D(evaluation["maxDiscountPct"]):
        return False
    if evaluation["belowCost"] and not values.get("belowCost"):
        return False
    if evaluation["belowMargin"] and not (values.get("belowMargin") or values.get("belowCost")):
        return False
    level = values.get("requiredLevel")
    if level and RANK.get(level, 0) < RANK[evaluation["requiredLevel"]]:
        return False
    return True


def covering_approval(document, evaluation):
    approved = ApprovalRequest.objects.filter(
        client_id=document.client_id,
        document_type__in=APPROVAL_TYPES,
        document_id__in=_related_document_ids(document),
        status="Approved",
        # Only requests the gate itself raised; a hand-made request cannot
        # vouch for a discount.
        new_values__system=True,
    ).order_by("-decided_at")
    for req in approved:
        if _covers(req, evaluation):
            return req
    return None


def _within(evaluation, source_evaluation):
    return (
        D(evaluation["maxDiscountPct"]) <= D(source_evaluation["maxDiscountPct"])
        and (not evaluation["belowCost"] or source_evaluation["belowCost"])
        and (not evaluation["belowMargin"] or source_evaluation["belowMargin"] or source_evaluation["belowCost"])
    )


def covered_by_committed_order(document, evaluation, policy):
    """An invoice raised from a confirmed order went through the gate when the
    order was confirmed; it needs nothing new unless it gives more."""
    order_id = getattr(document, "sales_order_id", None)
    if not order_id:
        return False
    from apps.sales.models import SalesOrder

    order = SalesOrder.objects.filter(pk=order_id, deleted_at__isnull=True).first()
    if order is None or order.stage in ("Draft", "Pending Approval", "Cancelled"):
        return False
    return _within(evaluation, evaluate_document(order, policy=policy))


def gate_document(document, user, document_kind, number, note=None):
    """Check a document at a commit point.

    Returns ``(evaluation, pending_request)``. ``pending_request`` is None when
    the document may go ahead; otherwise it is the open request the caller
    must wait for (the caller moves the document to "Pending Approval").
    """
    policy = get_policy(document.client_id)
    evaluation = evaluate_document(document, user=user, policy=policy)
    if (
        not evaluation["needsApproval"]
        or covering_approval(document, evaluation)
        or covered_by_committed_order(document, evaluation, policy)
    ):
        return evaluation, None

    level = evaluation["requiredLevel"]
    document_type = MARGIN_TYPE if (evaluation["belowCost"] or evaluation["belowMargin"]) else DISCOUNT_TYPE
    other_type = DISCOUNT_TYPE if document_type == MARGIN_TYPE else MARGIN_TYPE
    # One open request per document: a discount request that turned into a
    # margin request (or back) is replaced, not duplicated.
    ApprovalRequest.objects.filter(
        client_id=document.client_id,
        document_type=other_type,
        document_id=str(document.id),
        status="Pending",
    ).update(status="Cancelled")

    reason = "; ".join(evaluation["reasons"]) or "Discount above authority."
    if note:
        reason = f"{reason} Salesperson's reason: {note}"
    else:
        # Keep a reason given earlier when the gate runs again (e.g. on send).
        existing = ApprovalRequest.objects.filter(
            client_id=document.client_id, document_type=document_type,
            document_id=str(document.id), status="Pending",
        ).first()
        note = (existing.new_values or {}).get("requesterNote") if existing else None
        if note:
            reason = f"{reason} Salesperson's reason: {note}"
    req = raise_approval(
        client_id=document.client_id,
        document_type=document_type,
        document_id=document.id,
        document_number=number,
        requested_by=user,
        reason=f"{document_kind} {number}: {reason}",
        approver_role_code=approver_role(level, policy),
        level=RANK[level],
        # The owner may also clear a manager-level request; nobody below
        # the addressed level may.
        allowed_role_codes=(
            [policy.owner_role_code] if level == "owner"
            else [policy.manager_role_code, policy.owner_role_code]
        ),
        new_values={
            "documentKind": document_kind,
            "requiredLevel": level,
            "maxDiscountPct": evaluation["maxDiscountPct"],
            "belowCost": evaluation["belowCost"],
            "belowMargin": evaluation["belowMargin"],
            "lines": evaluation["lines"],
            "requesterNote": note or None,
        },
        variance_pct=D(evaluation["maxDiscountPct"]),
        variance_amount=D(document.total_discount or 0),
        urgency="High" if level == "owner" else "Normal",
    )
    return evaluation, req


def approval_payload(req, evaluation):
    role = (req.new_values or {}).get("approverRoleCode")
    return {
        "approvalRequestId": str(req.id),
        "requiredLevel": evaluation["requiredLevel"],
        "approverRoleCode": role,
        "lines": evaluation["lines"],
        "reasons": evaluation["reasons"],
    }


def approval_message(document_kind, number, evaluation):
    who = "the owner" if evaluation["requiredLevel"] == "owner" else "a sales manager"
    return (
        f"{document_kind} {number} needs approval by {who} before it can go ahead. "
        "The request is in the approvals inbox."
    )
