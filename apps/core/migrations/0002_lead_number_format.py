# Lead numbers render as L-001, L-002, ... (prefix L, dash, 3-digit pad).

from django.db import migrations


def forwards(apps, schema_editor):
    NumberSeries = apps.get_model("core", "NumberSeries")
    NumberSeries.objects.filter(series_key="LEAD").update(
        prefix="L", separator="-", pad_width=3, reset_policy="never"
    )


def backwards(apps, schema_editor):
    NumberSeries = apps.get_model("core", "NumberSeries")
    NumberSeries.objects.filter(series_key="LEAD").update(
        prefix="L", separator="", pad_width=8, reset_policy="never"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
