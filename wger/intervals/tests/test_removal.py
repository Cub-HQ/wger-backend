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

"""intervals-remove-planned against the in-memory fake Intervals. Synthetic data, no network."""

# Standard Library
import datetime
import io
import json
from unittest import mock

# Django
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections, transaction
from django.db.models.query import QuerySet
from django.test import TransactionTestCase

# wger
from wger.core.tests.base_testcase import BaseTestCase
from wger.intervals import client
from wger.intervals.models import IntervalsEventLink
from wger.intervals.tests import test_push
from wger.intervals.tests.test_command import CONFIGURED, KEY, response
from wger.manager.models import Day, Routine, SlotEntry, WorkoutLog, WorkoutSession


class Fake(test_push.FakeIntervals):
    """Answers 404 for a missing event; `activities` are served for any window."""

    activities = ()

    def __call__(self, method, url, params=None, json=None, **kw):
        path = url.split('/api/v1/')[1]
        if path == 'athlete/0/activities':
            self.calls.append((method, path))
            return response(list(self.activities))
        if path.startswith('athlete/0/events/') and method in ('GET', 'DELETE'):
            if (
                int(path.rsplit('/', 1)[1]) not in self.events
                and (method, 'event') not in self.fail
            ):
                self.calls.append((method, path))
                return response({}, 404)
        return super().__call__(method, url, params=params, json=json, **kw)


def plan_graph():
    return (
        list(Routine.objects.filter(pk=1).values()),
        list(Day.objects.filter(routine_id=1).values()),
    )


