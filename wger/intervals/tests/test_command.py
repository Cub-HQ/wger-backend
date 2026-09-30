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

"""intervals-sync command and the client's read path. requests is mocked; no network."""

# Standard Library
import io
import json
from unittest import mock

# Django
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

# Third Party
import requests

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals.models import EnduranceEntry
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST, ride
from wger.manager.models import WorkoutLog, WorkoutSession


KEY = 'synthetic-secret-key-123'
CONFIGURED = override_settings(
    INTERVALS_API_KEY=KEY, INTERVALS_ATHLETE_ID=ATHLETE, INTERVALS_WGER_USERNAME='admin'
)


def response(body, status=200, headers=None):
    fake = mock.Mock(status_code=status, headers=headers or {})
    if isinstance(body, Exception):
        fake.json.side_effect = body
    else:
        fake.json.return_value = body
    return fake


def intervals(athlete=None, activities=None):
    """A fake requests.request serving one athlete window (GET only)."""
    served = {
        'athlete/0': response(athlete or {'id': ATHLETE}),
        'athlete/0/activities': response([ride()] if activities is None else activities),
    }

    def serve(method, url, **kw):
        assert method == 'GET', method
        path = url.split('/api/v1/')[1]
        assert path in served, f'Unexpected Intervals path: {path}'
        return served[path]

    return mock.Mock(side_effect=serve)


