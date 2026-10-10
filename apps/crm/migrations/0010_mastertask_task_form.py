from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0009_alter_task_next_action_delete_leadcall"),
    ]

    operations = [
        migrations.AddField(
            model_name="mastertask",
            name="task_form_id",
            field=models.TextField(blank=True, null=True),
        ),
    ]
