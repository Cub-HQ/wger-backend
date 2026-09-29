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

"""intervals-sync command and GET-only client. requests is mocked; no network."""

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
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST, event, ride
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


def intervals(athlete=None, activities=None, events=None):
    """A fake requests.get serving one athlete window."""
    served = {
        'athlete/0': response(athlete or {'id': ATHLETE}),
        'athlete/0/activities': response([ride()] if activities is None else activities),
        'athlete/0/events': response([event()] if events is None else events),
    }
    return mock.Mock(side_effect=lambda url, **kw: served[url.split('/api/v1/')[1]])


@CONFIGURED
class IntervalsSyncCommandTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.history = (WorkoutSession.objects.count(), WorkoutLog.objects.count())

    def tearDown(self):
        self.assertEqual((WorkoutSession.objects.count(), WorkoutLog.objects.count()), self.history)
        super().tearDown()

    def run_command(self, get, *extra):
        out = io.StringIO()
        with mock.patch('wger.intervals.client.requests.get', get):
            call_command(
                'intervals-sync', '--oldest', OLDEST, '--newest', NEWEST, *extra, stdout=out
            )
        return json.loads(out.getvalue())

    def test_preview_is_get_only_and_writes_nothing_then_apply_writes_it(self):
        get = intervals()

        report = self.run_command(get)

        self.assertEqual(EnduranceEntry.objects.count(), 0)
        self.assertEqual(report['mode'], 'preview (no writes)')
        self.assertEqual([r['intervals_id'] for r in report['create']], ['i900001', '5001'])
        calls = [(c.args[0], c.kwargs) for c in get.call_args_list]
        self.assertEqual(
            [url.rsplit('/v1/', 1)[1] for url, _ in calls],
            ['athlete/0', 'athlete/0/activities', 'athlete/0/events'],
        )
        self.assertEqual(calls[1][1]['params'], {'oldest': OLDEST, 'newest': f'{NEWEST}T23:59:59'})
        self.assertEqual(calls[2][1]['params'], {'oldest': OLDEST, 'newest': NEWEST})
        self.assertTrue(all(kw['auth'] == ('API_KEY', KEY) and kw['timeout'] for _, kw in calls))

        applied = self.run_command(intervals(), '--apply', '--plan-hash', report['plan_hash'])
        self.assertEqual(applied['plan_hash'], report['plan_hash'])
        self.assertEqual(EnduranceEntry.objects.filter(user__username='admin').count(), 2)

        again = self.run_command(intervals())
        self.assertEqual((again['create'], again['update'], again['unchanged']), ([], [], 2))

    def test_apply_refuses_without_or_with_stale_hash(self):
        with self.assertRaisesMessage(CommandError, '--plan-hash'):
            self.run_command(intervals(), '--apply')
        stale = self.run_command(intervals())['plan_hash']
        with self.assertRaisesMessage(CommandError, 'changed since the preview'):
            self.run_command(
                intervals(activities=[ride(icu_training_load=1)]), '--apply', '--plan-hash', stale
            )
        self.assertEqual(EnduranceEntry.objects.count(), 0)

    def test_key_for_another_athlete_stops_before_reading_data(self):
        get = intervals(athlete={'id': 'i9999'})

        with self.assertRaisesMessage(CommandError, 'different athlete'):
            self.run_command(get)

        self.assertEqual(get.call_count, 1)

    def test_bad_window_and_unconfigured_refuse_before_any_request(self):
        get = intervals()
        for oldest, newest in (('2040-06-26', '2040-06-20'), ('26/06/2040', '2040-06-26')):
            with self.subTest(oldest=oldest), self.assertRaises(CommandError):
                with mock.patch('wger.intervals.client.requests.get', get):
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
