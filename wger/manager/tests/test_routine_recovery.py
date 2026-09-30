"""Routine trash/restore, recorded edits and rebuilds (wger-gym#24)"""

# Standard Library
import datetime
import threading
from unittest import skipUnless
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.test import APIClient

# wger
from wger.core.tests.base_testcase import (
    BaseTestCase,
    WgerTestCase,
)
from wger.intervals.push import gym_occurrences
from wger.manager import routine_recovery
from wger.manager.models import (
    Day,
    Routine,
    RoutineRecovery,
    Slot,
    SlotEntry,
    WeightConfig,
    WorkoutLog,
    WorkoutSession,
)


URL = '/api/v2/routine/'

REPLACEMENT = {
    'version': 1,
    'routine': {'name': 'Rebuilt', 'start': '2024-01-01', 'end': '2024-02-01'},
    'labels': [{'start_offset': 0, 'end_offset': 6, 'label': 'Deload'}],
    'days': [
        {
            'order': 1,
            'name': 'Push',
            'slots': [
                {
                    'order': 1,
                    'entries': [
                        {
                            'exercise': 1,
                            'order': 1,
                            'configs': {'weight': [{'iteration': 1, 'value': '80'}]},
                        }
                    ],
                }
            ],
        }
    ],
}


class RoutineRecoveryTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.routine = Routine.objects.get(pk=1)
        self.entry = SlotEntry.objects.filter(slot__day__routine=self.routine).first()
        self.session = WorkoutSession.objects.create(
            user=self.user, routine=self.routine, day=self.entry.slot.day, datetime_start=timezone.now()
        )
        self.log = WorkoutLog.objects.create(
            user=self.user,
            session=self.session,
            routine=self.routine,
            slot_entry=self.entry,
            exercise_id=1,
            repetitions=5,
            weight=100,
        )

    def history(self):
        """Completed history fields that no recovery operation may change"""
        log = WorkoutLog.objects.values().get(pk=self.log.pk)
        session = WorkoutSession.objects.values().get(pk=self.session.pk)
        return log, session

    def rev(self, pk=1):
        return self.api.get(f'{URL}{pk}/revision/').json()['revision']

    def post(self, path, data):
        return self.api.post(f'{URL}{path}', data, format='json')

    def restore(self, recovery_id, rev, key):
        return self.post(
            f'recoveries/{recovery_id}/restore/', {'expected_revision': rev, 'idempotency_key': key}
        )

    def test_trash_restore_roundtrip(self):
        before = self.history()
        rev = self.rev()
        r = self.post('1/trash/', {'expected_revision': rev, 'idempotency_key': 'k1'})
        self.assertEqual(r.status_code, 200, r.content)
        receipt = r.json()
        self.assertEqual(receipt['operation'], 'trash')

        # Idempotent repeat, then key reuse for a different request
        self.assertEqual(self.post('1/trash/', {'expected_revision': rev, 'idempotency_key': 'k1'}).json(), receipt)
        self.assertEqual(self.post('1/trash/', {'expected_revision': 'x', 'idempotency_key': 'k1'}).status_code, 409)

        ids = [r['id'] for r in self.api.get(URL).json()['results']]
        self.assertNotIn(1, ids)
        trashed = [r['id'] for r in self.api.get(f'{URL}?trashed=true').json()['results']]
        self.assertEqual(trashed, [1])
        self.assertEqual(self.api.get(f'{URL}1/').json()['deleted_at'][:4], str(timezone.now().year))
        self.assertEqual(self.api.patch(f'{URL}1/', {'name': 'x'}, format='json').json()['code'], 'routine_trashed')

        listed = self.api.get(f'{URL}recoveries/?routine=1').json()['results']
        self.assertEqual(listed[0]['recovery_id'], receipt['recovery_id'])
        self.assertTrue(listed[0]['restorable'])

        self.assertEqual(self.restore(receipt['recovery_id'], 'stale', 'r1').json()['code'], 'stale_revision')
        r = self.restore(receipt['recovery_id'], self.rev(), 'r1')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(Routine.objects.get(pk=1).deleted_at)
        self.assertEqual(r.json()['revision'], rev)
        self.assertEqual(self.history(), before)

    def test_legacy_delete_trashes(self):
        before = self.history()
        first = self.api.delete(f'{URL}1/')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.api.delete(f'{URL}1/').json(), first.json())
        self.assertTrue(Routine.objects.filter(pk=1, deleted_at__isnull=False).exists())
        self.assertEqual(self.history(), before)

    def test_other_users_cannot_see_or_restore(self):
        receipt = self.post('1/trash/', {'expected_revision': self.rev(), 'idempotency_key': 'k'}).json()
        other = APIClient()
        other.force_authenticate(User.objects.get(username='test'))
        self.assertEqual(other.get(f'{URL}1/revision/').status_code, 404)
        self.assertEqual(other.get(f'{URL}recoveries/').json()['count'], 0)
        r = other.post(f'{URL}recoveries/{receipt["recovery_id"]}/restore/', {'expected_revision': 'x', 'idempotency_key': 'o'}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_expired_recovery(self):
        receipt = self.post('1/trash/', {'expected_revision': self.rev(), 'idempotency_key': 'k'}).json()
        later = timezone.now() + routine_recovery.RECOVERY_WINDOW
        with patch.object(routine_recovery, '_now', return_value=later):
            r = self.restore(receipt['recovery_id'], self.rev(), 'r')
            self.assertEqual(r.status_code, 410)
            self.assertEqual(r.json()['code'], 'recovery_expired')
            self.assertEqual(self.api.get(f'{URL}recoveries/').json()['count'], 0)
        self.assertEqual(self.restore(receipt['recovery_id'], self.rev(), 'r2').status_code, 404)
        # Trashed routines are never purged
        self.assertTrue(Routine.objects.filter(pk=1).exists())

    def test_recorded_edit_undo_restores_by_pk(self):
        rev = self.rev()
        day = Day.objects.filter(routine=self.routine).exclude(pk=self.entry.slot.day_id).first()
        r = self.api.delete(f'/api/v2/day/{day.pk}/')
        self.assertEqual(r.status_code, 204)
        recovery_id = r['X-Routine-Recovery-Id']
        self.assertFalse(Day.objects.filter(pk=day.pk).exists())

        # A later edit blocks the undo until it is undone (LIFO)
        r2 = self.api.patch(f'{URL}1/', {'name': 'Renamed'}, format='json')
        self.assertEqual(self.restore(recovery_id, self.rev(), 'a').json()['code'], 'restore_conflict')
        self.assertEqual(self.restore(r2['X-Routine-Recovery-Id'], self.rev(), 'b').status_code, 200)

        r = self.restore(recovery_id, self.rev(), 'c')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(Day.objects.filter(pk=day.pk).exists())
        self.assertEqual(self.rev(), rev)

    def test_deleting_history_referenced_plan_rows_fails(self):
        before = self.history()
        rev = self.rev()
        r = self.api.delete(f'/api/v2/slot-entry/{self.entry.pk}/')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['code'], 'history_referenced')
        self.assertEqual(self.rev(), rev)
        self.assertEqual(RoutineRecovery.objects.count(), 0)
        self.assertEqual(self.history(), before)

    def test_noop_write_records_nothing(self):
        r = self.api.patch(f'{URL}1/', {'name': self.routine.name}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('X-Routine-Recovery-Id', r)
        self.assertEqual(RoutineRecovery.objects.count(), 0)

    def test_rebuild_preview_confirm_and_undo(self):
        before = self.history()
        rev = self.rev()
        bad = self.post('1/rebuild-preview/', {'expected_revision': rev, 'replacement': {**REPLACEMENT, 'days': [{'name': 'x', 'bogus': 1}]}}).json()
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['errors'][0]['path'], 'days[0].bogus')

        preview = self.post('1/rebuild-preview/', {'expected_revision': rev, 'replacement': REPLACEMENT}).json()
        self.assertTrue(preview['ok'], preview)
        self.assertEqual(preview['diff']['after']['entries'], 1)
        self.assertEqual(
            self.post('1/rebuild/', {'expected_revision': rev, 'replacement': REPLACEMENT, 'plan_hash': 'x', 'idempotency_key': 'b'}).json()['code'],
            'plan_changed',
        )
        r = self.post('1/rebuild/', {'expected_revision': rev, 'replacement': REPLACEMENT, 'plan_hash': preview['plan_hash'], 'idempotency_key': 'b2'})
        self.assertEqual(r.status_code, 200, r.content)
        receipt = r.json()
        new = Routine.objects.get(pk=receipt['replacement_routine_id'])
        self.assertEqual(new.days.get().slots.get().entries.get().weightconfig_set.get().value, 80)
        self.assertEqual(Routine.objects.get(pk=1).replaced_by_id, new.pk)
        self.assertEqual(self.history(), before)

        r = self.restore(receipt['previous_recovery_id'], self.rev(new.pk), 'u')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(Routine.objects.get(pk=1).deleted_at)
        self.assertIsNotNone(Routine.objects.get(pk=new.pk).deleted_at)
        self.assertEqual(self.rev(), rev)
        self.assertEqual(self.history(), before)

    def test_restore_boundary_is_exactly_14_days(self):
        receipt = self.post('1/trash/', {'expected_revision': self.rev(), 'idempotency_key': 'k'}).json()
        row = RoutineRecovery.objects.get(pk=receipt['recovery_id'])
        self.assertEqual(row.expires_at - row.created_at, datetime.timedelta(hours=14 * 24))
        rev = self.rev()
        tick = datetime.timedelta(microseconds=1)
        with patch.object(routine_recovery, '_now', return_value=row.expires_at):
            self.assertEqual(self.restore(receipt['recovery_id'], rev, 'late').status_code, 410)
        with patch.object(routine_recovery, '_now', return_value=row.expires_at - tick):
            self.assertEqual(self.restore(receipt['recovery_id'], rev, 'in-time').status_code, 200)

    def test_trashed_routines_leave_active_consumers(self):
        other = User.objects.get(username='test')
        routine_recovery.legacy_delete(other, 3)
        self.client.login(username='test', password='testtest')
        self.assertEqual(self.client.get(reverse('manager:routine:ical', kwargs={'pk': 3})).status_code, 404)

        routine_recovery.legacy_delete(self.user, 1)
        occurrences = gym_occurrences(self.user, datetime.date(2024, 3, 1), datetime.date(2024, 6, 1))
        self.assertEqual(occurrences, [])
        # Owner history stays readable
        self.assertEqual(self.api.get(f'/api/v2/workoutsession/{self.session.pk}/').status_code, 200)
        self.assertEqual(self.api.get(f'/api/v2/workoutlog/{self.log.pk}/').status_code, 200)

    def test_public_templates_grant_no_mutation(self):
        rev = self.api.get(f'{URL}5/revision/')
        self.assertEqual(rev.status_code, 404)
        self.assertEqual(self.post('5/trash/', {'expected_revision': 'x', 'idempotency_key': 'p'}).status_code, 404)
        self.assertIn(self.api.delete(f'{URL}5/').status_code, (403, 404))
        self.assertIsNone(Routine.objects.get(pk=5).deleted_at)

        routine_recovery.legacy_delete(User.objects.get(pk=5), 5)
        self.assertEqual(self.api.get('/api/v2/public-templates/').json()['count'], 0)
        self.assertEqual(self.api.get(f'{URL}5/').status_code, 404)

    def test_config_edit_undo_restores_value_and_pk(self):
        config = WeightConfig.objects.create(slot_entry=self.entry, iteration=99, value=50)
        rev = self.rev()
        r = self.api.patch(f'/api/v2/weight-config/{config.pk}/', {'value': 60}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.restore(r['X-Routine-Recovery-Id'], self.rev(), 'c').status_code, 200)
        self.assertEqual(WeightConfig.objects.get(pk=config.pk).value, 50)
        self.assertEqual(self.rev(), rev)

    def test_failures_roll_back_everything(self):
        rev = self.rev()
        with patch.object(routine_recovery, '_record', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                routine_recovery.trash(self.user, 1, rev, 'boom')
        self.assertIsNone(Routine.objects.get(pk=1).deleted_at)

        plan_hash = routine_recovery.preview(self.user, 1, rev, REPLACEMENT)['plan_hash']
        count = Routine.objects.count()
        with patch.object(routine_recovery, '_save_graph', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                routine_recovery.rebuild(self.user, 1, rev, REPLACEMENT, plan_hash, 'boom2')
        self.assertEqual(Routine.objects.count(), count)
        self.assertEqual(self.rev(), rev)
        self.assertEqual(RoutineRecovery.objects.count(), 0)

    def test_unsupported_replacements_fail_before_writes(self):
        rev = self.rev()
        entry = REPLACEMENT['days'][0]['slots'][0]['entries'][0]
        cases = {
            'replacement.version': {**REPLACEMENT, 'version': 2},
            'days[0].slots[0].entries[0].class_name': {
                **REPLACEMENT,
                'days': [{'name': 'x', 'slots': [{'entries': [{**entry, 'class_name': 'nope'}]}]}],
            },
            'days[0].slots[0].entries[0].configs.weight[0].requirements': {
                **REPLACEMENT,
                'days': [{'name': 'x', 'slots': [{'entries': [{**entry, 'configs': {'weight': [{'iteration': 1, 'value': 1, 'requirements': {'rules': ['bad']}}]}}]}]}],
            },
        }
        count = Routine.objects.count()
        for path, replacement in cases.items():
            result = routine_recovery.preview(self.user, 1, rev, replacement)
            self.assertFalse(result['ok'])
            self.assertIn(path, [e['path'] for e in result['errors']])
            r = self.post('1/rebuild/', {'expected_revision': rev, 'replacement': replacement, 'plan_hash': 'x', 'idempotency_key': path})
            self.assertEqual(r.status_code, 400)
        self.assertEqual(Routine.objects.count(), count)

    def test_rebuild_undo_keeps_history_logged_since(self):
        rev = self.rev()
        plan_hash = routine_recovery.preview(self.user, 1, rev, REPLACEMENT)['plan_hash']
        receipt = routine_recovery.rebuild(self.user, 1, rev, REPLACEMENT, plan_hash, 'r')
        new = Routine.objects.get(pk=receipt['replacement_routine_id'])
        entry = SlotEntry.objects.get(slot__day__routine=new)
        session = WorkoutSession.objects.create(user=self.user, routine=new, day=entry.slot.day, datetime_start=timezone.now())
        log = WorkoutLog.objects.create(user=self.user, session=session, routine=new, slot_entry=entry, exercise_id=1, repetitions=3, weight=80)
        later = (WorkoutLog.objects.values().get(pk=log.pk), WorkoutSession.objects.values().get(pk=session.pk))

        self.assertEqual(self.restore(receipt['previous_recovery_id'], self.rev(new.pk), 'u').status_code, 200)
        self.assertEqual((WorkoutLog.objects.values().get(pk=log.pk), WorkoutSession.objects.values().get(pk=session.pk)), later)
        self.assertTrue(SlotEntry.objects.filter(pk=entry.pk).exists())

    def test_caches_reset_only_on_commit(self):
        with patch.object(routine_recovery, 'reset_routine_cache') as reset:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                routine_recovery.legacy_delete(self.user, 1)
            reset.assert_not_called()
            for callback in callbacks:
                callback()
            reset.assert_called()

    def test_cross_routine_moves_are_refused_before_any_write(self):
        before = self.history()
        rev = self.rev()
        day, slot = self.entry.slot.day, self.entry.slot
        other = Routine.objects.create(user=self.user, name='Other', start='2024-01-01', end='2024-02-01')
        other_slot = Slot.objects.create(day=Day.objects.create(routine=other, order=1), order=1)
        moves = (
            (f'/api/v2/day/{day.pk}/', {'routine': other.pk}),
            (f'/api/v2/slot/{slot.pk}/', {'day': other_slot.day_id}),
            (f'/api/v2/slot-entry/{self.entry.pk}/', {'slot': other_slot.pk}),
        )
        for url, body in moves:
            r = self.api.patch(url, body, format='json')
            self.assertEqual(r.status_code, 409, url)
            self.assertEqual(r.json()['code'], 'cross_routine_move')
        self.assertEqual(self.rev(), rev)
        self.assertFalse(RoutineRecovery.objects.exists())
        self.assertEqual(self.history(), before)
        self.assertEqual(Day.objects.get(pk=day.pk).routine_id, self.routine.pk)
        self.assertEqual(SlotEntry.objects.get(pk=self.entry.pk).slot.day.routine_id, self.routine.pk)

        # Reordering and moving within the routine stay possible and undoable
        target = Day.objects.filter(routine=self.routine).exclude(pk=day.pk).first()
        r = self.api.patch(f'/api/v2/slot/{slot.pk}/', {'day': target.pk, 'order': 7}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.restore(r['X-Routine-Recovery-Id'], self.rev(), 'u').status_code, 200)
        self.assertEqual(Slot.objects.get(pk=slot.pk).day_id, day.pk)
        self.assertEqual(self.rev(), rev)
        self.assertEqual(self.history(), before)

    def test_trainer_logged_in_as_member_gets_no_owner_rights(self):
        member, trainer = User.objects.get(username='test'), User.objects.get(username='trainer1')
        day = Day.objects.create(routine_id=2, order=1, name='Owner day')
        owner = APIClient()
        owner.force_authenticate(member)
        edit = owner.patch(f'/api/v2/day/{day.pk}/', {'name': 'Owner edit'}, format='json')
        self.assertEqual(edit.status_code, 200, edit.content)
        recovery_id = edit['X-Routine-Recovery-Id']
        routine_recovery.legacy_delete(member, 3)
        own = Routine.objects.create(user=trainer, name='Tpl', is_template=True, start='2024-01-01', end='2024-02-01')
        trashed = Routine.objects.create(user=trainer, name='Old', is_template=True, start='2024-01-01', end='2024-02-01')
        routine_recovery.legacy_delete(trainer, trashed.pk)
        state = lambda: (
            list(Routine.objects.order_by('pk').values()),
            list(Day.objects.order_by('pk').values()),
            list(RoutineRecovery.objects.order_by('pk').values_list('pk', 'restored_at')),
        )

        self.client.login(username='trainer1', password='trainer1trainer1')
        page = self.client.get(reverse('core:user:overview', kwargs={'pk': member.pk}))
        self.assertEqual([d['routine'].pk for d in page.context['routine_data']], [2, 4])
        self.assertEqual(self.client.post(reverse('core:user:trainer-login', args=[member.pk])).status_code, 302)
        self.assertEqual(self.client.session['trainer.identity'], trainer.pk)
        before = state()

        # Reads keep working, except the owner's recovery data and trashed rows
        self.assertEqual(self.client.get(f'{URL}2/').status_code, 200)
        self.assertEqual(self.client.get(f'{URL}{own.pk}/').status_code, 200)
        self.assertEqual(self.client.get(f'{URL}{trashed.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'{URL}{trashed.pk}/structure/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v2/day/{day.pk}/').status_code, 200)

        body = {'expected_revision': 'x', 'idempotency_key': 't', 'replacement': REPLACEMENT}
        json = 'application/json'
        refused = {
            'revision': self.client.get(f'{URL}2/revision/'),
            'recoveries': self.client.get(f'{URL}recoveries/'),
            'restore': self.client.post(f'{URL}recoveries/{recovery_id}/restore/', body, content_type=json),
            'trash': self.client.post(f'{URL}2/trash/', body, content_type=json),
            'rebuild-preview': self.client.post(f'{URL}2/rebuild-preview/', body, content_type=json),
            'rebuild': self.client.post(f'{URL}2/rebuild/', body, content_type=json),
            'routine patch': self.client.patch(f'{URL}2/', {'name': 'T'}, content_type=json),
            'routine delete': self.client.delete(f'{URL}2/'),
            'day create': self.client.post('/api/v2/day/', {'routine': 2, 'order': 2}, content_type=json),
            'day patch': self.client.patch(f'/api/v2/day/{day.pk}/', {'name': 'T'}, content_type=json),
            'day delete': self.client.delete(f'/api/v2/day/{day.pk}/'),
            'slot create': self.client.post('/api/v2/slot/', {'day': day.pk, 'order': 1}, content_type=json),
        }
        self.assertEqual({k: r.status_code for k, r in refused.items()}, dict.fromkeys(refused, 404))
        sync = self.client.patch(
            '/api/v2/upload-powersync-data', {'table': 'manager_routine', 'data': {'id': 2, 'name': 'T'}}, content_type=json
        )
        self.assertEqual(sync.json()['error'], 'Forbidden')
        self.assertEqual(state(), before)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks')
class RoutineRecoveryConcurrencyTestCase(BaseTestCase, TransactionTestCase):
    def test_concurrent_writes_serialize_on_the_routine(self):
        user = User.objects.get(username='admin')
        rev = routine_recovery.revision(Routine.objects.get(pk=1))
        barrier = threading.Barrier(2)
        results = []

        def trash(key):
            try:
                barrier.wait()
                results.append(routine_recovery.trash(user, 1, rev, key)['operation'])
            except routine_recovery.RecoveryError as e:
                results.append(e.code)
            finally:
                connection.close()

        threads = [threading.Thread(target=trash, args=(k,)) for k in ('a', 'b')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ['stale_revision', 'trash'])
        self.assertEqual(RoutineRecovery.objects.count(), 1)
