"""Seed the permanent fabric catalogue for every tenant.

Idempotent: ``get_or_create`` on ``(client, name)`` / ``(client, sku)``.
Run any time -- after migrate, after creating a new tenant, or to repair
a tenant missing rows. Seeds both the ``Fabric`` master (dropdown source)
and the ``Fabric``-kind ``Item`` rows the PO page filters on.
"""
import re
from decimal import Decimal

from django.core.management.base import BaseCommand


def slug_code(name, fallback="FABRIC"):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
    return cleaned[:24] or fallback


def sku_for(name):
    base = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
    return f"FAB-{base}"[:40]


class Command(BaseCommand):
    help = "Seed Cotton/Linen/Silk/... fabrics + Fabric items for all tenants."

    def handle(self, *args, **options):
        from apps.accounts.models import Client
        from apps.masters.models import DEFAULT_FABRICS, Fabric, Item, ItemCategory, Location

        created_fabrics = 0
        created_items = 0
        for client in Client.objects.all().only("id", "name"):
            for name in DEFAULT_FABRICS:
                _, made = Fabric.objects.get_or_create(
                    client=client,
                    name=name,
                    defaults={"code": slug_code(name)},
                )
                if made:
                    created_fabrics += 1

            category, _ = ItemCategory.objects.get_or_create(
                client=client,
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
                    client=client, is_active=True, deleted_at__isnull=True
                )
                .values_list("id", flat=True)
                .first()
            )
            for name in DEFAULT_FABRICS:
                _, made = Item.objects.get_or_create(
                    client=client,
                    sku=sku_for(name),
                    defaults={
                        "name": f"{name} Grey Fabric",
                        "category": category,
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
                if made:
                    created_items += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Fabrics ready ({created_fabrics} masters, {created_items} items)."
            )
        )
