#!/usr/bin/env python3
"""Stage the pinned wger session-recovery overrides without modifying source."""
from pathlib import Path
import hashlib
import sys


MODEL_SOURCE = '''"""Private, transactional, fifteen-day workout recovery snapshots."""
import datetime
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound

from .session import WorkoutSession
from .log import WorkoutLog


class RecoveryConflict(APIException):
    status_code = 409
    default_detail = 'This workout cannot be recovered without changing other history.'
    default_code = 'recovery_conflict'


class WorkoutSessionRecovery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    original_session_id = models.UUIDField()
    routine_id = models.IntegerField(null=True)
    deleted_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    snapshot = models.JSONField()

    class Meta:
        indexes = [models.Index(fields=['user', 'expires_at'], name='session_recovery_owner_expiry')]


def _snapshot(obj):
    # Model field codecs preserve decimals, UUIDs and timestamp microseconds.
    # All concrete fields are included, including targets and cardio metrics.
    return {
        field.attname: None if getattr(obj, field.attname) is None else field.value_to_string(obj)
        for field in obj._meta.concrete_fields
    }


def _decode(model, data):
    fields = model._meta.concrete_fields
    if not isinstance(data, dict) or set(data) != {field.attname for field in fields}:
        raise RecoveryConflict()
    return {
        field.attname: None if data[field.attname] is None else
        (field.target_field if field.is_relation else field).to_python(data[field.attname])
        for field in fields
    }


def recovery_summary(row):
    return {
        'id': str(row.pk),
        'original_session_id': str(row.original_session_id),
        'routine_id': row.routine_id,
        'deleted_at': row.deleted_at.isoformat(),
        'expires_at': row.expires_at.isoformat(),
        'datetime_start': row.snapshot['session']['datetime_start'],
    }


def _lock_owner(user):
    # Match WorkoutLog.assign_session's owner lock before acquiring session locks.
    get_user_model()._default_manager.select_for_update().get(pk=user.pk)


@transaction.atomic
def archive_session(user, session_id):
    _lock_owner(user)
    session = WorkoutSession.objects.select_for_update().filter(pk=session_id, user=user).first()
    if session is None:
        raise NotFound()
    logs = list(WorkoutLog.objects.select_for_update().filter(session=session).order_by('pk'))
    ids = [log.pk for log in logs]
    if any(log.user_id != user.pk for log in logs):
        raise RecoveryConflict()
    # next_log uses CASCADE upstream. Never delete an unrelated incoming chain.
    if WorkoutLog.objects.filter(next_log_id__in=ids).exclude(pk__in=ids).exists():
        raise RecoveryConflict()
    now = timezone.now()
    row = WorkoutSessionRecovery.objects.create(
        user=user, original_session_id=session.pk, routine_id=session.routine_id,
        deleted_at=now, expires_at=now + datetime.timedelta(days=15),
        snapshot={'version': 1, 'session': _snapshot(session), 'logs': [_snapshot(log) for log in logs]},
    )
    session.delete()
    return row


def _check_references(session_data, logs_data, user, session_id, log_ids):
    references = {}
    owned_references = {}
    for model, records in ((WorkoutSession, [session_data]), (WorkoutLog, logs_data)):
        for field in model._meta.concrete_fields:
            if not field.is_relation:
                continue
            related = field.remote_field.model
            values = {data[field.attname] for data in records if data[field.attname] is not None}
            if related is WorkoutSession:
                values.discard(session_id)
            elif related is WorkoutLog:
                values.difference_update(log_ids)
            if not values:
                continue
            references.setdefault(related, set()).update(values)
            # Exercise and unit references are public; history references are owned.
            if field.name in ('routine', 'day', 'slot_entry', 'next_log', 'session'):
                owned_references.setdefault(related, set()).update(values)
    for related, ids in references.items():
        objects = related._default_manager.select_for_update().filter(pk__in=ids).order_by('pk')
        found = set()
        owned_ids = owned_references.get(related, set())
        for obj in objects:
            found.add(obj.pk)
            if obj.pk in owned_ids and obj.get_owner_object().user_id != user.pk:
                raise RecoveryConflict()
        if found != ids:
            raise RecoveryConflict()


def _invalidate_restored_caches(session, logs):
    # Bulk restore deliberately bypasses save hooks, but not their cache effects.
    # Run only after commit, otherwise readers could repopulate deleted history.
    from ..signals import handle_workout_log_change, handle_workout_session_change

    handle_workout_session_change(WorkoutSession, session)
    routines = {session.routine_id}
    for log in logs:
        if log.routine_id is not None and log.routine_id not in routines:
            handle_workout_log_change(WorkoutLog, log)
            routines.add(log.routine_id)


def restore_session(user, recovery_id):
    try:
        with transaction.atomic():
            _lock_owner(user)
            row = WorkoutSessionRecovery.objects.select_for_update().filter(pk=recovery_id, user=user).first()
            if row is None or row.expires_at <= timezone.now():
                raise NotFound()
            snapshot = row.snapshot
            if snapshot.get('version') != 1:
                raise RecoveryConflict()
            session_data = _decode(WorkoutSession, snapshot['session'])
            logs_data = [_decode(WorkoutLog, data) for data in snapshot['logs']]
            session_id = session_data['id']
            log_ids = {data['id'] for data in logs_data}
            if (session_id != row.original_session_id or session_data['user_id'] != user.pk
                    or len(log_ids) != len(logs_data)
                    or any(data['user_id'] != user.pk or data['session_id'] != session_id for data in logs_data)):
                raise RecoveryConflict()
            if (WorkoutSession.objects.filter(pk=session_id).exists()
                    or WorkoutLog.objects.filter(pk__in=log_ids).exists()):
                raise RecoveryConflict()
            _check_references(session_data, logs_data, user, session_id, log_ids)
            session = WorkoutSession(**session_data)
            # Bypass save hooks that recalculate session bounds or assign sessions.
            WorkoutSession.objects.bulk_create([session])
            logs = [WorkoutLog(**{**data, 'next_log_id': None}) for data in logs_data]
            WorkoutLog.objects.bulk_create(logs)
            linked_logs = []
            for log, data in zip(logs, logs_data):
                if data['next_log_id'] is not None:
                    log.next_log_id = data['next_log_id']
                    linked_logs.append(log)
            WorkoutLog.objects.bulk_update(linked_logs, ['next_log'])
            row.delete()
            transaction.on_commit(lambda: _invalidate_restored_caches(session, logs))
            return session
    except IntegrityError as error:
        # Catch outside atomic so all inserts roll back and the snapshot survives.
        raise RecoveryConflict() from error


def purge_expired_recoveries(now=None, batch_size=500, user=None):
    """One bounded batch; the API does not depend on the scheduler's timing."""
    now = timezone.now() if now is None else now
    batch_size = max(1, min(int(batch_size), 500))
    with transaction.atomic():
        rows = WorkoutSessionRecovery.objects.filter(expires_at__lte=now)
        if user is not None:
            rows = rows.filter(user=user)
        ids = list(rows.select_for_update(skip_locked=True).order_by('expires_at', 'pk')
                   .values_list('pk', flat=True)[:batch_size])
        deleted, _ = WorkoutSessionRecovery.objects.filter(pk__in=ids).delete()
        return deleted
'''

