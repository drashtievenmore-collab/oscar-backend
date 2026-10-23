"""Approval decisions for HRMS (see core.approval_services)."""
from apps.core.approval_services import approval_handler, document_uuid
from apps.core.exceptions import Conflict
from apps.core.money import D


@approval_handler("incentive_adjustment")
def on_incentive_adjustment(req, decision):
    """Apply an approved manual incentive amount; a rejected one never applies."""
    from apps.pms.models import IncentiveCalculation
    from apps.hrms.models import SalespersonIncentiveAccrual

    doc_id = document_uuid(req)
    if decision != "Approved" or doc_id is None:
        return
    new_val = D((req.new_values or {}).get("calculatedIncentive") or (req.new_values or {}).get("incentive_amount") or 0)

    # 1. Canonical PMS IncentiveCalculation (P12)
    calc = IncentiveCalculation.objects.select_for_update().filter(
        client_id=req.client_id, id=doc_id
    ).first()
    if calc is not None:
        if calc.status in ("Approved", "Posted", "Paid"):
            raise Conflict(
                f"This incentive is already {calc.status}; the adjustment cannot apply.",
                code="INCENTIVE_LOCKED",
            )
        calc.incentive_amount = new_val
        calc.save(update_fields=["incentive_amount", "updated_at"])
        return

    # 2. Legacy HRMS SalespersonIncentiveAccrual fallback
    accrual = SalespersonIncentiveAccrual.objects.select_for_update().filter(
        client_id=req.client_id, id=doc_id
    ).first()
    if accrual is not None:
        if accrual.status in ("APPROVED", "PAID"):
            raise Conflict(
                f"This incentive is already {accrual.status}; the adjustment cannot apply.",
                code="INCENTIVE_LOCKED",
            )
        accrual.calculated_incentive = new_val
        accrual.save(update_fields=["calculated_incentive", "updated_at"])
