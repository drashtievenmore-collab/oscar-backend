"""Approval decisions for inventory (see core.approval_services)."""
from apps.core.approval_services import approval_handler
from apps.inventory.adjustments import DOCUMENT_TYPE, post_adjustment


@approval_handler(DOCUMENT_TYPE)
def on_stock_adjustment(req, decision):
    """Post a held adjustment once approved; a rejected one is simply never posted."""
    if decision != "Approved":
        return
    from apps.inventory.models import LotTaka
    from apps.masters.models import Item, Location

    values = req.new_values or {}
    item = Item.objects.get(client_id=req.client_id, id=values["itemId"])
    location = Location.objects.get(client_id=req.client_id, id=values["locationId"])
    lot = None
    if values.get("lotTakaId"):
        lot = LotTaka.objects.get(client_id=req.client_id, id=values["lotTakaId"])
    movement = post_adjustment(
        client_id=req.client_id,
        item=item,
        location=location,
        lot=lot,
        delta=values["delta"],
        unit_cost=values.get("unitCost"),
        reason=f"{values.get('reason') or ''} (approved: {req.document_number})",
        user=req.requested_by,
    )
    req.new_values = {**values, "movementId": str(movement.id)}
    req.save(update_fields=["new_values", "updated_at"])
