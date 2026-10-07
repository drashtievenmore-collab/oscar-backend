# Grey fabric monitoring moved from apps.production into PMS.
# Tables keep prod_* names so existing rows stay valid.
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pms", "0002_initial"),
        ("hrms", "0001_initial"),
        ("accounting", "0001_initial"),
        ("accounts", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IncentiveScheme",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("name", models.TextField()),
                ("description", models.TextField(blank=True, null=True)),
                ("rate_pct", models.DecimalField(decimal_places=4, default=0, max_digits=7)),
                ("applies_from", models.DateField(blank=True, null=True)),
                ("applies_to", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "incentive_schemes",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="IncentiveCalculation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("calc_number", models.TextField(blank=True, null=True)),
                ("period_label", models.TextField()),
                ("period_start", models.DateField()),
                ("period_end", models.DateField()),
                ("net_sales", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("returns_total", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("incentive_amount", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("status", models.TextField(choices=[("Calculated", "Calculated"), ("Posted", "Posted"), ("Reversed", "Reversed")], default="Calculated")),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("reversal_reason", models.TextField(blank=True, null=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="incentive_calcs", to="hrms.employee")),
                ("journal_entry", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="accounting.journalentry")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("scheme", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="calculations", to="pms.incentivescheme")),
            ],
            options={
                "db_table": "incentive_calcs",
                "ordering": ["-period_start", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ProductionInstruction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("instruction_number", models.TextField(blank=True, null=True)),
                ("agency_name", models.TextField()),
                ("order_reference", models.TextField()),
                ("order_meter", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("status", models.TextField(choices=[("Draft", "Draft"), ("In Progress", "In Progress"), ("Verified", "Verified"), ("Closed", "Closed")], default="Draft")),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, null=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_instructions", to="hrms.employee")),
                ("supervisor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="supervised_production_instructions", to="hrms.employee")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("verified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "prod_instructions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="DailyProductionEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("entry_date", models.DateField()),
                ("meters", models.DecimalField(decimal_places=4, default=0, max_digits=18)),
                ("remarks", models.TextField(blank=True, null=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("entered_by_employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_entries", to="hrms.employee")),
                ("entered_by_user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("instruction", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="daily_entries", to="pms.productioninstruction")),
            ],
            options={
                "db_table": "prod_daily_entries",
                "ordering": ["entry_date", "-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="incentivescheme",
            constraint=models.UniqueConstraint(condition=models.Q(("deleted_at__isnull", True)), fields=("client", "name"), name="uq_incentive_schemes_name"),
        ),
        migrations.AddIndex(
            model_name="incentivecalculation",
            index=models.Index(fields=["client", "employee", "period_start"], name="ix_incentive_calcs_emp"),
        ),
        migrations.AddConstraint(
            model_name="incentivecalculation",
            constraint=models.UniqueConstraint(condition=models.Q(("calc_number__isnull", False), ("deleted_at__isnull", True)), fields=("client", "calc_number"), name="uq_incentive_calcs_number"),
        ),
        migrations.AddConstraint(
            model_name="incentivecalculation",
            constraint=models.UniqueConstraint(condition=models.Q(("deleted_at__isnull", True)), fields=("client", "scheme", "employee", "period_label"), name="uq_incentive_calc_period"),
        ),
        migrations.AddIndex(
            model_name="productioninstruction",
            index=models.Index(fields=["client", "agency_name"], name="ix_prod_instructions_agency"),
        ),
        migrations.AddIndex(
            model_name="productioninstruction",
            index=models.Index(fields=["client", "employee"], name="ix_prod_instructions_emp"),
        ),
        migrations.AddConstraint(
            model_name="productioninstruction",
            constraint=models.UniqueConstraint(condition=models.Q(("deleted_at__isnull", True), ("instruction_number__isnull", False)), fields=("client", "instruction_number"), name="uq_prod_instructions_number"),
        ),
        migrations.AddConstraint(
            model_name="productioninstruction",
            constraint=models.CheckConstraint(condition=models.Q(("order_meter__gte", 0)), name="ck_prod_instructions_meter"),
        ),
        migrations.AddIndex(
            model_name="dailyproductionentry",
            index=models.Index(fields=["client", "instruction", "entry_date"], name="ix_prod_daily_entries_instr"),
        ),
        migrations.AddIndex(
            model_name="dailyproductionentry",
            index=models.Index(fields=["client", "entered_by_employee", "entry_date"], name="ix_prod_daily_entries_emp"),
        ),
        migrations.AddConstraint(
            model_name="dailyproductionentry",
            constraint=models.CheckConstraint(condition=models.Q(("meters__gt", 0)), name="ck_prod_daily_entries_meters"),
        ),
    ]
