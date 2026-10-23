"""Rate master lookup and purchase order rate approval services (PDF §01, §06, §11, §16)."""
from decimal import Decimal
from django.db.models import Q
from django.utils import timezone

from apps.core.approval_services import get_matching_rule, raise_approval
from apps.core.models import ApprovalRequest
from apps.core.money import D, ZERO, round2
from apps.masters.models import RateMaster


def resolve_rate(
    client_id,
    rate_type="Sales",
    party_id=None,
    item_id=None,
    fabric_quality_id=None,
    design_id=None,
    shade_id=None,
    as_of=None,
):
    """
    Look up the most specific matching RateMaster entry for a given rate_type and criteria.
    Specificity score:
      - Specific party match: +16
      - Specific shade match: +8
      - Specific design match: +4
      - Specific fabric quality match: +2
      - Specific item match: +1
    """
    as_of = as_of or timezone.localdate()
    if isinstance(as_of, str):
        try:
            from datetime import date
            as_of = date.fromisoformat(as_of)
        except Exception:
            as_of = timezone.localdate()

    rate_type_normalized = "Purchase" if str(rate_type).strip().lower() == "purchase" else "Sales"

    qs = RateMaster.objects.filter(
        client_id=client_id,
        rate_type=rate_type_normalized,
        is_active=True,
        valid_from__lte=as_of,
    ).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=as_of)
    )

    # Party filter: match specific party OR general (no party)
    if party_id:
        qs = qs.filter(Q(party_id=party_id) | Q(party__isnull=True))
    else:
        qs = qs.filter(party__isnull=True)

    # Item filter: match specific item OR general
    if item_id:
        qs = qs.filter(Q(item_id=item_id) | Q(item__isnull=True))
    else:
        qs = qs.filter(item__isnull=True)

    # Fabric quality filter: match specific quality OR general
    if fabric_quality_id:
        qs = qs.filter(Q(fabric_quality_id=fabric_quality_id) | Q(fabric_quality__isnull=True))
    else:
        qs = qs.filter(fabric_quality__isnull=True)

    # Design filter: match specific design OR general
    if design_id:
        qs = qs.filter(Q(design_id=design_id) | Q(design__isnull=True))
    else:
        qs = qs.filter(design__isnull=True)

    # Shade filter: match specific shade OR general
    if shade_id:
        qs = qs.filter(Q(shade_id=shade_id) | Q(shade__isnull=True))
    else:
        qs = qs.filter(shade__isnull=True)

    candidates = list(qs)
    if not candidates:
        return None

    def score_candidate(rm):
        score = 0
        if rm.party_id and str(rm.party_id) == str(party_id):
            score += 16
        if rm.shade_id and str(rm.shade_id) == str(shade_id):
            score += 8
        if rm.design_id and str(rm.design_id) == str(design_id):
            score += 4
        if rm.fabric_quality_id and str(rm.fabric_quality_id) == str(fabric_quality_id):
            score += 2
        if rm.item_id and str(rm.item_id) == str(item_id):
            score += 1
        return score

    # Sort candidates by score desc, valid_from desc, created_at desc
    candidates.sort(key=lambda rm: (score_candidate(rm), rm.valid_from, rm.created_at), reverse=True)
    return candidates[0]


def evaluate_po_rates(po):
    """Evaluate Purchase Order lines against standard purchase RateMaster entries."""
    lines_info = []
    max_variance_pct = ZERO
    needs_approval = False
    reasons = []
    highest_rule = None

    for line in po.line_items.filter(deleted_at__isnull=True).order_by("line_no"):
        std = resolve_rate(
            client_id=po.client_id,
            rate_type="Purchase",
            party_id=po.party_id,
            item_id=line.item_id,
            fabric_quality_id=getattr(line, "fabric_quality_id", None),
            design_id=getattr(line, "design_id", None),
            shade_id=getattr(line, "shade_id", None),
            as_of=po.doc_date,
        )
        line_rate = D(line.rate or 0)
        std_rate = D(std.rate if std else 0)
        variance_amount = line_rate - std_rate if (std and std_rate > ZERO) else ZERO
        variance_pct = round2((variance_amount / std_rate) * 100) if (std and std_rate > ZERO and variance_amount > ZERO) else ZERO

        rule = None
        tolerance_pct = ZERO
        line_needs_approval = False

        if std and std_rate > ZERO and variance_amount > ZERO:
            rule = get_matching_rule(
                po.client_id,
                "purchase_order_rate",
                variance_pct=variance_pct,
                variance_amount=variance_amount,
            )
            tolerance_pct = D(rule.threshold_value if rule else 0)
            if variance_pct > tolerance_pct:
                line_needs_approval = True
                needs_approval = True
                highest_rule = rule or highest_rule
                reasons.append(
                    f"Line {line.line_no} ({line.item_name or 'Fabric'}): Rate {round2(line_rate)} exceeds standard purchase rate {round2(std_rate)} by {round2(variance_pct)}% (tolerance: {round2(tolerance_pct)}%)."
                )

        lines_info.append({
            "lineId": str(line.id),
            "lineNo": line.line_no,
            "itemId": str(line.item_id) if line.item_id else None,
            "itemName": line.item_name,
            "poRate": str(round2(line_rate)),
            "standardRate": str(round2(std_rate)),
            "varianceAmount": str(round2(variance_amount)),
            "variancePct": str(round2(variance_pct)),
            "tolerancePct": str(round2(tolerance_pct)),
            "needsApproval": line_needs_approval,
            "rateMasterId": str(std.id) if std else None,
        })
        if variance_pct > max_variance_pct:
            max_variance_pct = variance_pct

    approver_role_code = (
        highest_rule.approver_role_code
        if highest_rule and highest_rule.approver_role_code
        else "PURCHASE_MGR"
    )

    return {
        "needsApproval": needs_approval,
        "reasons": reasons,
        "maxVariancePct": str(round2(max_variance_pct)),
        "approverRoleCode": approver_role_code if needs_approval else None,
        "lines": lines_info,
    }


def check_po_rate_tolerance(po, user=None):
    """
    Check if PO rates exceed tolerance. If so, create or refresh an ApprovalRequest
    and move PO to 'Pending Approval'. If within tolerance, cancel any previous request.
    """
    evaluation = evaluate_po_rates(po)
    if not evaluation["needsApproval"]:
        # Cancel any previous pending request for rate change
        ApprovalRequest.objects.filter(
            client_id=po.client_id,
            document_type="purchase_order_rate",
            document_id=str(po.id),
            status="Pending",
        ).update(status="Cancelled")
        return False, None, []

    approver_role = evaluation["approverRoleCode"] or "PURCHASE_MGR"
    reason = f"Purchase Order {po.po_number}: " + "; ".join(evaluation["reasons"])
    req = raise_approval(
        client_id=po.client_id,
        document_type="purchase_order_rate",
        document_id=po.id,
        document_number=po.po_number,
        requested_by=user or getattr(po, "created_by", None),
        reason=reason,
        approver_role_code=approver_role,
        allowed_role_codes=[approver_role, "OWNER", "ADMIN"],
        variance_pct=D(evaluation["maxVariancePct"]),
        new_values={
            "documentKind": "Purchase Order",
            "poNumber": po.po_number,
            "approverRoleCode": approver_role,
            "maxVariancePct": evaluation["maxVariancePct"],
            "lines": evaluation["lines"],
        },
    )

    if po.status != "Pending Approval":
        po.status = "Pending Approval"
        po.save(update_fields=["status", "updated_at"])

    return True, req, evaluation["reasons"]
