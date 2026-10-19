import django.db.models.deletion
from django.db import migrations, models


def backfill(apps, schema_editor):
    """Classify existing orders without changing their behaviour.

    - Orders a final job work PO was generated for, or numbered ``JWPO-``,
      are Final Job Work.
    - Orders that started production (a production instruction carries their
      number, or they sit In Production) are Vendor A Production.
    - Everything else stays a grey stock purchase (the field default).
    - The old project link (``reference_number`` = project code) is copied
      into the new ``project`` FK; ``reference_number`` is left as it was.
    """
    PurchaseOrder = apps.get_model("purchase", "PurchaseOrder")
    ProductionInstruction = apps.get_model("pms", "ProductionInstruction")
    Project = apps.get_model("pms", "Project")

    final_po_ids = set(
        ProductionInstruction.objects.exclude(final_po_id=None).values_list("final_po_id", flat=True)
    )
    production_refs = set(
        ProductionInstruction.objects.exclude(order_reference=None)
        .values_list("client_id", "order_reference")
    )

    for po in PurchaseOrder.objects.all().only(
        "id", "client_id", "po_number", "status", "reference_number"
    ):
        purchase_type = None
        if po.id in final_po_ids or (po.po_number or "").startswith("JWPO"):
            purchase_type = "Final Job Work"
        elif po.status == "In Production" or (po.client_id, po.po_number) in production_refs:
            purchase_type = "Vendor A Production"

        project_id = None
        if po.reference_number:
            project_id = (
                Project.objects.filter(
                    client_id=po.client_id, code=po.reference_number, deleted_at__isnull=True
                )
                .values_list("id", flat=True)
                .first()
            )

        updates = {}
        if purchase_type:
            updates["purchase_type"] = purchase_type
        if project_id:
            updates["project_id"] = project_id
        if updates:
            PurchaseOrder.objects.filter(pk=po.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("pms", "0010_project_customer_project_received_meters_and_more"),
        ("purchase", "0008_purchasebill_broker_fk_purchasebill_salesperson_fk_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="purchase_type",
            field=models.TextField(
                choices=[
                    ("Grey Stock Purchase", "Grey Stock Purchase"),
                    ("Vendor A Production", "Vendor A Production"),
                    ("Final Job Work", "Final Job Work"),
                ],
                default="Grey Stock Purchase",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_purchase_orders",
                to="pms.project",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
