# Convert StageTask.repeats from a boolean flag to an integer count.
#
# The frontend always edited this as "MAX REPEATS" (a count: 1, 14, ...),
# but the column was a BooleanField, so every count was coerced to true and
# the real value was lost. false (create once) becomes 1; true (the user had
# entered some count) becomes 14, the frontend's own default max.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0003_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats DROP DEFAULT; "
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats TYPE integer "
                        "USING CASE WHEN repeats THEN 14 ELSE 1 END; "
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats SET DEFAULT 1;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats DROP DEFAULT; "
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats TYPE boolean "
                        "USING COALESCE(repeats, 1) > 1; "
                        "ALTER TABLE crm_stage_tasks ALTER COLUMN repeats SET DEFAULT false;"
                    ),
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="stagetask",
                    name="repeats",
                    field=models.IntegerField(blank=True, default=1, null=True),
                ),
            ],
        ),
    ]
