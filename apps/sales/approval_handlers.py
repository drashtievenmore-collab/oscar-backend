"""Approval decisions for sales documents (see core.approval_services)."""
from apps.core.approval_services import approval_handler, document_uuid
from apps.sales.discount_control import APPROVAL_TYPES

PENDING = "Pending Approval"


@approval_handler("brokerage_rate_change")
def on_brokerage_rate_change(req, decision):
    """Apply an approved broker / commission rate; a rejected one never applies."""
    from apps.sales import broker_services

    if decision == "Approved" and document_uuid(req) is not None:
        broker_services.apply_rate_change(req)


@approval_handler(*APPROVAL_TYPES)
def on_sales_discount(req, decision):
    """Release the document that was waiting for a discount / margin approval.

    Approved: a sales order is confirmed (and reserves its takas); a quotation
    goes back to Draft, ready to send. Rejected or returned: the document goes
    back to Draft for the salesperson to correct. An invoice never moves on
    its own -- it stays Draft and is finalized again.
    """
    from apps.sales import services
    from apps.sales.models import Quotation, SalesOrder

    kind = (req.new_values or {}).get("documentKind")
    doc_id = document_uuid(req)
    if doc_id is None:
        return
    if kind == "Sales Order":
        order = SalesOrder.objects.select_for_update().filter(
            client_id=req.client_id, id=doc_id, deleted_at__isnull=True
        ).first()
        if order is None or order.stage != PENDING:
            return
        if decision == "Approved":
            order.stage = "Confirmed"
            order.save(update_fields=["stage", "updated_at"])
            services.reserve_order_takas(order)
        else:
            order.stage = "Draft"
            order.save(update_fields=["stage", "updated_at"])
    elif kind == "Quotation":
        quotation = Quotation.objects.filter(
            client_id=req.client_id, id=doc_id, deleted_at__isnull=True
        ).first()
        if quotation is not None and quotation.status == PENDING:
            quotation.status = "Draft"
            quotation.save(update_fields=["status", "updated_at"])
