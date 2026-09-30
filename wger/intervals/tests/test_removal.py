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
from django.db import OperationalError
from django.db.models.query import QuerySet

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals.models import IntervalsEventLink
from wger.intervals.tests import test_push
from wger.intervals.tests.test_command import CONFIGURED, response
from wger.manager.models import Day, Routine, WorkoutLog, WorkoutSession


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
class IntervalsRemovePlannedTest(WgerTestCase):
    # Push through the real intervals-push-gym command (routine 1, admin).
    run_command = test_push.IntervalsPushGymTest.run_command
    apply = test_push.IntervalsPushGymTest.apply

    def setUp(self):
        super().setUp()
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

        WorkoutSession.objects.create(
            user=User.objects.get(username='admin'),
            routine_id=1,
            datetime_start=datetime.datetime.combine(
                self.link.date, datetime.time(12), datetime.timezone.utc
            ),
        )
        self.assert_refused('workout session is logged')
        WorkoutSession.objects.filter(datetime_start__date=self.link.date).delete()
        self.history = (WorkoutSession.objects.count(), self.history[1])

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
