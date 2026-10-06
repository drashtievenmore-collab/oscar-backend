# Seed Fabric-kind Items so the PO "Select fabric..." dropdown has rows.
# Frontend filters GET /inventory/items/ by itemKind == 'Fabric'.
import re
from decimal import Decimal

from django.db import migrations


def slug_code(name, fallback="FABRIC"):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
    return cleaned[:24] or fallback


DEFAULT_FABRICS = [
    "Cotton",
    "Linen",
    "Silk",
    "Wool",
    "Polyester",
    "Nylon",
    "Spandex (Elastane)",
    "Rayon (Viscose)",
    "Denim",
    "Velvet",
    "Chiffon",
    "Georgette",
]


def sku_for(name):
    base = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
    return f"FAB-{base}"[:40]


def seed_fabric_items(apps, schema_editor):
    Client = apps.get_model("accounts", "Client")
    ItemCategory = apps.get_model("masters", "ItemCategory")
    Item = apps.get_model("masters", "Item")
    Location = apps.get_model("masters", "Location")
    for client in Client.objects.all().only("id"):
        category, _ = ItemCategory.objects.get_or_create(
            client_id=client.id,
            code="CAT-FABRIC",
            defaults={
                "name": "Grey Fabric",
                "kind": "stock",
                "default_hsn_code": "5208.52",
                "lead_time_days": 7,
            },
        )
        default_location = (
            Location.objects.filter(
                client_id=client.id, is_active=True, deleted_at__isnull=True
            )
            .values_list("id", flat=True)
            .first()
        )
        for name in DEFAULT_FABRICS:
            Item.objects.get_or_create(
                client_id=client.id,
                sku=sku_for(name),
                defaults={
                    "name": f"{name} Grey Fabric",
                    "category_id": category.id,
                    "item_kind": "Fabric",
                    "uom": "Mtr",
                    "hsn_code": "5208.52",
                    "cost_price": Decimal("0"),
                    "selling_price": Decimal("0"),
                    "reorder_level": Decimal("0"),
                    "fabric_quality": name,
                    "default_location_id": default_location,
                    "lifecycle_status": "Active",
                },
            )


def unseed_fabric_items(apps, schema_editor):
    Item = apps.get_model("masters", "Item")
    Item.objects.filter(sku__in=[sku_for(n) for n in DEFAULT_FABRICS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("masters", "0007_fabric"),
    ]

    operations = [
        migrations.RunPython(seed_fabric_items, unseed_fabric_items),
    ]
