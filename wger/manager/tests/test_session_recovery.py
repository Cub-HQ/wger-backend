# This file is part of wger Workout Manager.
#
# wger Workout Manager is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# wger Workout Manager is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License

"""Recoverable workout session deletion (fitness-coach #381), ported from the
deploy-time patch tests onto the real wger models."""

# Standard Library
import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import (
    IntegrityError,
    transaction,
)
from django.db.models.signals import post_delete
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

# wger
from wger.core.models import (
    RepetitionUnit,
    WeightUnit,
)
from wger.core.tests.base_testcase import BaseTestCase
from wger.exercises.models import Exercise
from wger.manager.models import (
    Day,
    Routine,
    SlotEntry,
    WorkoutLog,
    WorkoutSession,
    WorkoutSessionRecovery,
)
from wger.manager.models import session_recovery as recovery
from wger.manager.tasks import purge_session_recoveries
from wger.utils.cache import CacheKeyMapper


class SessionRecoveryTestCase(BaseTestCase, TransactionTestCase):
    """
    Runs in autocommit (TransactionTestCase) so the atomic blocks inside the
    recovery code, including their rollbacks, own the observed behaviour.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.other = User.objects.get(username='test')
        self.user.usercache.last_activity = None
        self.user.usercache.save()
        self.routine = Routine.objects.get(pk=1)
        self.day = Day.objects.get(pk=1)
        self.slot = SlotEntry.objects.get(pk=1)
        self.exercise = Exercise.objects.get(pk=1)
        self.reps = RepetitionUnit.objects.get(pk=1)
        self.kg = WeightUnit.objects.get(pk=1)
        self.now = timezone.now().replace(microsecond=123456)
        self.session = WorkoutSession.objects.create(
            user=self.user,
            routine=self.routine,
            day=self.day,
            datetime_start=self.now - datetime.timedelta(hours=1),
            datetime_end=self.now,
            notes='Unicode café\nRecovery is lossless.',
            impression='3',
        )
        self.strength = WorkoutLog.objects.create(
            user=self.user,
            session=self.session,
            routine=self.routine,
            exercise=self.exercise,
            slot_entry=self.slot,
            iteration=4,
            date=self.now,
            repetitions_unit=self.reps,
            weight_unit=self.kg,
            repetitions=Decimal('8.50'),
            repetitions_target=Decimal('9.00'),
            weight=Decimal('82.75'),
            weight_target=Decimal('85.25'),
            rir=Decimal('1.5'),
            rir_target=Decimal('2.0'),
            rest=75,
            rest_target=90,
        )
        self.cardio = WorkoutLog.objects.create(
            user=self.user,
            session=self.session,
            exercise=self.exercise,
            date=self.now,
            repetitions_unit=self.reps,
            weight_unit=self.kg,
            average_speed=Decimal('9.25'),
            pace=Decimal('6.49'),
            incline=Decimal('2.50'),
            calories=Decimal('321.25'),
        )
        WorkoutLog.objects.filter(pk=self.strength.pk).update(next_log=self.cardio)
        self.unrelated = WorkoutSession.objects.create(user=self.other)
        self.unrelated_log = WorkoutLog.objects.create(
            user=self.other,
            session=self.unrelated,
            exercise=self.exercise,
            repetitions=Decimal('3.00'),
        )
        cache.clear()

    @staticmethod
    def values(model):
        return {str(row['id']): row for row in model.objects.values()}

    def state(self):
        return self.values(WorkoutSession), self.values(WorkoutLog)

    def archive(self):
        return recovery.archive_session(self.user, self.session.pk)

    def assert_conflict_preserves_archive(self, row):
        before = self.state()
        snapshot = WorkoutSessionRecovery.objects.get(pk=row.pk).snapshot
        with self.assertRaises(recovery.RecoveryConflict) as caught:
            recovery.restore_session(self.user, row.pk)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.state(), before)
        self.assertEqual(WorkoutSessionRecovery.objects.get(pk=row.pk).snapshot, snapshot)

    def test_lossless_round_trip_all_concrete_fields_and_unrelated_rows(self):
        before = self.state()
        row = self.archive()
        others = (
            {k: v for k, v in before[0].items() if k != str(self.session.pk)},
            {
                k: v
                for k, v in before[1].items()
                if k not in (str(self.strength.pk), str(self.cardio.pk))
            },
        )
        self.assertEqual(self.state(), others)
        self.assertEqual(row.original_session_id, self.session.pk)
        self.assertEqual(row.routine_id, self.routine.pk)
        self.assertEqual(row.snapshot['version'], 1)
        self.assertEqual(row.expires_at - row.deleted_at, datetime.timedelta(days=15))
        cardio = next(log for log in row.snapshot['logs'] if log['id'] == str(self.cardio.pk))
        self.assertIsNone(cardio['rir'])
        self.assertEqual(cardio['calories'], '321.25')

        restored = recovery.restore_session(self.user, row.pk)

        self.assertEqual(restored.pk, self.session.pk)
        self.assertEqual(self.state(), before)
        self.assertFalse(WorkoutSessionRecovery.objects.filter(pk=row.pk).exists())

    def test_owner_is_hidden_for_archive_and_restore(self):
        before = self.state()
        with self.assertRaises(NotFound):
            recovery.archive_session(self.other, self.session.pk)
        self.assertEqual(self.state(), before)
        self.assertFalse(WorkoutSessionRecovery.objects.exists())
        row = self.archive()
        with self.assertRaises(NotFound):
            recovery.restore_session(self.other, row.pk)
        self.assertTrue(WorkoutSessionRecovery.objects.filter(pk=row.pk).exists())
        self.assertFalse(WorkoutSession.objects.filter(pk=self.session.pk).exists())

    def test_expiry_equality_is_not_restorable(self):
        row = self.archive()
        WorkoutSessionRecovery.objects.filter(pk=row.pk).update(expires_at=self.now)
        with patch('django.utils.timezone.now', return_value=self.now):
            with self.assertRaises(NotFound):
                recovery.restore_session(self.user, row.pk)
        self.assertFalse(WorkoutSession.objects.filter(pk=self.session.pk).exists())
        self.assertFalse(WorkoutLog.objects.filter(session_id=self.session.pk).exists())

    def test_session_uuid_collision_keeps_snapshot_and_existing_rows(self):
        row = self.archive()
        WorkoutSession.objects.create(pk=self.session.pk, user=self.user, notes='new occupant')
        self.assert_conflict_preserves_archive(row)

    def test_log_uuid_collision_does_not_leave_partial_session(self):
        row = self.archive()
        WorkoutLog.objects.bulk_create(
            [
                WorkoutLog(
                    pk=self.cardio.pk,
                    user=self.other,
                    exercise=self.exercise,
                    session=self.unrelated,
                    repetitions=Decimal('1.00'),
                )
            ]
        )
        self.assert_conflict_preserves_archive(row)
        self.assertFalse(WorkoutSession.objects.filter(pk=self.session.pk).exists())

    def test_missing_nullable_reference_is_not_silently_discarded(self):
        row = self.archive()
        SlotEntry.objects.filter(pk=self.slot.pk).delete()
        self.assert_conflict_preserves_archive(row)

    def test_reference_reassigned_to_another_owner_conflicts(self):
        row = self.archive()
        Routine.objects.filter(pk=self.routine.pk).update(user=self.other)
        self.assert_conflict_preserves_archive(row)

    def test_archive_insert_failure_does_not_delete_live_data(self):
        before = self.state()
        with patch.object(
            WorkoutSessionRecovery, 'save', side_effect=IntegrityError('injected insert failure')
        ):
            with self.assertRaises((IntegrityError, recovery.RecoveryConflict)):
                self.archive()
        self.assertEqual(self.state(), before)
        self.assertFalse(WorkoutSessionRecovery.objects.exists())

    def test_archive_delete_failure_rolls_back_snapshot_and_deleted_rows(self):
        before = self.state()

        def fail_after_delete(sender, instance, **kwargs):
            if instance.pk == self.session.pk:
                raise RuntimeError('injected after session deletion')

        post_delete.connect(fail_after_delete, sender=WorkoutSession, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected after session deletion'):
                self.archive()
        finally:
            post_delete.disconnect(fail_after_delete, sender=WorkoutSession)
        self.assertEqual(self.state(), before)
        self.assertFalse(WorkoutSessionRecovery.objects.exists())

    def test_restore_late_failure_rolls_back_all_inserted_rows(self):
        row = self.archive()
        before = self.state()

        def fail_after_consumption(sender, instance, **kwargs):
            raise RuntimeError('injected after archive consumption')

        post_delete.connect(fail_after_consumption, sender=WorkoutSessionRecovery, weak=False)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected after archive consumption'):
                recovery.restore_session(self.user, row.pk)
        finally:
            post_delete.disconnect(fail_after_consumption, sender=WorkoutSessionRecovery)
        self.assertEqual(self.state(), before)
        self.assertEqual(WorkoutSessionRecovery.objects.get(pk=row.pk).snapshot, row.snapshot)

    def test_external_incoming_next_log_cannot_cascade_delete_unrelated_logs(self):
        own_log = WorkoutLog.objects.create(
            user=self.user,
            exercise=self.exercise,
            session=self.unrelated_session_for_user(),
            repetitions=Decimal('2.00'),
        )
        WorkoutLog.objects.filter(pk=own_log.pk).update(next_log=self.strength)
        before = self.state()
        with self.assertRaises(recovery.RecoveryConflict):
            self.archive()
        self.assertEqual(self.state(), before)
        self.assertFalse(WorkoutSessionRecovery.objects.exists())

    def unrelated_session_for_user(self):
        return WorkoutSession.objects.create(
            user=self.user, datetime_start=self.now - datetime.timedelta(days=3)
        )

    def test_cleanup_is_bounded_and_only_removes_expired_archives(self):
        before = self.state()
        expired = []
        active = None
        for offset in (-3, -2, 0, 1):
            row = WorkoutSessionRecovery.objects.create(
                user=self.user,
                original_session_id=uuid.uuid4(),
                routine_id=self.routine.pk,
                deleted_at=self.now - datetime.timedelta(days=10),
                expires_at=self.now + datetime.timedelta(seconds=offset),
                snapshot={'version': 1, 'session': {}, 'logs': []},
            )
            if offset <= 0:
                expired.append(row.pk)
            else:
                active = row.pk
        recovery.purge_expired_recoveries(now=self.now, batch_size=2)
        self.assertEqual(WorkoutSessionRecovery.objects.filter(pk__in=expired).count(), 1)
        self.assertTrue(WorkoutSessionRecovery.objects.filter(pk=active).exists())
        self.assertEqual(self.state(), before)
        recovery.purge_expired_recoveries(now=self.now, batch_size=2)
        self.assertFalse(WorkoutSessionRecovery.objects.filter(pk__in=expired).exists())
        self.assertTrue(WorkoutSessionRecovery.objects.filter(pk=active).exists())
        self.assertEqual(self.state(), before)

    def test_scheduled_task_purges_expired_archives(self):
        row = self.archive()
        WorkoutSessionRecovery.objects.filter(pk=row.pk).update(
            expires_at=timezone.now() - datetime.timedelta(seconds=1)
        )
        self.assertEqual(purge_session_recoveries(), 1)
        self.assertFalse(WorkoutSessionRecovery.objects.exists())

    def test_summary_exposes_only_recovery_metadata(self):
        row = self.archive()
        summary = recovery.recovery_summary(row)
        self.assertEqual(
            set(summary),
            {'id', 'original_session_id', 'routine_id', 'deleted_at', 'expires_at', 'datetime_start'},
        )
        self.assertEqual(summary['id'], str(row.pk))
        self.assertEqual(summary['original_session_id'], str(self.session.pk))
        self.assertEqual(summary['routine_id'], self.routine.pk)

    def seed_recovery_caches(self, routines):
        keys = []
        for routine in routines:
            keys.extend(
                [
                    CacheKeyMapper.routine_api_logs(routine.pk, routine.user_id),
                    CacheKeyMapper.routine_date_sequence_key(routine.pk),
                    CacheKeyMapper.routine_api_date_sequence_display_key(
                        routine.pk, routine.user_id
                    ),
                    CacheKeyMapper.routine_api_date_sequence_gym_key(routine.pk, routine.user_id),
                    CacheKeyMapper.routine_api_stats(routine.pk, routine.user_id),
                ]
            )
        cache.set_many({key: 'stale-deleted-workout' for key in keys})
        return keys

    def test_restore_evicts_routine_caches_only_after_outer_commit(self):
        other_routine = Routine.objects.create(
            user=self.user, name='other', start=self.now.date(), end=self.now.date()
        )
        WorkoutLog.objects.filter(pk=self.cardio.pk).update(routine=other_routine)
        row = self.archive()
        self.user.usercache.last_activity = None
        self.user.usercache.save()
        keys = self.seed_recovery_caches([self.routine, other_routine])
        untouched = self.seed_recovery_caches([Routine.objects.get(pk=2)])
        structure = CacheKeyMapper.routine_api_structure_key(self.routine.pk, self.user.pk)
        cache.set(structure, 'unchanged-routine-structure')
        with transaction.atomic():
            recovery.restore_session(self.user, row.pk)
            self.assertEqual(cache.get_many(keys), dict.fromkeys(keys, 'stale-deleted-workout'))
            self.user.usercache.refresh_from_db()
            self.assertIsNone(self.user.usercache.last_activity)
        self.assertEqual(cache.get_many(keys), {})
        self.assertEqual(
            cache.get_many(untouched), dict.fromkeys(untouched, 'stale-deleted-workout')
        )
        self.assertEqual(cache.get(structure), 'unchanged-routine-structure')
        self.user.usercache.refresh_from_db()
        self.assertIsNotNone(self.user.usercache.last_activity)

    def test_restore_outer_rollback_preserves_cache_and_recovery_snapshot(self):
        row = self.archive()
        keys = self.seed_recovery_caches([self.routine])
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'outer transaction failed'):
            with transaction.atomic():
                recovery.restore_session(self.user, row.pk)
                raise RuntimeError('outer transaction failed')
        self.assertEqual(cache.get_many(keys), dict.fromkeys(keys, 'stale-deleted-workout'))
        self.assertEqual(self.state(), before)
        self.assertEqual(WorkoutSessionRecovery.objects.get(pk=row.pk).snapshot, row.snapshot)

    def test_api_delete_list_restore_and_owner_isolation(self):
        owner = APIClient()
        owner.force_authenticate(self.user)
        intruder = APIClient()
        intruder.force_authenticate(self.other)
        detail = reverse('workoutsession-detail', kwargs={'pk': self.session.pk})
        listing = reverse('workoutsession-recoveries')

        before = self.state()
        self.assertEqual(intruder.delete(detail).status_code, 404)
        self.assertEqual(self.state(), before)

        deleted = owner.delete(detail)
        self.assertEqual(deleted.status_code, 200)
        recovery_id = deleted.json()['id']
        self.assertFalse(WorkoutSession.objects.filter(pk=self.session.pk).exists())

        self.assertEqual(intruder.get(listing).json(), [])
        listed = owner.get(listing, {'routine': self.routine.pk})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row['id'] for row in listed.json()], [recovery_id])
        self.assertEqual(owner.get(listing, {'routine': -1}).json(), [])

        restore = reverse('workoutsession-restore-recovery', kwargs={'recovery_id': recovery_id})
        self.assertEqual(intruder.post(restore).status_code, 404)
        self.assertTrue(WorkoutSessionRecovery.objects.filter(pk=recovery_id).exists())
        restored = owner.post(restore)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()['id'], str(self.session.pk))
        self.assertEqual(self.state(), before)
        self.assertEqual(owner.get(listing).json(), [])
        self.assertEqual(owner.post(restore).status_code, 404)

    def test_api_rejects_malformed_ids_without_changes(self):
        owner = APIClient()
        owner.force_authenticate(self.user)
        before = self.state()
        restore = reverse('workoutsession-restore-recovery', kwargs={'recovery_id': 'not-a-uuid'})
        self.assertEqual(owner.post(restore).status_code, 404)
        listing = reverse('workoutsession-recoveries')
        self.assertEqual(owner.get(listing, {'routine': 'abc'}).status_code, 404)
        self.assertEqual(self.state(), before)