@CONFIGURED
class IntervalsRemovePlannedTest(BaseTestCase, TransactionTestCase):
    """Autocommit (TransactionTestCase), as in production: apply refuses to
    run inside a transaction, and its tombstone commit is what is observed."""

    # Push through the real intervals-push-gym command (routine 1, admin).
    run_command = test_push.IntervalsPushGymTest.run_command
    apply = test_push.IntervalsPushGymTest.apply

    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.fake = Fake()
        self.apply(self.fake)
        self.link = IntervalsEventLink.objects.order_by('date').first()
        self.key, self.event_id = self.link.external_id, self.link.intervals_event_id
        self.others = {k: dict(v) for k, v in self.fake.events.items() if k != self.event_id}
        self.graph = plan_graph()
        self.history = (WorkoutSession.objects.count(), WorkoutLog.objects.count())

    def tearDown(self):
        self.assertEqual((WorkoutSession.objects.count(), WorkoutLog.objects.count()), self.history)
        super().tearDown()

    def local(self, day_offset, hour, minute=0):
        """An aware datetime at the given local time around the link's date."""
        date = self.link.date + datetime.timedelta(days=day_offset)
        return datetime.datetime.combine(
            date, datetime.time(hour, minute), self.user.userprofile.zone_info
        )

    def session(self, when, **fields):
        session = WorkoutSession.objects.create(user=self.user, datetime_start=when, **fields)
        self.history = (self.history[0] + 1, self.history[1])
        return session

    def log(self, session, **fields):
        log = WorkoutLog.objects.create(
            user=self.user, session=session, exercise_id=1, duration=600, **fields
        )
        self.history = (self.history[0], self.history[1] + 1)
        return log

    def remove(self, *extra):
        out = io.StringIO()
        args = ['--external-id', self.key, '--event-id', str(self.event_id), *extra]
        with mock.patch('wger.intervals.client.requests.request', self.fake):
            try:
                call_command('intervals-remove-planned', *args, stdout=out)
            finally:
                self.output = out.getvalue()
        return json.loads(self.output)

    def remove_approved(self):
        preview = self.remove()
        return self.remove(
            '--apply',
            '--removal-hash',
            preview['removal_hash'],
            '--approve',
            preview['approval_required'],
        )

    def assert_refused(self, message, *extra):
        writes = len(self.fake.writes())
        with self.assertRaisesMessage(CommandError, message):
            self.remove(*extra)
        self.assertEqual(len(self.fake.writes()), writes)
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'active')

    def assert_never_recreated(self):
        for flags in ((), ('--recreate-missing',), ('--recreate-missing', '--overwrite-mirror')):
            report = self.apply(self.fake, *flags)
            self.assertIn(self.key, [r['external_id'] for r in report['removed']])
            for action in ('create', 'recreate', 'adopt', 'update', 'delete', 'conflict'):
                self.assertNotIn(self.key, [i['external_id'] for i in report[action]], action)
        self.assertNotIn(self.key, [e['external_id'] for e in self.fake.events.values()])
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'removed')

    def assert_source_and_others_unchanged(self):
        self.assertEqual(plan_graph(), self.graph)
        self.assertEqual(
            {k: v for k, v in self.fake.events.items() if k != self.event_id}, self.others
        )

    def test_unchanged_owned_event_is_removed_once_and_never_pushed_again(self):
        preview = self.remove()
        self.assertEqual(self.fake.writes()[-1][0], 'POST')  # preview wrote nothing
        self.assertEqual((preview['action'], preview['verified_recovery']), ('delete', None))
        self.assertIn(self.key, preview['approval_required'])

        applied = self.remove(
            '--apply',
            '--removal-hash',
            preview['removal_hash'],
            '--approve',
            preview['approval_required'],
        )

        self.assertEqual(applied['ledger_state'], 'removed')
        self.assertEqual(self.fake.writes()[-1], ('DELETE', f'athlete/0/events/{self.event_id}'))
        self.assertNotIn(self.event_id, self.fake.events)
        row = IntervalsEventLink.objects.get(pk=self.link.pk)
        kept = (
            'external_id',
            'intervals_event_id',
            'pushed_hash',
            'pushed_at',
            'routine_id',
            'day_id',
            'date',
        )
        self.assertEqual([getattr(row, f) for f in kept], [getattr(self.link, f) for f in kept])
        self.assertEqual(row.removal['event']['id'], self.event_id)
        self.assertEqual(
            (row.removal['prior_state'], row.removal['readback']), ('active', 'absent')
        )
        self.assert_source_and_others_unchanged()
        self.assert_never_recreated()
        with self.assertRaisesMessage(CommandError, 'is removed; nothing to remove'):
            self.remove()

    def test_apply_needs_the_exact_approval_and_a_current_hash(self):
        preview = self.remove()
        base = ['--apply', '--removal-hash', preview['removal_hash']]
        self.assert_refused('irreversible approval', *base)
        self.assert_refused('irreversible approval', *base, '--approve', 'yes')
        other = preview['approval_required'].replace(preview['removal_hash'], '0' * 64)
        self.assert_refused('irreversible approval', *base, '--approve', other)

        self.fake.events[self.event_id]['updated'] = '2030-01-01T00:00:00Z'
        self.assert_refused(
            'changed since the preview', *base, '--approve', preview['approval_required']
        )
        self.assertIn(self.event_id, self.fake.events)

    def test_edited_paired_or_non_matching_events_are_refused(self):
        event = self.fake.events[self.event_id]
        cases = [
            ({'description': 'my own notes'}, 'edited in Intervals since the push'),
            ({'paired_activity_id': 'i1'}, 'paired with an activity'),
            ({'type': 'Ride'}, 'is not WeightTraining'),
        ]
        for change, message in cases:
            with self.subTest(change):
                original = dict(event)
                event.update(change)
                self.assert_refused(message)
                event.clear()
                event.update(original)

        self.fake.events[99] = {**event, 'id': 99}
        self.assert_refused('is not the only event')
        del self.fake.events[99]

        self.fake.activities = [{'id': 'i1', 'icu_athlete_id': event['athlete_id']}]
        self.assert_refused('completed activity')
        self.fake.activities = ()

        self.session(self.local(0, 12), routine_id=self.link.routine_id)
        self.assert_refused('belong to this routine/day')

    def test_only_sessions_of_the_target_routine_or_day_on_its_local_day_block(self):
        # Stretch: another routine's completed session and log on the same date.
        stretch = Routine.objects.create(
            user=self.user, name='Stretch', start=self.link.date, end=self.link.date
        )
        unrelated = self.session(self.local(0, 7), routine=stretch, notes='stretch')
        self.log(unrelated, routine=stretch)
        # Target routine, but the next local day (still the date in the instance zone).
        self.user.userprofile.time_zone = 'Pacific/Auckland'
        self.user.userprofile.save()
        self.session(self.local(1, 0, 30), routine_id=self.link.routine_id)
        before = list(WorkoutSession.objects.filter(pk=unrelated.pk).values())
        logs_before = list(WorkoutLog.objects.filter(session=unrelated).values())

        self.assertEqual(self.remove_approved()['ledger_state'], 'removed')

        self.assertEqual(list(WorkoutSession.objects.filter(pk=unrelated.pk).values()), before)
        self.assertEqual(list(WorkoutLog.objects.filter(session=unrelated).values()), logs_before)

    def test_a_session_of_the_target_day_blocks_by_its_logs_at_local_midnight(self):
        self.user.userprofile.time_zone = 'Pacific/Auckland'
        self.user.userprofile.save()
        # Just after local midnight: the previous date in UTC and the instance zone.
        other = Routine.objects.create(
            user=self.user, name='Other', start=self.link.date, end=self.link.date
        )
        session = self.session(self.local(0, 0, 30), routine=other)
        entry = SlotEntry.objects.filter(slot__day_id=self.link.day_id).first()
        self.log(session, slot_entry=entry, repetitions=5, repetitions_unit_id=1)

        self.assert_refused('belong to this routine/day')

    def test_foreign_or_unowned_targets_are_refused(self):
        self.assert_refused('not 1', '--event-id', '1')  # argparse keeps the last value
        IntervalsEventLink.objects.filter(pk=self.link.pk).update(
            user=User.objects.get(username='test')
        )
        with self.assertRaisesMessage(CommandError, 'no ledger row'):
            self.remove()
        self.assertIn(self.event_id, self.fake.events)

    def test_failed_delete_keeps_a_tombstone_and_resumes(self):
        self.fake.fail[('DELETE', 'event')] = 1

        with self.assertRaisesMessage(CommandError, 'preview again to resume'):
            self.remove_approved()

        self.assertIn(self.event_id, self.fake.events)
        row = IntervalsEventLink.objects.get(pk=self.link.pk)
        self.assertEqual(row.state, 'removing')
        report = self.apply(self.fake, '--recreate-missing', '--overwrite-mirror')
        self.assertEqual(
            report['removed'],
            [{'external_id': self.key, 'state': 'removing', 'remote_present': True}],
        )
        self.assertIn(self.event_id, self.fake.events)  # the push never deletes it either

        self.assertEqual(self.remove_approved()['ledger_state'], 'removed')
        self.assertNotIn(self.event_id, self.fake.events)
        self.assertEqual(
            len(IntervalsEventLink.objects.get(pk=self.link.pk).removal['attempts']), 2
        )
        self.assert_never_recreated()

    def test_apply_refuses_inside_a_transaction_before_any_write(self):
        preview = self.remove()
        writes = len(self.fake.writes())
        with transaction.atomic(), self.assertRaisesMessage(CommandError, 'autocommit'):
            self.remove(
                '--apply',
                '--removal-hash',
                preview['removal_hash'],
                '--approve',
                preview['approval_required'],
            )
        self.assertEqual(len(self.fake.writes()), writes)
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'active')

    def test_tombstone_is_committed_before_the_delete_is_sent(self):
        link_pk = self.link.pk
        seen = []

        class Observes(Fake):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method == 'DELETE':
                    other = connections.create_connection('default')
                    try:
                        with other.cursor() as cursor:
                            cursor.execute(
                                'SELECT state FROM intervals_intervalseventlink WHERE id = %s',
                                [str(link_pk).replace('-', '')],
                            )
                            seen.append(cursor.fetchone())
                    finally:
                        other.close()
                return super().__call__(method, url, params=params, json=json, **kw)

        self.fake.__class__ = Observes
        self.remove_approved()
        self.assertEqual(seen, [('removing',)])

    def test_removed_is_reported_only_after_the_ledger_reads_back_removed(self):
        real = QuerySet.update
        calls = []

        def update(qs, **fields):
            calls.append(fields.get('state'))
            return 0 if fields.get('state') == 'removed' else real(qs, **fields)

        with (
            mock.patch.object(QuerySet, 'update', update),
            self.assertRaisesMessage(CommandError, 'was not recorded removed'),
        ):
            self.remove_approved()
        self.assertEqual(calls, ['removing', 'removed'])
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'removing')

    def test_not_found_is_a_typed_redacted_intervals_error(self):
        with (
            mock.patch('wger.intervals.client.requests.request', return_value=response({}, 404)),
            self.assertRaises(client.NotFound) as raised,
        ):
            client.get_event(KEY, f'{KEY}1')
        self.assertIsInstance(raised.exception, client.IntervalsError)
        self.assertIn('HTTP 404', str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))

    def test_readback_error_other_than_404_is_not_taken_as_absent(self):
        class ReadbackFails(Fake):
            deleted = False

            def __call__(self, method, url, params=None, json=None, **kw):
                if method == 'GET' and self.deleted and '/events/' in url:
                    return response({}, 503)
                self.deleted |= method == 'DELETE'
                return super().__call__(method, url, params=params, json=json, **kw)

        self.fake.__class__ = ReadbackFails
        with self.assertRaisesMessage(CommandError, 'preview again to resume'):
            self.remove_approved()
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'removing')

    def test_uncertain_delete_is_confirmed_by_readback_without_a_second_delete(self):
        class LostResponse(Fake):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method == 'DELETE':
                    del self.events[int(url.rsplit('/', 1)[1])]
                    self.calls.append((method, url))
                    return response({}, 503)
                return super().__call__(method, url, params=params, json=json, **kw)

        self.fake.__class__ = LostResponse
        with self.assertRaises(CommandError):
            self.remove_approved()
        self.assertEqual(IntervalsEventLink.objects.get(pk=self.link.pk).state, 'removing')

        preview = self.remove()
        self.assertEqual((preview['action'], preview['approval_required']), ('tombstone', None))
        self.assertEqual(preview['event']['id'], self.event_id)
        deletes = [c for c in self.fake.writes() if c[0] == 'DELETE']
        self.remove('--apply', '--removal-hash', preview['removal_hash'])
        self.assertEqual([c for c in self.fake.writes() if c[0] == 'DELETE'], deletes)
        self.assert_source_and_others_unchanged()
        self.assert_never_recreated()

    def test_delete_that_answers_ok_but_leaves_the_event_is_not_recorded_removed(self):
        class IgnoresDelete(Fake):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method == 'DELETE':
                    self.calls.append((method, url))
                    return response(None)
                return super().__call__(method, url, params=params, json=json, **kw)

        self.fake.__class__ = IgnoresDelete
        with self.assertRaisesMessage(CommandError, 'still in Intervals'):
            self.remove_approved()

        row = IntervalsEventLink.objects.get(pk=self.link.pk)
        self.assertEqual(row.state, 'removing')
        self.assertNotIn('removed_at', row.removal)

    def test_tombstone_commit_failure_happens_before_any_delete(self):
        with (
            mock.patch.object(QuerySet, 'update', side_effect=OperationalError('db down')),
            self.assertRaises(CommandError),
        ):
            self.remove_approved()

        self.assertNotIn('DELETE', [m for m, _ in self.fake.writes()])
        self.assertIn(self.event_id, self.fake.events)
        row = IntervalsEventLink.objects.get(pk=self.link.pk)
        self.assertEqual((row.state, row.removal), ('active', None))
