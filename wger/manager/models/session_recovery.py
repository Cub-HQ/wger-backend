"""Private, transactional, fifteen-day workout recovery snapshots."""
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


# Nullable columns added after snapshot version 1 was first written (manager
# 0032). Snapshots archived before that migration lack them and restore as NULL;
# any other missing or unknown key still refuses the restore.
ADDED_AFTER_V1 = {
    WorkoutLog: {
        'duration', 'distance', 'distance_unit_id', 'level', 'max_speed', 'max_speed_unit_id'
    },
}


def _decode(model, data):
    fields = model._meta.concrete_fields
    if not isinstance(data, dict):
        raise RecoveryConflict()
    expected = {field.attname for field in fields}
    if set(data) not in (expected, expected - ADDED_AFTER_V1.get(model, set())):
        raise RecoveryConflict()
    return {
        field.attname: None if data.get(field.attname) is None else
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
