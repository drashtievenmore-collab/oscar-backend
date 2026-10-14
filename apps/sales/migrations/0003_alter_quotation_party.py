from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('masters', '0003_party_vendor_type_alter_party_type'),
        ('sales', '0002_remove_quotation_estimate_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quotation',
            name='party',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='masters.party'),
        ),
    ]