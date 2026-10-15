# Phase 1.3 job work safety: Closed status, outward transport fields,
# server-side JWO documents (replaces the localStorage-only list).

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_initial'),
        ('core', '0001_initial'),
        ('jobwork', '0006_vpiprogressentry_photo_file'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='jobworkorder',
            name='status',
            field=models.TextField(
                choices=[('Draft', 'Draft'), ('In-Process', 'In-Process'), ('Completed', 'Completed'), ('Closed', 'Closed')],
                default='In-Process',
            ),
        ),
        migrations.AddField(
            model_name='jwooutward',
            name='warehouse',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jwooutward',
            name='vehicle_no',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jwooutward',
            name='driver',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jwooutward',
            name='remarks',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='JWODocument',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('type', models.TextField(default='Other')),
                ('no', models.TextField(blank=True, null=True)),
                ('date', models.DateField(blank=True, null=True)),
                ('file_name', models.TextField(default='')),
                ('remarks', models.TextField(blank=True, null=True)),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.client')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('deleted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('file', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='core.file')),
                ('job_work_order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='jobwork.jobworkorder')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'jobwork_documents',
                'ordering': ['-created_at'],
            },
        ),
    ]