VIEW_IMPORT = '''from wger.manager.models.session_recovery import (
    WorkoutSessionRecovery,
    archive_session,
    purge_expired_recoveries,
    recovery_summary,
    restore_session,
)
from django.core.exceptions import ValidationError as RecoveryValidationError
from django.utils import timezone as recovery_timezone
from rest_framework.exceptions import NotFound as RecoveryNotFound


'''

VIEW_METHODS = '''    def destroy(self, request, *args, **kwargs):
        try:
            row = archive_session(request.user, kwargs['pk'])
        except (RecoveryValidationError, ValueError):
            raise RecoveryNotFound()
        return Response(recovery_summary(row))

    @action(detail=False, methods=['get'], url_path='recoveries')
    def recoveries(self, request):
        purge_expired_recoveries(user=request.user)
        rows = WorkoutSessionRecovery.objects.filter(
            user=request.user, expires_at__gt=recovery_timezone.now()
        ).order_by('-deleted_at', 'pk')
        if 'routine' in request.query_params:
            try:
                routine_id = int(request.query_params['routine'])
            except (TypeError, ValueError):
                raise RecoveryNotFound()
            rows = rows.filter(routine_id=routine_id)
        summaries = rows.values(
            'id', 'original_session_id', 'routine_id', 'deleted_at', 'expires_at',
            'snapshot__session__datetime_start',
        )
        return Response([{
            'id': str(row['id']),
            'original_session_id': str(row['original_session_id']),
            'routine_id': row['routine_id'],
            'deleted_at': row['deleted_at'].isoformat(),
            'expires_at': row['expires_at'].isoformat(),
            'datetime_start': row['snapshot__session__datetime_start'],
        } for row in summaries])

    @action(detail=False, methods=['post'], url_path=r'recoveries/(?P<recovery_id>[^/.]+)/restore')
    def restore_recovery(self, request, recovery_id=None):
        try:
            session = restore_session(request.user, recovery_id)
        except (RecoveryValidationError, ValueError):
            raise RecoveryNotFound()
        return Response(self.get_serializer(session).data)

'''

