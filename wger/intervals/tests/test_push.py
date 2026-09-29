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
import re
from unittest import mock

# Django
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import override_settings

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals import push
from wger.intervals.models import IntervalsEventLink
from wger.intervals.planning import PlanError
from wger.intervals.tests.test_command import CONFIGURED, KEY, response
from wger.intervals.tests.test_planning import ATHLETE
from wger.manager.models import Day, Routine, WorkoutLog, WorkoutSession


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
        day = first['external_id'].rsplit(':', 1)[1]
        link = re.search(r'Open in wger: (\S+)', first['description']).group(1)
        prefix = 'http://localhost:8000/en/routine/1/view?day='
        self.assertTrue(link.startswith(prefix) and link.endswith(f'&date={day}'), link)
        day_id = int(link[len(prefix) :].split('&')[0])
        self.assertTrue(Day.objects.filter(pk=day_id, routine_id=1, is_rest=False).exists())
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
        # The failed POST left only a pending row (no event id); a rerun creates it once.
        self.assertEqual(IntervalsEventLink.objects.exclude(intervals_event_id=None).count(), 1)
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
        self.assertEqual(len(fake.events), 1)
        self.assertEqual(IntervalsEventLink.objects.exclude(intervals_event_id=None).count(), 0)

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

    def rename_first_day(self):
        day = Day.objects.filter(routine_id=1, is_rest=False).order_by('pk').first()
        day.name = 'Renamed'
        day.save()

    def failing_ledger_save(self, nth):
        """Patch the ledger so its nth update_or_create raises a DB error."""
        real, calls = IntervalsEventLink.objects.update_or_create, []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == nth:
                raise OperationalError('connection lost')
            return real(*a, **kw)

        return mock.patch.object(IntervalsEventLink.objects, 'update_or_create', flaky)

    def test_db_error_after_remote_writes_keeps_earlier_ledger_rows_and_rerun_adopts(self):
        fake = FakeIntervals()
        plan_hash = self.run_command(fake)['plan_hash']

        # Save 1-2: pending + record of the 1st POST; save 4 records the 2nd POST.
        with self.failing_ledger_save(4), self.assertRaisesMessage(CommandError, 'connection lost'):
            self.run_command(fake, '--apply', '--plan-hash', plan_hash)

        self.assertEqual(len(fake.events), 2)
        self.assertEqual(IntervalsEventLink.objects.exclude(intervals_event_id=None).count(), 1)
        rerun = self.run_command(fake)
        self.assertEqual(len(rerun['adopt']), 1)
        self.run_command(fake, '--apply', '--plan-hash', rerun['plan_hash'])
        ids = [e['external_id'] for e in fake.events.values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.apply(fake)['unchanged'], len(ids))

    def test_lost_update_ledger_is_repaired_by_adopt_not_a_permanent_conflict(self):
        fake = FakeIntervals()
        self.apply(fake)
        self.rename_first_day()
        plan_hash = self.run_command(fake)['plan_hash']

        with self.failing_ledger_save(1), self.assertRaises(CommandError):
            self.run_command(fake, '--apply', '--plan-hash', plan_hash)

        rerun = self.run_command(fake)
        self.assertEqual((rerun['conflict'], len(rerun['adopt'])), ([], 1))
        writes = len(fake.writes())
        self.run_command(fake, '--apply', '--plan-hash', rerun['plan_hash'])
        # Adopt only records; the one PUT per remaining update is all that is written.
        self.assertEqual(len(fake.writes()) - writes, len(rerun['update']))
        again = self.run_command(fake)
        self.assertEqual(again['adopt'] + again['conflict'] + again['update'], [])

    def test_readback_differing_on_any_field_stops_and_never_loops(self):
        class Normalizing(FakeIntervals):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method in ('POST', 'PUT') and json:
                    json = {**json, 'description': json['description'][:10]}
                return super().__call__(method, url, params=params, json=json, **kw)

        fake = Normalizing()

        with self.assertRaisesMessage(CommandError, 'stored with different description'):
            self.apply(fake)

        self.assertEqual(len(fake.events), 1)
        again = self.run_command(fake)
        self.assertEqual([c['reason'] for c in again['conflict']], ['edited in Intervals'])
        self.assertEqual(again['update'] + again['adopt'], [])

    def test_whitespace_only_normalisation_is_not_a_mismatch(self):
        class Trimming(FakeIntervals):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method in ('POST', 'PUT') and json:
                    json = {**json, 'description': json['description'].replace('\n', '\r\n')}
                return super().__call__(method, url, params=params, json=json, **kw)

        fake = Trimming()
        applied = self.apply(fake)

        self.assertIsNone(applied['failed'])
        self.assertEqual(self.apply(fake)['unchanged'], len(applied['done']))

    def test_delete_refuses_a_different_event_carrying_our_external_id(self):
        fake = FakeIntervals()
        self.apply(fake)
        link = IntervalsEventLink.objects.order_by('-date').first()
        ours = fake.events.pop(link.intervals_event_id)
        fake.events[7] = {**ours, 'id': 7, 'name': 'Josh own note', 'description': 'mine'}
        self.shorten_routine('2024-03-05')

        report = self.apply(fake)

        self.assertIn(
            {'external_id': link.external_id, 'reason': 'not the ledger event'},
            [{k: c[k] for k in ('external_id', 'reason')} for c in report['conflict']],
        )
        self.assertIn(7, fake.events)

    def test_delete_refuses_an_event_edited_in_intervals_unless_overwrite(self):
        fake = FakeIntervals()
        self.apply(fake)
        link = IntervalsEventLink.objects.order_by('-date').first()
        fake.events[link.intervals_event_id]['description'] = 'my notes from the session'
        self.shorten_routine('2024-03-05')

        report = self.apply(fake)

        self.assertEqual([c['external_id'] for c in report['conflict']], [link.external_id])
        self.assertIn(link.intervals_event_id, fake.events)
        self.apply(fake, '--overwrite-mirror')
        self.assertNotIn(link.intervals_event_id, fake.events)

    def test_edit_made_after_the_fetch_is_not_overwritten_or_deleted(self):
        class ConcurrentEdit(FakeIntervals):
            armed = False

            def __call__(self, method, url, params=None, json=None, **kw):
                if (
                    self.armed
                    and method == 'GET'
                    and url.split('/')[-1].isdigit()
                    and 'events/' in url
                ):
                    self.events[int(url.rsplit('/', 1)[1])]['description'] = 'edited now'
                return super().__call__(method, url, params=params, json=json, **kw)

        fake = ConcurrentEdit()
        self.apply(fake)
        self.rename_first_day()
        plan_hash = self.run_command(fake)['plan_hash']
        writes = len(fake.writes())
        fake.armed = True

        with self.assertRaisesMessage(CommandError, 'changed in Intervals during this run'):
            self.run_command(fake, '--apply', '--plan-hash', plan_hash)

        self.assertEqual(len(fake.writes()), writes)
        self.assertIn('edited now', [e['description'] for e in fake.events.values()])

    def test_normalised_create_plus_db_error_stays_owned_not_orphaned(self):
        class Normalizing(FakeIntervals):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method in ('POST', 'PUT') and json:
                    json = {**json, 'name': json['name'].upper()}
                return super().__call__(method, url, params=params, json=json, **kw)

        fake = Normalizing()
        plan_hash = self.run_command(fake)['plan_hash']
        with self.failing_ledger_save(2), self.assertRaises(CommandError):
            self.run_command(fake, '--apply', '--plan-hash', plan_hash)
        [event] = fake.events.values()

        rerun = self.run_command(fake)

        self.assertEqual([a['external_id'] for a in rerun['adopt']], [event['external_id']])
        self.assertNotIn(event['external_id'], [c['external_id'] for c in rerun['create']])

    def test_dropped_external_id_is_never_recreated_or_orphaned(self):
        """U2 unverified: if Intervals drops external_id, repeated
        --recreate-missing runs must neither duplicate nor lose the event."""

        class DropsExternalId(FakeIntervals):
            def __call__(self, method, url, params=None, json=None, **kw):
                if method in ('POST', 'PUT') and json:
                    json = {k: v for k, v in json.items() if k != 'external_id'}
                return super().__call__(method, url, params=params, json=json, **kw)

        fake = DropsExternalId()
        with self.assertRaisesMessage(CommandError, 'different external_id'):
            self.apply(fake)
        [first] = fake.events
        key = IntervalsEventLink.objects.get(intervals_event_id=first).external_id

        for flags in (('--recreate-missing',), ('--recreate-missing',), ('--overwrite-mirror',)):
            report = self.run_command(fake, *flags)
            self.assertIn(
                (key, 'ledger event lost external_id'),
                [(c['external_id'], c['reason']) for c in report['conflict']],
            )
            self.assertNotIn(key, [r['external_id'] for r in report['recreate']])
            with self.assertRaises(CommandError):  # other days still stop on the dropped id
                self.run_command(fake, *flags, '--apply', '--plan-hash', report['plan_hash'])

        day = fake.events[first]['start_date_local']
        self.assertEqual(
            [e['id'] for e in fake.events.values() if e['start_date_local'] == day], [first]
        )
        self.assertEqual(IntervalsEventLink.objects.get(external_id=key).intervals_event_id, first)

        self.shorten_routine('2024-03-03')  # every day leaves the plan
        gone = self.run_command(fake)
        self.assertIn(key, [c['external_id'] for c in gone['conflict']])
        self.assertEqual(gone['forget'] + gone['delete'], [])

    def test_service_entrypoints_bind_the_user_to_the_configured_athlete(self):
        fake, other = FakeIntervals(), User.objects.get(username='test')
        with mock.patch('wger.intervals.client.requests.request', fake):
            plan_hash = push.preview(User.objects.get(username='admin'), OLDEST, NEWEST)[
                'plan_hash'
            ]
            for call in (
                lambda: push.preview(other, OLDEST, NEWEST),
                lambda: push.apply(other, OLDEST, NEWEST, plan_hash),
            ):
                with self.assertRaisesMessage(PlanError, 'not the one paired'):
                    call()
            with override_settings(INTERVALS_WGER_USERNAME=''), self.assertRaises(PlanError):
                push.apply(User.objects.get(username='admin'), OLDEST, NEWEST, plan_hash)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(IntervalsEventLink.objects.count(), 0)
