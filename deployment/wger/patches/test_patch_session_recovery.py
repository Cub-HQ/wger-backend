"""Exercise generated recovery code in an isolated, real SQLite/Django process."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


# The recovery model under test is the committed fork source the backend image ships.
SOURCE = Path(__file__).parents[3]
MODEL = SOURCE / 'wger/manager/models/session_recovery.py'


def fork_view_methods():
    # The shipped WorkoutSessionViewSet recovery actions, destroy through restore_recovery.
    views = (SOURCE / 'wger/manager/api/views.py').read_text()
    start = views.index('    def destroy(self, request, *args, **kwargs):\n')
    return views[start:views.index('    def get_queryset(self):\n', start)]


# These models retain the production concrete field names, types, nullability,
# and CASCADE relations. No recovery behavior is replaced by fixture code.
REFERENCE_MODELS = '''
from django.conf import settings
from django.db import models

class Routine(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    def get_owner_object(self):
        return self

class Day(models.Model):
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='days')

    def get_owner_object(self):
        return self.routine

class SlotEntry(models.Model):
    day = models.ForeignKey(Day, on_delete=models.CASCADE)

    def get_owner_object(self):
        return self.day.routine

class Slot(models.Model):
    day = models.ForeignKey(Day, on_delete=models.CASCADE, related_name='slots')

class UserCache(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='usercache')
    last_activity = models.DateField(null=True)

class Exercise(models.Model):
    name = models.CharField(max_length=100)

class RepetitionUnit(models.Model):
    name = models.CharField(max_length=100)

class WeightUnit(models.Model):
    name = models.CharField(max_length=100)
'''

SESSION_MODEL = '''
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class WorkoutSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    routine = models.ForeignKey('Routine', null=True, on_delete=models.CASCADE, related_name='sessions')
    day = models.ForeignKey('Day', null=True, on_delete=models.CASCADE)
    datetime_start = models.DateTimeField(default=timezone.now)
    datetime_end = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    impression = models.CharField(max_length=2, default='2')

    def get_owner_object(self):
        return self
'''

LOG_MODEL = '''
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class WorkoutLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    date = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    next_log = models.ForeignKey('self', null=True, default=None, on_delete=models.CASCADE)
    session = models.ForeignKey('WorkoutSession', null=True, on_delete=models.CASCADE, related_name='logs')
    exercise = models.ForeignKey('Exercise', on_delete=models.CASCADE)
    routine = models.ForeignKey('Routine', null=True, on_delete=models.CASCADE)
    slot_entry = models.ForeignKey('SlotEntry', null=True, on_delete=models.CASCADE)
    iteration = models.PositiveIntegerField(null=True)
    repetitions_unit = models.ForeignKey('RepetitionUnit', null=True, blank=True, on_delete=models.CASCADE)
    weight_unit = models.ForeignKey('WeightUnit', null=True, blank=True, on_delete=models.CASCADE)
    repetitions = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    repetitions_target = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    weight_target = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    average_speed = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    pace = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    incline = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    calories = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    rir = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    rir_target = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    rest = models.PositiveIntegerField(null=True, blank=True)
    rest_target = models.PositiveIntegerField(null=True, blank=True)

    def get_owner_object(self):
        return self
'''

# Scoped copies of pinned signals/helpers/cache behavior, not the full wger
# imports. Locmem and UserCache are real; activity uses the fixture's UTC zone.
CACHE_SIGNALS = '''
from functools import wraps
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from .models import WorkoutLog, WorkoutSession

class CacheKeyMapper:
    @classmethod
    def routine_date_sequence_key(cls, pk):
        return f'routine-date-sequence-{pk}'
    @classmethod
    def routine_api_date_sequence_display_key(cls, pk, user_id):
        return f'routine-api-date-sequence-display-{user_id}-{pk}'
    @classmethod
    def routine_api_date_sequence_gym_key(cls, pk, user_id):
        return f'routine-api-date-sequence-gym-{user_id}-{pk}'
    @classmethod
    def routine_api_logs(cls, pk, user_id):
        return f'routine-api-logs-{user_id}-{pk}'
    @classmethod
    def routine_api_stats(cls, pk, user_id):
        return f'routine-api-stats-{user_id}-{pk}'
    @classmethod
    def routine_api_structure_key(cls, pk, user_id=None):
        return f'routine-api-structure-{user_id}-{pk}'
    @classmethod
    def slot_entry_configs_key(cls, pk):
        return f'slot-entry-configs-{pk}'

def reset_routine_cache(instance, structure=True):
    cache.delete(CacheKeyMapper.routine_date_sequence_key(instance.id))
    cache.delete(CacheKeyMapper.routine_api_date_sequence_display_key(instance.id, instance.user_id))
    cache.delete(CacheKeyMapper.routine_api_date_sequence_gym_key(instance.id, instance.user_id))
    cache.delete(CacheKeyMapper.routine_api_logs(instance.id, instance.user_id))
    cache.delete(CacheKeyMapper.routine_api_stats(instance.id, instance.user_id))
    if structure:
        cache.delete(CacheKeyMapper.routine_api_structure_key(instance.id, instance.user_id))
    if instance.pk:
        for day in instance.days.all():
            for slot in day.slots.all():
                for entry in slot.entries.all():
                    cache.delete(CacheKeyMapper.slot_entry_configs_key(entry.id))

def ignore_missing_relations(handler):
    @wraps(handler)
    def wrapper(sender, instance, **kwargs):
        try:
            handler(sender, instance, **kwargs)
        except ObjectDoesNotExist:
            pass
    return wrapper

def get_user_last_activity(user):
    dates = []
    last_log = WorkoutLog.objects.filter(user=user).order_by('date').last()
    if last_log:
        dates.append(timezone.localdate(last_log.date))
    last_session = WorkoutSession.objects.filter(user=user).order_by('datetime_start').last()
    if last_session:
        dates.append(timezone.localdate(last_session.datetime_start))
    return max(dates) if dates else None

def update_activity_cache(sender, instance, **kwargs):
    user = instance.user
    user.usercache.last_activity = get_user_last_activity(user)
    user.usercache.save()

@ignore_missing_relations
def handle_workout_log_change(sender, instance, **kwargs):
    update_activity_cache(sender, instance, **kwargs)
    if instance.routine:
        cache.delete(CacheKeyMapper.routine_api_logs(instance.routine.id, instance.user_id))
        reset_routine_cache(instance.routine, structure=False)

@ignore_missing_relations
def handle_workout_session_change(sender, instance, **kwargs):
    update_activity_cache(sender, instance, **kwargs)
    if instance.routine:
        cache.delete(CacheKeyMapper.routine_api_logs(instance.routine.id, instance.user_id))
        reset_routine_cache(instance.routine, structure=False)
'''


def configure_orm(directory):
    root = Path(directory)
    package = root / 'manager'
    models_dir = package / 'models'
    models_dir.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (models_dir / '__init__.py').write_text(
        'from .references import *\nfrom .session import WorkoutSession\n'
        'from .log import WorkoutLog\nfrom .recovery import WorkoutSessionRecovery\n'
    )
    (models_dir / 'references.py').write_text(textwrap.dedent(REFERENCE_MODELS))
    (models_dir / 'session.py').write_text(textwrap.dedent(SESSION_MODEL))
    (models_dir / 'log.py').write_text(textwrap.dedent(LOG_MODEL))
    (models_dir / 'recovery.py').write_text(MODEL.read_text())
    (package / 'signals.py').write_text(textwrap.dedent(CACHE_SIGNALS))
    sys.path.insert(0, str(root))
    from django.conf import settings
    settings.configure(
        SECRET_KEY='isolated-recovery-tests',
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', 'manager'],
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(root / 'test.sqlite3')}},
        USE_TZ=True,
        TIME_ZONE='UTC',
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
        DEFAULT_AUTO_FIELD='django.db.models.AutoField',
    )
    import django
    django.setup()
    from django.apps import apps
    from django.db import connection
    with connection.schema_editor() as editor:
        for model in apps.get_models():
            editor.create_model(model)


class ForkRecoveryORMTests(unittest.TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from manager.models import references, session, log, recovery
        self.code = recovery
        self.Session = session.WorkoutSession
        self.Log = log.WorkoutLog
        self.Recovery = recovery.WorkoutSessionRecovery
        self.refs = references
        self.User = get_user_model()
        # Autocommit rather than TestCase's wrapping transaction proves that
        # generated atomic blocks, including their rollback, own the behavior.
        self.Recovery.objects.all().delete()
        self.Log.objects.all().delete()
        self.Session.objects.all().delete()
        self.User.objects.all().delete()
        for model in (references.Exercise, references.RepetitionUnit, references.WeightUnit):
            model.objects.all().delete()
        self.user = self.User.objects.create(username='owner')
        references.UserCache.objects.create(user=self.user)
        from django.core.cache import cache
        cache.clear()
        self.other = self.User.objects.create(username='other')
        self.routine = references.Routine.objects.create(user=self.user)
        self.day = references.Day.objects.create(routine=self.routine)
        self.slot = references.SlotEntry.objects.create(day=self.day)
        self.exercise = references.Exercise.objects.create(name='squat')
        self.reps = references.RepetitionUnit.objects.create(name='reps')
        self.kg = references.WeightUnit.objects.create(name='kg')
        self.now = timezone.now().replace(microsecond=123456)
        self.session = self.Session.objects.create(
            user=self.user, routine=self.routine, day=self.day,
            datetime_start=self.now - timedelta(hours=1), datetime_end=self.now,
            notes='Unicode café\nRecovery is lossless.', impression='3',
        )
        self.strength = self.Log.objects.create(
            user=self.user, session=self.session, routine=self.routine,
            exercise=self.exercise, slot_entry=self.slot, iteration=4,
            date=self.now, repetitions_unit=self.reps, weight_unit=self.kg,
            repetitions='8.50', repetitions_target='9.00', weight='82.75',
            weight_target='85.25', rir='1.5', rir_target='2.0', rest=75, rest_target=90,
        )
        self.cardio = self.Log.objects.create(
            user=self.user, session=self.session, exercise=self.exercise,
            date=self.now, repetitions='32.50', weight='5.25',
            repetitions_unit=self.reps, weight_unit=self.kg,
            average_speed='9.25', pace='6.49', incline='2.50', calories='321.25',
        )
        self.strength.next_log = self.cardio
        self.strength.save(update_fields=['next_log'])
        self.unrelated = self.Session.objects.create(user=self.other, notes=None)
        self.unrelated_log = self.Log.objects.create(
            user=self.other, session=self.unrelated, exercise=self.exercise, repetitions='3.00',
        )

    @staticmethod
    def values(model):
        return {str(row['id']): row for row in model.objects.values()}

    def state(self):
        return self.values(self.Session), self.values(self.Log)

    def archive(self):
        return self.code.archive_session(self.user, self.session.pk)

    def assert_conflict_preserves_archive(self, row):
        before = self.state()
        snapshot = self.Recovery.objects.get(pk=row.pk).snapshot
        with self.assertRaises(self.code.RecoveryConflict) as caught:
            self.code.restore_session(self.user, row.pk)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.Recovery.objects.get(pk=row.pk).snapshot, snapshot)

    def test_lossless_round_trip_all_concrete_fields_and_unrelated_rows(self):
        before = self.state()
        row = self.archive()
        self.assertEqual(self.state(), (
            {str(self.unrelated.pk): before[0][str(self.unrelated.pk)]},
            {str(self.unrelated_log.pk): before[1][str(self.unrelated_log.pk)]},
        ))
        self.assertEqual(row.original_session_id, self.session.pk)
        self.assertEqual(row.routine_id, self.routine.pk)
        self.assertEqual(row.snapshot['version'], 1)
        from datetime import timedelta
        self.assertEqual(row.expires_at - row.deleted_at, timedelta(days=15))
        self.assertIsNone(next(log for log in row.snapshot['logs'] if log['id'] == str(self.cardio.pk))['rir'])
        restored = self.code.restore_session(self.user, row.pk)
        self.assertEqual(restored.pk, self.session.pk)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.Recovery.objects.filter(pk=row.pk).exists())

    def test_owner_is_hidden_for_archive_and_restore(self):
        from rest_framework.exceptions import NotFound
        before = self.state()
        with self.assertRaises(NotFound):
            self.code.archive_session(self.other, self.session.pk)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.Recovery.objects.exists())
        row = self.archive()
        with self.assertRaises(NotFound):
            self.code.restore_session(self.other, row.pk)
        self.assertTrue(self.Recovery.objects.filter(pk=row.pk).exists())
        self.assertFalse(self.Session.objects.filter(pk=self.session.pk).exists())

    def test_expiry_equality_is_not_restorable(self):
        from rest_framework.exceptions import NotFound
        row = self.archive()
        self.Recovery.objects.filter(pk=row.pk).update(expires_at=self.now)
        with patch('django.utils.timezone.now', return_value=self.now):
            with self.assertRaises(NotFound):
                self.code.restore_session(self.user, row.pk)
        self.assertFalse(self.Session.objects.filter(pk=self.session.pk).exists())
        self.assertFalse(self.Log.objects.filter(session_id=self.session.pk).exists())

    def test_session_uuid_collision_keeps_snapshot_and_existing_rows(self):
        row = self.archive()
        self.Session.objects.create(pk=self.session.pk, user=self.user, notes='new occupant')
        self.assert_conflict_preserves_archive(row)

    def test_log_uuid_collision_does_not_leave_partial_session(self):
        row = self.archive()
        self.Log.objects.create(pk=self.cardio.pk, user=self.user, exercise=self.exercise, session=self.unrelated)
        self.assert_conflict_preserves_archive(row)
        self.assertFalse(self.Session.objects.filter(pk=self.session.pk).exists())

    def test_missing_required_reference_keeps_snapshot(self):
        row = self.archive()
        self.exercise.delete()
        self.assert_conflict_preserves_archive(row)

    def test_missing_nullable_reference_is_not_silently_discarded(self):
        row = self.archive()
        self.slot.delete()
        self.assert_conflict_preserves_archive(row)

    def test_archive_insert_failure_does_not_delete_live_data(self):
        from django.db import IntegrityError
        before = self.state()
        with patch.object(self.Recovery, 'save', side_effect=IntegrityError('injected archive insert failure')):
            with self.assertRaises((IntegrityError, self.code.RecoveryConflict)):
                self.archive()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.Recovery.objects.exists())

    def test_archive_delete_failure_rolls_back_snapshot_and_deleted_rows(self):
        from django.db.models.signals import post_delete
        before = self.state()

        def fail_after_delete(sender, instance, **kwargs):
            if instance.pk == self.session.pk:
                raise RuntimeError('injected after session deletion')

        post_delete.connect(fail_after_delete, sender=self.Session, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected after session deletion'):
                self.archive()
        finally:
            post_delete.disconnect(fail_after_delete, sender=self.Session)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.Recovery.objects.exists())

    def test_restore_late_failure_rolls_back_all_inserted_rows(self):
        from django.db.models.signals import post_delete
        row = self.archive()
        before = self.state()

        def fail_after_consumption(sender, instance, **kwargs):
            raise RuntimeError('injected after archive consumption')

        post_delete.connect(fail_after_consumption, sender=self.Recovery, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected after archive consumption'):
                self.code.restore_session(self.user, row.pk)
        finally:
            post_delete.disconnect(fail_after_consumption, sender=self.Recovery)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.Recovery.objects.get(pk=row.pk).snapshot, row.snapshot)

    def test_external_incoming_next_log_cannot_cascade_delete_unrelated_logs(self):
        self.unrelated_log.next_log = self.strength
        self.unrelated_log.save(update_fields=['next_log'])
        before = self.state()
        with self.assertRaises(self.code.RecoveryConflict):
            self.archive()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.Recovery.objects.exists())

    def test_cleanup_is_bounded_and_only_removes_expired_archives(self):
        import uuid
        from datetime import timedelta
        before = self.state()
        expired = []
        for offset in (-3, -2, 0, 1):
            row = self.Recovery.objects.create(
                user=self.user, original_session_id=uuid.uuid4(), routine_id=self.routine.pk,
                deleted_at=self.now - timedelta(days=10),
                expires_at=self.now + timedelta(seconds=offset),
                snapshot={'version': 1, 'session': {}, 'logs': []},
            )
            if offset <= 0:
                expired.append(row.pk)
            else:
                active = row.pk
        self.code.purge_expired_recoveries(now=self.now, batch_size=2)
        self.assertEqual(self.Recovery.objects.filter(pk__in=expired).count(), 1)
        self.assertTrue(self.Recovery.objects.filter(pk=active).exists())
        self.assertEqual(self.state(), before)
        self.code.purge_expired_recoveries(now=self.now, batch_size=2)
        self.assertFalse(self.Recovery.objects.filter(pk__in=expired).exists())
        self.assertTrue(self.Recovery.objects.filter(pk=active).exists())
        self.assertEqual(self.state(), before)

    def test_summary_exposes_only_recovery_metadata(self):
        row = self.archive()
        summary = self.code.recovery_summary(row)
        self.assertEqual(set(summary), {
            'id', 'original_session_id', 'routine_id', 'deleted_at', 'expires_at', 'datetime_start',
        })
        self.assertEqual(str(summary['id']), str(row.pk))
        self.assertEqual(str(summary['original_session_id']), str(self.session.pk))
        self.assertEqual(summary['routine_id'], self.routine.pk)
        from django.utils.dateparse import parse_datetime
        self.assertEqual(parse_datetime(str(summary['datetime_start'])), self.session.datetime_start)

    def test_api_delete_list_restore_and_owner_isolation(self):
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        from rest_framework import serializers, viewsets
        from rest_framework.decorators import action
        from rest_framework.exceptions import NotFound
        from rest_framework.permissions import IsAuthenticated
        from rest_framework.response import Response
        from rest_framework.test import APIRequestFactory, force_authenticate

        session_model = self.Session

        class SessionSerializer(serializers.ModelSerializer):
            class Meta:
                model = session_model
                fields = '__all__'

        namespace = {
            'viewsets': viewsets, 'action': action, 'Response': Response,
            'RecoveryValidationError': ValidationError, 'RecoveryNotFound': NotFound,
            'recovery_timezone': timezone, 'SessionSerializer': SessionSerializer,
            'IsAuthenticated': IsAuthenticated,
        }
        for name in ('WorkoutSessionRecovery', 'archive_session', 'restore_session',
                     'purge_expired_recoveries', 'recovery_summary'):
            namespace[name] = getattr(self.code, name)
        exec(
            'class RecoveryViewSet(viewsets.GenericViewSet):\n'
            '    serializer_class = SessionSerializer\n'
            '    permission_classes = [IsAuthenticated]\n'
            + fork_view_methods(),
            namespace,
        )
        viewset = namespace['RecoveryViewSet']
        factory = APIRequestFactory()

        def request(method, action_name, user, path='/', **kwargs):
            incoming = getattr(factory, method)(path)
            if user is not None:
                force_authenticate(incoming, user=user)
            return viewset.as_view({method: action_name})(incoming, **kwargs)

        before = self.state()
        denied = request('delete', 'destroy', self.other, pk=str(self.session.pk))
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.state(), before)
        deleted = request('delete', 'destroy', self.user, pk=str(self.session.pk))
        self.assertEqual(deleted.status_code, 200)
        recovery_id = deleted.data['id']
        self.assertEqual(request('get', 'recoveries', self.other).data, [])
        listed = request('get', 'recoveries', self.user, path=f'/?routine={self.routine.pk}')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row['id'] for row in listed.data], [recovery_id])
        self.assertEqual(request('get', 'recoveries', self.user, path='/?routine=-1').data, [])
        denied = request('post', 'restore_recovery', self.other, recovery_id=recovery_id)
        self.assertEqual(denied.status_code, 404)
        self.assertTrue(self.Recovery.objects.filter(pk=recovery_id).exists())
        restored = request('post', 'restore_recovery', self.user, recovery_id=recovery_id)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.data['id'], str(self.session.pk))
        self.assertEqual(self.state(), before)
        self.assertEqual(request('get', 'recoveries', self.user).data, [])
        self.assertEqual(
            request('post', 'restore_recovery', self.user, recovery_id=recovery_id).status_code, 404,
        )

    def test_reference_reassigned_to_another_owner_conflicts(self):
        row = self.archive()
        self.refs.Routine.objects.filter(pk=self.routine.pk).update(user=self.other)
        self.assert_conflict_preserves_archive(row)

    def seed_recovery_caches(self, routines):
        from django.core.cache import cache
        from manager.signals import CacheKeyMapper
        keys = []
        for routine in routines:
            keys.extend([
                CacheKeyMapper.routine_api_logs(routine.pk, routine.user_id),
                CacheKeyMapper.routine_date_sequence_key(routine.pk),
                CacheKeyMapper.routine_api_date_sequence_display_key(routine.pk, routine.user_id),
                CacheKeyMapper.routine_api_date_sequence_gym_key(routine.pk, routine.user_id),
                CacheKeyMapper.routine_api_stats(routine.pk, routine.user_id),
            ])
        cache.set_many({key: 'stale-deleted-workout' for key in keys})
        return keys

    def test_restore_evicts_real_routine_caches_only_after_outer_commit(self):
        from django.core.cache import cache
        from django.db import transaction
        from manager.signals import CacheKeyMapper
        other_routine = self.refs.Routine.objects.create(user=self.user)
        self.Log.objects.filter(pk=self.cardio.pk).update(routine=other_routine)
        row = self.archive()
        keys = self.seed_recovery_caches([self.routine, other_routine])
        untouched_routine = self.refs.Routine.objects.create(user=self.other)
        untouched = self.seed_recovery_caches([untouched_routine])
        structure = CacheKeyMapper.routine_api_structure_key(self.routine.pk, self.user.pk)
        cache.set(structure, 'unchanged-routine-structure')
        with transaction.atomic():
            self.code.restore_session(self.user, row.pk)
            self.assertEqual(cache.get_many(keys), dict.fromkeys(keys, 'stale-deleted-workout'))
            self.user.usercache.refresh_from_db()
            self.assertIsNone(self.user.usercache.last_activity)
        self.assertEqual(cache.get_many(keys), {})
        self.assertEqual(cache.get_many(untouched), dict.fromkeys(untouched, 'stale-deleted-workout'))
        self.assertEqual(cache.get(structure), 'unchanged-routine-structure')
        self.user.usercache.refresh_from_db()
        self.assertEqual(self.user.usercache.last_activity, self.now.date())

    def test_restore_outer_rollback_preserves_cache_and_recovery_snapshot(self):
        from django.core.cache import cache
        from django.db import transaction
        row = self.archive()
        keys = self.seed_recovery_caches([self.routine])
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'outer transaction failed'):
            with transaction.atomic():
                self.code.restore_session(self.user, row.pk)
                raise RuntimeError('outer transaction failed')
        self.assertEqual(cache.get_many(keys), dict.fromkeys(keys, 'stale-deleted-workout'))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.Recovery.objects.get(pk=row.pk).snapshot, row.snapshot)
        self.user.usercache.refresh_from_db()
        self.assertIsNone(self.user.usercache.last_activity)


class RecoveryORMTests(unittest.TestCase):
    def test_fork_recovery_orm_behaviors(self):
        missing = [name for name in ('django', 'rest_framework') if importlib.util.find_spec(name) is None]
        if missing:
            self.skipTest('Real ORM recovery tests require missing dependencies: ' + ', '.join(missing))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--orm'],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def load_tests(loader, tests, pattern):
    # Discovery must never import a second app into another test's Django registry.
    return loader.loadTestsFromTestCase(RecoveryORMTests)


if __name__ == '__main__':
    if '--orm' in sys.argv:
        with tempfile.TemporaryDirectory(prefix='recovery-orm-') as directory:
            configure_orm(directory)
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(ForkRecoveryORMTests)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            sys.exit(not result.wasSuccessful())
    unittest.main()