TASK_SOURCE = '''"""Bounded recovery retention, discovered by Celery on all worker roles."""
from celery import shared_task
from wger.manager.models.session_recovery import purge_expired_recoveries


@shared_task(name='wger.manager.tasks.purge_session_recoveries')
def purge_session_recoveries():
    return purge_expired_recoveries()
'''

MIGRATION_SOURCE = '''import uuid
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
'''

ARTIFACT_TARGETS = {
    'manager-session-recovery.py': 'wger/manager/models/session_recovery.py',
    'manager-models-init.py': 'wger/manager/models/__init__.py',
    'manager-api-views.py': 'wger/manager/api/views.py',
    'manager-tasks.py': 'wger/manager/tasks.py',
    'manager-0031-session-recovery.py': 'wger/manager/migrations/0031_workoutsessionrecovery.py',
    'manager-log.py': 'wger/manager/models/log.py',
    'manager-0030-workoutlog-cardio-metrics.py': 'wger/manager/migrations/0030_workoutlog_cardio_metrics.py',
}

PINNED_BLOBS = {
    'wger/manager/models/session.py': '7b3bf702f361c14e4ad5fa2decf43017d24829ab',
    'wger/manager/models/log.py': '34ad0f55439fde128bcea4c43fc85ac30cf5191c',
    'wger/manager/models/__init__.py': '79ab3fdb600537bd45fce01daeb15dd7fcce58f7',
    'wger/manager/api/views.py': '7436d2aa75f31bb47d89169b997780979ddb15ec',
    'wger/manager/migrations/0030_workoutlog_cardio_metrics.py': '6480098d4bf6299e91149daddb3e366f0cdcf87d',
}


def replace_once(text, old, new, path):
    if text.count(old) != 1:
        raise ValueError(f'Expected exactly one recovery anchor in {path}; found {text.count(old)}')
    return text.replace(old, new)


def build_outputs(root):
    root = Path(root)
    for path, expected in PINNED_BLOBS.items():
        content = (root / path).read_bytes()
        digest = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
        if digest != expected:
            raise ValueError(f'Pinned recovery source drift: {path}')
    for name in ('manager-session-recovery.py', 'manager-tasks.py', 'manager-0031-session-recovery.py'):
        if (root / ARTIFACT_TARGETS[name]).exists():
            raise ValueError(f'Recovery target already exists: {ARTIFACT_TARGETS[name]}')
    # Require the shipped cardio migration, not an unpatched incompatible base.
    migration = root / 'wger/manager/migrations/0030_workoutlog_cardio_metrics.py'
    if not migration.is_file():
        raise ValueError('Expected pinned manager migration 0030_workoutlog_cardio_metrics')
    export_path = ARTIFACT_TARGETS['manager-models-init.py']
    exports = replace_once((root / export_path).read_text(),
        'from .session import WorkoutSession\n',
        'from .session import WorkoutSession\nfrom .session_recovery import WorkoutSessionRecovery\n', export_path)
    views_path = ARTIFACT_TARGETS['manager-api-views.py']
    views = (root / views_path).read_text()
    anchor = 'class WorkoutSessionViewSet(WgerOwnerObjectModelViewSet):\n'
    views = replace_once(views, anchor, VIEW_IMPORT + anchor, views_path)
    methods_anchor = "    serializer_class = WorkoutSessionSerializer\n    is_private = True\n    ordering_fields = '__all__'\n    filterset_class = WorkoutSessionFilterSet\n"
    views = replace_once(views, methods_anchor, methods_anchor + '\n' + VIEW_METHODS, views_path)
    return {
        'manager-session-recovery.py.next': MODEL_SOURCE,
        'manager-models-init.py.next': exports,
        'manager-api-views.py.next': views,
        'manager-tasks.py.next': TASK_SOURCE,
        'manager-0031-session-recovery.py.next': MIGRATION_SOURCE,
        'manager-log.py.next': (root / ARTIFACT_TARGETS['manager-log.py']).read_text(),
        'manager-0030-workoutlog-cardio-metrics.py.next': migration.read_text(),
    }


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit('usage: patch_session_recovery.py SOURCE_ROOT OUTPUT_DIR')
    outputs = build_outputs(args[0])
    output_dir = Path(args[1])
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (output_dir / name).write_text(content)


if __name__ == '__main__':
    main()
