# Persist the master-task list icon so the frontend's choice survives refresh.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0003_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='mastertask',
            name='icon',
            field=models.TextField(default='call'),
        ),
    ]
