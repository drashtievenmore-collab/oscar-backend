# Textile units of measurement. Meter is primary; Taka and Kg are tracked
# secondary units (er-diagram.md, inventory lot and taka). Codes match the
# strings the frontend already stores on items and lead requirements.
from django.db import migrations

DEFAULT_UNITS = [
    ("Meter", "Meter"),
    ("Kg", "Kilogram"),
    ("Taka", "Taka"),
    ("Roll", "Roll"),
]

#: A tenant seeded by ``seed_demo`` already has metre as ``Mtr``.
ALIASES = {"Meter": ["Meter", "Mtr", "Metre"]}


def seed_units(apps, schema_editor):
    Client = apps.get_model("accounts", "Client")
    Unit = apps.get_model("masters", "Unit")
    for client in Client.objects.all().only("id"):
        for code, label in DEFAULT_UNITS:
            codes = ALIASES.get(code, [code])
            if not Unit.objects.filter(
                client_id=client.id, code__in=codes, deleted_at__isnull=True
            ).exists():
                Unit.objects.create(client_id=client.id, code=code, label=label)


class Migration(migrations.Migration):

    dependencies = [
        ("masters", "0009_textile_masters"),
    ]

    operations = [
        migrations.RunPython(seed_units, migrations.RunPython.noop),
    ]
