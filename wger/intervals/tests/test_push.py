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

"""intervals-push-gym against an in-memory fake Intervals. No network."""

# Standard Library
import io
import json
from unittest import mock

# Django
from django.core.management import call_command
from django.core.management.base import CommandError

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals.models import IntervalsEventLink
from wger.intervals.tests.test_command import CONFIGURED, KEY, response
from wger.intervals.tests.test_planning import ATHLETE
from wger.manager.models import Routine, WorkoutLog, WorkoutSession


# Routine 1 (admin, fixture) runs 2024-03-01..2024-06-01 with three training days.
OLDEST, NEWEST = '2024-03-04', '2024-03-10'


class FakeIntervals:
    """Stores events. `fail[(method, what)] = n` answers the n-th such call with
    HTTP 503; `what` is the path, or 'event' for a single-event path."""

    def __init__(self, events=(), permission='WRITE'):
        self.events = {e['id']: dict(e) for e in events}
        self.permission = permission
        self.next_id = 1000
        self.calls = []
        self.fail = {}

    def __call__(self, method, url, params=None, json=None, **kw):
        assert kw['auth'] == ('API_KEY', KEY) and kw['timeout']
        path = url.split('/api/v1/')[1]
        self.calls.append((method, path))
        what = 'event' if path.startswith('athlete/0/events/') else path
        if (method, what) in self.fail:
            self.fail[(method, what)] -= 1
            if not self.fail[(method, what)]:
                del self.fail[(method, what)]
                return response({}, 503)
        if path == 'athlete/0':
            return response({'id': ATHLETE, 'icu_permission': self.permission})
        if path == 'athlete/0/activities':
            return response([])
        if path == 'athlete/0/events' and method == 'GET':
            return response(list(self.events.values()))
        if method == 'POST':
            self.next_id += 1
            self.events[self.next_id] = {**json, 'id': self.next_id, 'athlete_id': ATHLETE}
            return response({'id': self.next_id})
        event_id = int(path.rsplit('/', 1)[1])
        if method == 'PUT':
            self.events[event_id].update(json)
        elif method == 'DELETE':
            del self.events[event_id]
            return response(None)
        return response(self.events[event_id])

    def writes(self):
        return [(m, p) for m, p in self.calls if m != 'GET']


