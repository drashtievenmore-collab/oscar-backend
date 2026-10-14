from django.db import migrations, models
from django.utils import timezone


def remove_duplicate_stage_tasks(apps, schema_editor):
    StageTask = apps.get_model("crm", "StageTask")
    seen = set()
    # Keep earliest created row per (client, stage, lower(title)).
    for task in StageTask.objects.order_by("created_at", "id").all():
        if getattr(task, "deleted_at", None) is not None:
            continue
        key = (task.client_id, task.stage_id, (task.title or "").strip().lower())
        if key in seen:
            task.deleted_at = timezone.now()
            try:
                task.save(update_fields=["deleted_at", "updated_at"])
            except Exception:
                task.delete()
        else:
            seen.add(key)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0010_mastertask_task_form"),
    ]

    operations = [
        migrations.RunPython(remove_duplicate_stage_tasks, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="stagetask",
            constraint=models.UniqueConstraint(
                condition=models.Q(deleted_at__isnull=True),
                fields=("client", "stage", "title"),
                name="uq_crm_stage_task_title",
            ),
        ),
    ]