@CONFIGURED
class IntervalsSyncCommandTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.history = (
            list(WorkoutSession.objects.order_by('pk').values()),
            list(WorkoutLog.objects.order_by('pk').values()),
        )

    def tearDown(self):
        self.assertEqual(
            (
                list(WorkoutSession.objects.order_by('pk').values()),
                list(WorkoutLog.objects.order_by('pk').values()),
            ),
            self.history,
        )
        super().tearDown()

    def run_command(self, get, *extra):
        out = io.StringIO()
        with mock.patch('wger.intervals.client.requests.request', get):
            call_command(
                'intervals-sync', '--oldest', OLDEST, '--newest', NEWEST, *extra, stdout=out
            )
        return json.loads(out.getvalue())

    def test_preview_is_get_only_and_writes_nothing_then_apply_writes_it(self):
        get = intervals()

        report = self.run_command(get)

        self.assertEqual(EnduranceEntry.objects.count(), 0)
        self.assertEqual(report['operation'], 'preview (no writes)')
        self.assertEqual((report['mode'], report['selected']), ('window', None))
        self.assertEqual([r['intervals_id'] for r in report['create']], ['i900001'])
        calls = [(c.args[1], c.kwargs) for c in get.call_args_list]
        self.assertEqual(
            [url.rsplit('/v1/', 1)[1] for url, _ in calls],
            ['athlete/0', 'athlete/0/activities'],
        )
        self.assertEqual(calls[1][1]['params'], {'oldest': OLDEST, 'newest': f'{NEWEST}T23:59:59'})
        self.assertTrue(all(kw['auth'] == ('API_KEY', KEY) and kw['timeout'] for _, kw in calls))

        applied = self.run_command(intervals(), '--apply', '--plan-hash', report['plan_hash'])
        self.assertEqual(applied['plan_hash'], report['plan_hash'])
        self.assertEqual(applied['operation'], 'apply')
        self.assertEqual(EnduranceEntry.objects.filter(user__username='admin').count(), 1)

        again = self.run_command(intervals())
        self.assertEqual((again['create'], again['update'], again['unchanged']), ([], [], 1))

    def test_apply_refuses_without_or_with_stale_hash(self):
        with self.assertRaisesMessage(CommandError, '--plan-hash'):
            self.run_command(intervals(), '--apply')
        stale = self.run_command(intervals())['plan_hash']
        with self.assertRaisesMessage(CommandError, 'changed since the preview'):
            self.run_command(
                intervals(activities=[ride(icu_training_load=1)]), '--apply', '--plan-hash', stale
            )
        self.assertEqual(EnduranceEntry.objects.count(), 0)

    def test_exact_imports_selected_ride_only_and_reapply_is_noop(self):
        activities = [
            ride(),
            ride(id='i2'),
            ride(id='i3', type='WeightTraining'),
            ride(id='i4', external_id='wger-gym-session:1:42'),
        ]
        report = self.run_command(intervals(activities=activities), '--activity', 'i900001')
        self.assertEqual((report['mode'], report['selected']), ('exact', 'i900001'))
        self.assertEqual(report['operation'], 'preview (no writes)')
        self.assertEqual(report['skipped'], {'echo': 1, 'weight_training': 1})
        self.assertFalse(EnduranceEntry.objects.exists())
        [entry] = report['create']
        self.assertEqual(entry['intervals_id'], 'i900001')
        applied = self.run_command(
            intervals(activities=activities),
            '--activity',
            'i900001',
            '--apply',
            '--plan-hash',
            report['plan_hash'],
        )
        self.assertEqual(applied['operation'], 'apply')
        stored = EnduranceEntry.objects.get()
        self.assertEqual(stored.intervals_id, 'i900001')
        self.assertEqual(
            (
                stored.moving_time_s,
                stored.elapsed_time_s,
                stored.distance_m,
                stored.training_load,
                stored.avg_hr,
            ),
            (14400, 15320, 112340.5, 187, 131),
        )
        self.user_login('admin')
        entry = self.client.get(f'/api/v2/endurance-entry/{stored.pk}/').json()
        self.assertEqual(entry['link'], 'https://intervals.icu/activities/i900001')
        self.assertTrue(entry['link_exact'])
        snapshot = list(EnduranceEntry.objects.values())
        again = self.run_command(intervals(activities=activities), '--activity', 'i900001')
        self.assertEqual((again['create'], again['update'], again['unchanged']), ([], [], 1))
        self.run_command(
            intervals(activities=activities),
            '--activity',
            'i900001',
            '--apply',
            '--plan-hash',
            again['plan_hash'],
        )
        self.assertEqual(list(EnduranceEntry.objects.values()), snapshot)

    def test_exact_refuses_unavailable_or_untrustworthy_batches_without_writes(self):
        for activities in (
            [],
            [ride(type='WeightTraining')],
            [ride(external_id='wger-gym-session:1:42')],
            [ride(moving_time=None, elapsed_time=None)],
            [ride(), ride(id='i2', icu_athlete_id='i9999')],
            [ride(), ride(id='i2', start_date_local='2040-06-27T00:00:00')],
            [ride(), ride(id='i2'), ride(id='i2')],
        ):
            for operation in ((), ('--apply', '--plan-hash', 'f' * 64)):
                with self.subTest(activities=activities, operation=operation):
                    with self.assertRaises(CommandError):
                        self.run_command(
                            intervals(activities=activities), '--activity', 'i900001', *operation
                        )
                    self.assertFalse(EnduranceEntry.objects.exists())

    def test_cross_mode_apply_refuses_without_writes(self):
        window = self.run_command(intervals())
        exact = self.run_command(intervals(), '--activity', 'i900001')
        self.assertEqual(window['create'], exact['create'])
        self.assertNotEqual(window['plan_hash'], exact['plan_hash'])
        for digest, selector in (
            (window['plan_hash'], ('--activity', 'i900001')),
            (exact['plan_hash'], ()),
        ):
            with self.subTest(selector=selector), self.assertRaises(CommandError):
                self.run_command(intervals(), *selector, '--apply', '--plan-hash', digest)
        self.assertFalse(EnduranceEntry.objects.exists())

    def test_key_for_another_athlete_stops_before_reading_data(self):
        get = intervals(athlete={'id': 'i9999'})

        with self.assertRaisesMessage(CommandError, 'different athlete'):
            self.run_command(get)

        self.assertEqual(get.call_count, 1)

    def test_bad_window_and_unconfigured_refuse_before_any_request(self):
        get = intervals()
        for oldest, newest in (('2040-06-26', '2040-06-20'), ('26/06/2040', '2040-06-26')):
            with self.subTest(oldest=oldest), self.assertRaises(CommandError):
                with mock.patch('wger.intervals.client.requests.request', get):
                    call_command('intervals-sync', '--oldest', oldest, '--newest', newest)
        with (
            override_settings(INTERVALS_API_KEY=''),
            self.assertRaisesMessage(CommandError, 'must be set'),
        ):
            self.run_command(get)
        with override_settings(INTERVALS_WGER_USERNAME='nobody'), self.assertRaises(CommandError):
            self.run_command(get)
        self.assertEqual(get.call_count, 0)

    def test_foreign_rows_in_fetch_refuse_apply_and_preview(self):
        foreign = intervals(activities=[ride(icu_athlete_id='i9999')])
        with self.assertRaisesMessage(CommandError, 'belongs to athlete'):
            self.run_command(foreign)

    def test_http_failures_are_typed_and_never_leak_the_key(self):
        failures = {
            'timeout': mock.Mock(side_effect=requests.Timeout(f'https://API_KEY:{KEY}@x')),
            'connection': mock.Mock(side_effect=requests.ConnectionError(f'auth {KEY} refused')),
            'rate limit': mock.Mock(return_value=response({}, 429, {'Retry-After': '60'})),
            'unauthorised': mock.Mock(return_value=response({'error': KEY}, 401)),
            'server': mock.Mock(return_value=response({}, 503)),
            'not json': mock.Mock(return_value=response(ValueError(KEY))),
            'not a list': intervals(activities={'rows': []}),
        }
        expected = {
            'timeout': 'no response within 30s',
            'connection': 'request failed (ConnectionError)',
            'rate limit': 'rate limited, retry after 60s',
            'unauthorised': 'HTTP 401, API key rejected',
            'server': 'HTTP 503',
            'not json': 'response is not JSON',
            'not a list': 'expected a list',
        }
        for name, get in failures.items():
            with self.subTest(name), self.assertRaises(CommandError) as raised:
                self.run_command(get)
            self.assertIn(expected[name], str(raised.exception))
            self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(EnduranceEntry.objects.count(), 0)
