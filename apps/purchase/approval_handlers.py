"""Approval decisions for purchase documents (see core.approval_services)."""
from apps.core.approval_services import approval_handler, document_uuid


@approval_handler("purchase_order")
def on_purchase_order(req, decision):
    from apps.purchase.models import PurchaseOrder

    doc_id = document_uuid(req)
    if doc_id is None:
        return
    po = PurchaseOrder.objects.filter(client_id=req.client_id, id=doc_id).first()
    if po is None or po.status != "Pending Approval":
        return
    po.status = {"Approved": "Approved", "Rejected": "Rejected", "Returned": "Draft"}[decision]
    po.save(update_fields=["status", "updated_at"])


@approval_handler("vendor_bill_mismatch")
def on_vendor_bill_mismatch(req, decision):
    """Record the decision on the bill. The bill itself is approved by
    calling its approve action again, which now finds this approval."""
    from django.utils import timezone

    from apps.purchase.models import VendorBill

    doc_id = document_uuid(req)
    if doc_id is None:
        return
    bill = VendorBill.objects.select_for_update().filter(client_id=req.client_id, id=doc_id).first()
    if bill is None:
        return
    history = list(bill.approval_history or [])
    history.append({
        "at": timezone.now().isoformat(),
        "by": getattr(req.decided_by, "name", None) or str(req.decided_by),
        "action": f"Mismatch {decision.lower()}",
        "remarks": req.comments,
        "approvalRequestId": str(req.id),
    })
    bill.approval_history = history
    bill.save(update_fields=["approval_history", "updated_at"])
