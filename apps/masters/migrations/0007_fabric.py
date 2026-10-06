# Permanent fabric catalogue.
import re
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


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


def seed_fabrics(apps, schema_editor):
    Client = apps.get_model("accounts", "Client")
    Fabric = apps.get_model("masters", "Fabric")
    for client in Client.objects.all().only("id"):
        for name in DEFAULT_FABRICS:
            Fabric.objects.get_or_create(
                client_id=client.id,
                name=name,
                defaults={"code": slug_code(name)},
            )


def unseed_fabrics(apps, schema_editor):
    Fabric = apps.get_model("masters", "Fabric")
    Fabric.objects.filter(name__in=DEFAULT_FABRICS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("masters", "0006_item_fabric_color_item_fabric_design_item_fabric_gsm_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Fabric",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("name", models.TextField()),
                ("code", models.TextField()),
                ("description", models.TextField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "fabrics",
                "ordering": ["name"],
            },
        ),
        migrations.AddConstraint(
            model_name="fabric",
            constraint=models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_fabrics_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="fabric",
            constraint=models.UniqueConstraint(
                fields=["client", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_fabrics_code",
            ),
        ),
        migrations.AddIndex(
            model_name="fabric",
            index=models.Index(fields=["client", "name"], name="ix_fabrics_name"),
        ),
        migrations.RunPython(seed_fabrics, unseed_fabrics),
    ]
