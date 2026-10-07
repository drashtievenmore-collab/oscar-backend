# Production Completion & Verification fields (PI summary screen).
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pms", "0003_production_monitoring"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="productioninstruction",
            name="pi_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="fabric",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="process_type",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="agreed_job_rate",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="rejected_qty",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="verification_remarks",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="productioninstruction",
            name="completed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="productioninstruction",
            name="status",
            field=models.TextField(
                choices=[
                    ("Draft", "Draft"),
                    ("In Progress", "In Progress"),
                    ("Completed", "Completed"),
                    ("Verified", "Verified"),
                    ("Closed", "Closed"),
                ],
                default="Draft",
            ),
        ),
        migrations.AddConstraint(
            model_name="productioninstruction",
            constraint=models.CheckConstraint(
                condition=models.Q(("agreed_job_rate__gte", 0)),
                name="ck_prod_instructions_rate",
            ),
        ),
        migrations.AddConstraint(
            model_name="productioninstruction",
            constraint=models.CheckConstraint(
                condition=models.Q(("rejected_qty__gte", 0)),
                name="ck_prod_instructions_rejected",
            ),
        ),
    ]