@CONFIGURED
class IntervalsPushGymTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.history = (WorkoutSession.objects.count(), WorkoutLog.objects.count())

    def tearDown(self):
        self.assertEqual((WorkoutSession.objects.count(), WorkoutLog.objects.count()), self.history)
        super().tearDown()

    def run_command(self, fake, *extra):
        out = io.StringIO()
        with mock.patch('wger.intervals.client.requests.request', fake):
            try:
                call_command(
                    'intervals-push-gym', '--oldest', OLDEST, '--newest', NEWEST, *extra, stdout=out
                )
            finally:
                self.output = out.getvalue()
        return json.loads(self.output)

    def apply(self, fake, *extra):
        plan_hash = self.run_command(fake, *extra)['plan_hash']
        return self.run_command(fake, *extra, '--apply', '--plan-hash', plan_hash)

    def shorten_routine(self, end):
        routine = Routine.objects.get(pk=1)
        routine.end = end
        routine.save()  # signals reset the cached date_sequence

    def test_preview_is_get_only_then_apply_pushes_once_and_rerun_is_a_no_op(self):
        fake = FakeIntervals()

        report = self.run_command(fake)

        self.assertEqual(fake.writes(), [])
        self.assertEqual(IntervalsEventLink.objects.count(), 0)
        created = report['create']
        self.assertTrue(created)
        self.assertTrue(all(c['external_id'].startswith('wger-gym:1:') for c in created))
        first = created[0]
        self.assertIn('Open in wger: http://localhost:8000/en/routine/1/view', first['description'])
        day = first['external_id'].rsplit(':', 1)[1]
        self.assertEqual(first['intervals_day_link'], f'https://intervals.icu/?s={day}&e={day}')

        applied = self.run_command(fake, '--apply', '--plan-hash', report['plan_hash'])

        self.assertEqual([m for m, _ in fake.writes()], ['POST'] * len(created))
        self.assertEqual(
            sorted(e['external_id'] for e in fake.events.values()),
            [c['external_id'] for c in created],
        )
        for pushed in fake.events.values():
            self.assertEqual((pushed['category'], pushed['type']), ('WORKOUT', 'WeightTraining'))
            self.assertNotIn('moving_time', pushed)
            self.assertNotIn('icu_training_load', pushed)
        self.assertEqual(len(applied['done']), len(created))
        self.assertEqual(
            IntervalsEventLink.objects.filter(user__username='admin').count(), len(created)
        )

        writes = len(fake.writes())
        again = self.apply(fake)
        self.assertEqual((again['create'], again['unchanged']), ([], len(created)))
        self.assertEqual(len(fake.writes()), writes)

    def test_failure_mid_run_keeps_earlier_ledger_rows_and_rerun_never_duplicates(self):
        fake = FakeIntervals()
        fake.fail[('POST', 'athlete/0/events')] = 2

        with self.assertRaisesMessage(CommandError, 'preview again to resume'):
            self.apply(fake)

        self.assertEqual(len(fake.events), 1)
        self.assertEqual(IntervalsEventLink.objects.count(), 1)
        self.assertEqual(json.loads(self.output)['failed']['action'], 'create')

        self.apply(fake)
        ids = [e['external_id'] for e in fake.events.values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(IntervalsEventLink.objects.count(), len(ids))

    def test_crash_after_post_before_ledger_save_adopts_instead_of_posting_again(self):
        fake = FakeIntervals()
        preview = self.run_command(fake)
        fake.fail[('GET', 'event')] = 1  # readback of the first POST fails

        with self.assertRaises(CommandError):
            self.run_command(fake, '--apply', '--plan-hash', preview['plan_hash'])
        self.assertEqual((len(fake.events), IntervalsEventLink.objects.count()), (1, 0))

        rerun = self.run_command(fake)
        self.assertEqual(len(rerun['adopt']), 1)
        self.assertEqual(len(rerun['create']), len(preview['create']) - 1)
        self.run_command(fake, '--apply', '--plan-hash', rerun['plan_hash'])
        self.assertEqual(len(fake.events), len(preview['create']))
        self.assertEqual(IntervalsEventLink.objects.count(), len(preview['create']))

    def test_apply_needs_write_permission_and_a_current_plan_hash(self):
        read_only = FakeIntervals(permission='READ')
        plan_hash = self.run_command(read_only)['plan_hash']
        with self.assertRaisesMessage(CommandError, 'WRITE permission'):
            self.run_command(read_only, '--apply', '--plan-hash', plan_hash)
        self.assertEqual(read_only.writes(), [])

        fake = FakeIntervals()
        stale = self.run_command(fake)['plan_hash']
        self.shorten_routine('2024-03-05')
        with self.assertRaisesMessage(CommandError, 'changed since the preview'):
            self.run_command(fake, '--apply', '--plan-hash', stale)
        self.assertEqual(fake.writes(), [])

    def test_edited_in_intervals_is_a_conflict_and_foreign_events_are_untouched(self):
        foreign = {'id': 1, 'athlete_id': ATHLETE, 'external_id': '', 'name': 'Ride'}
        fake = FakeIntervals(events=[foreign])
        self.apply(fake)
        edited = next(e for e in fake.events.values() if e['id'] != 1)
        edited['description'] = 'changed in Intervals'
        writes = len(fake.writes())

        report = self.apply(fake)

        self.assertEqual([c['external_id'] for c in report['conflict']], [edited['external_id']])
        self.assertEqual(len(fake.writes()), writes)
        self.assertEqual(fake.events[1], foreign)
        self.assertEqual(report['skipped']['foreign_events'], 1)

        overwritten = self.apply(fake, '--overwrite-mirror')
        self.assertEqual([u['external_id'] for u in overwritten['update']], [edited['external_id']])
        self.assertNotEqual(fake.events[edited['id']]['description'], 'changed in Intervals')

    def test_days_leaving_the_plan_delete_only_their_ledgered_events(self):
        fake = FakeIntervals()
        pushed = {d['external_id'] for d in self.apply(fake)['done']}
        self.shorten_routine('2024-03-05')

        report = self.apply(fake)

        gone = {d['external_id'] for d in report['delete']}
        kept = {e['external_id'] for e in fake.events.values()}
        self.assertTrue(gone and kept)
        self.assertEqual(gone | kept, pushed)
        self.assertTrue(all(k <= 'wger-gym:1:2024-03-05' for k in kept))
        states = IntervalsEventLink.objects.filter(external_id__in=gone).values_list(
            'state', flat=True
        )
        self.assertEqual(set(states), {'deleted'})
