"""Manual stock adjustments: posting, and the approval for large ones (PDF §16, §17).

An adjustment is not a document of its own; it is a StockMovement of type
ADJUSTMENT plus its journal. One worth more than
``settings.STOCK_ADJUSTMENT_APPROVAL_LIMIT`` is held as an approval request
carrying everything needed to post it, and is posted by the approval handler.
"""
import uuid

from apps.core.approval_services import raise_approval
from apps.core.exceptions import ValidationFailed
from apps.core.money import ZERO, D, round2, round4

DOCUMENT_TYPE = "stock_adjustment"
APPROVER_ROLE_CODES = ["INV_MGR", "OWNER"]


def post_adjustment(*, client_id, item, location, lot, delta, unit_cost, reason, user):
    """Post the movement and its journal. Re-checks that stock stays >= 0,
    since an approved adjustment may be posted long after it was entered."""
    from apps.accounting.services import post_stock_adjustment
    from apps.inventory import services as stock

    delta = round4(D(delta))
    if lot is not None:
        lot.refresh_from_db()
        base = D(lot.available_quantity)
    else:
        base = D(stock.calculate_item_stock(client_id, item, location.id)["onHand"])
    if base + delta < ZERO:
        raise ValidationFailed(
            f"Adjustment of {delta} would now leave negative stock ({base + delta}). Current stock: {base}.",
            field_errors={"quantity": ["Adjustment results in negative stock."]},
        )
    movement = stock.post_movement(
        client_id=client_id,
        item=item,
        location=location,
        type="ADJUSTMENT",
        quantity=delta,
        unit_cost=unit_cost,
        notes=reason,
        user=user,
        lot_taka=lot,
    )
    post_stock_adjustment(movement, user=user)
    return movement


def request_adjustment_approval(*, client_id, item, location, lot, delta, unit_cost, reason, value, limit, user):
    ref = uuid.uuid4()
    return raise_approval(
        client_id=client_id,
        document_type=DOCUMENT_TYPE,
        document_id=ref,
        document_number=f"ADJ {item.sku or item.name}",
        requested_by=user,
        reason=f"Stock adjustment of {round4(D(delta))} {item.uom or ''} of {item.name} worth {value} (limit {round2(limit)}): {reason}",
        approver_role_code=APPROVER_ROLE_CODES[0],
        allowed_role_codes=APPROVER_ROLE_CODES,
        new_values={
            "itemId": str(item.id),
            "locationId": str(location.id),
            "lotTakaId": str(lot.id) if lot is not None else None,
            "delta": str(round4(D(delta))),
            "unitCost": str(D(unit_cost or 0)),
            "reason": reason,
            "adjustmentValue": str(value),
        },
        variance_amount=value,
        urgency="High" if value > limit * 2 else "Normal",
    )
