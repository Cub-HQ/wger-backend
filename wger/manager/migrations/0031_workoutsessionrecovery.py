import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('manager', '0030_workoutlog_cardio_metrics'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='WorkoutSessionRecovery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('original_session_id', models.UUIDField()),
                ('routine_id', models.IntegerField(null=True)),
                ('deleted_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('snapshot', models.JSONField()),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={'indexes': [models.Index(fields=['user', 'expires_at'], name='session_recovery_owner_expiry')]},
        ),
    ]
