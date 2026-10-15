"""Phase 1 -- unify Sales Order stages.

The only valid stages are Draft, Confirmed, Partially Dispatched, Fully
Dispatched, Invoiced, Closed, Cancelled. Older code (and the frontend) wrote
``Dispatched`` / ``Delivered`` and a few free-text values. Each legacy row is
re-derived from its line quantities so the stage matches what actually moved.
"""
from decimal import Decimal

from django.db import migrations

VALID_STAGES = {
    "Draft", "Confirmed", "Partially Dispatched", "Fully Dispatched",
    "Invoiced", "Closed", "Cancelled",
}
#: Fallback when an order has no live lines to derive from.
LEGACY_FALLBACK = {
    "Dispatched": "Partially Dispatched",
    "Partially Delivered": "Partially Dispatched",
    "Delivered": "Fully Dispatched",
    "Shipped": "Fully Dispatched",
    "Completed": "Closed",
    "Pending": "Draft",
}


def _derive(order, lines):
    if not lines:
        return LEGACY_FALLBACK.get(order.stage, "Confirmed")
    if all(Decimal(l.invoiced_qty or 0) >= Decimal(l.qty or 0) for l in lines):
        return "Invoiced"
    if all(Decimal(l.dispatched_qty or 0) >= Decimal(l.qty or 0) for l in lines):
        return "Fully Dispatched"
    if any(Decimal(l.dispatched_qty or 0) > 0 for l in lines):
        return "Partially Dispatched"
    return LEGACY_FALLBACK.get(order.stage, "Confirmed")


def forwards(apps, schema_editor):
    SalesOrder = apps.get_model("sales", "SalesOrder")
    SalesOrderLine = apps.get_model("sales", "SalesOrderLine")
    for order in SalesOrder.objects.exclude(stage__in=VALID_STAGES).iterator():
        lines = list(
            SalesOrderLine.objects.filter(sales_order_id=order.id, deleted_at__isnull=True)
        )
        order.stage = _derive(order, lines)
        order.save(update_fields=["stage"])


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0005_deliverychallan_broker_deliverychallan_credit_days_and_more"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
