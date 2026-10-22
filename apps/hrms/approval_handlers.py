"""Approval decisions for HRMS (see core.approval_services)."""
from apps.core.approval_services import approval_handler, document_uuid
from apps.core.exceptions import Conflict
from apps.core.money import D


@approval_handler("incentive_adjustment")
def on_incentive_adjustment(req, decision):
    """Apply an approved manual incentive amount; a rejected one never applies."""
    from apps.hrms.models import SalespersonIncentiveAccrual

    doc_id = document_uuid(req)
    if decision != "Approved" or doc_id is None:
        return
    accrual = SalespersonIncentiveAccrual.objects.select_for_update().filter(
        client_id=req.client_id, id=doc_id
    ).first()
    if accrual is None:
        return
    if accrual.status in ("APPROVED", "PAID"):
        raise Conflict(
            f"This incentive is already {accrual.status}; the adjustment cannot apply.",
            code="INCENTIVE_LOCKED",
        )
    accrual.calculated_incentive = D((req.new_values or {})["calculatedIncentive"])
    accrual.save(update_fields=["calculated_incentive", "updated_at"])
