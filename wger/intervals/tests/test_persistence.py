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

"""EnduranceEntry persistence and read-only API, synthetic rows only."""

# Django
from django.contrib.auth.models import User

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals.models import EnduranceEntry
from wger.intervals.persistence import sync_window
from wger.intervals.planning import PlanError
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST, event, ride
from wger.manager.models import WorkoutLog, WorkoutSession


URL = '/api/v2/endurance-entry/'


class SyncWindowTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.history = (WorkoutSession.objects.count(), WorkoutLog.objects.count())

    def sync(self, activities=(), events=(), user=None):
        return sync_window(
            user or self.user, ATHLETE, OLDEST, NEWEST, list(activities), list(events)
        )

    def tearDown(self):
        self.assertEqual((WorkoutSession.objects.count(), WorkoutLog.objects.count()), self.history)
        super().tearDown()

    def test_creates_then_retry_is_a_no_op(self):
        self.sync([ride()], [event()])
        first = {e.pk: e.fetched_at for e in EnduranceEntry.objects.all()}

        again = self.sync([ride()], [event()])

        self.assertEqual(len(again['unchanged']), 2)
        self.assertEqual({e.pk: e.fetched_at for e in EnduranceEntry.objects.all()}, first)
        stored = EnduranceEntry.objects.get(kind='completed')
        self.assertEqual(
            (stored.user, stored.intervals_id, stored.moving_time_s, stored.paired_event_id),
            (self.user, 'i900001', 14400, 5001),
        )

    def test_changed_value_updates_the_same_row(self):
        self.sync([ride()])
        pk = EnduranceEntry.objects.get().pk

        self.sync([ride(icu_training_load=190, average_heartrate=None)])

        stored = EnduranceEntry.objects.get()
        self.assertEqual((stored.pk, stored.training_load, stored.avg_hr), (pk, 190, None))

    def test_absent_row_is_kept_as_missing_and_revives(self):
        self.sync([ride()], [event()])

        self.sync([], [event()])
        self.assertEqual(EnduranceEntry.objects.get(kind='completed').upstream_state, 'missing')
        self.assertEqual(EnduranceEntry.objects.get(kind='planned').upstream_state, 'present')

        self.sync([ride()], [event()])
        self.assertEqual(EnduranceEntry.objects.get(kind='completed').upstream_state, 'present')

    def test_date_moved_into_window_updates_instead_of_duplicating(self):
        sync_window(
            self.user,
            ATHLETE,
            '2040-06-13',
            '2040-06-19',
            [ride(start_date_local='2040-06-19T07:00:00')],
            [],
        )

        self.sync([ride()])

        self.assertEqual(EnduranceEntry.objects.get().local_date.isoformat(), '2040-06-24')

    def test_same_intervals_id_is_separate_per_user(self):
        other = User.objects.get(username='test')
        self.sync([ride()])
        self.sync([ride(icu_training_load=1)], user=other)

        self.assertEqual(
            sorted(EnduranceEntry.objects.values_list('user__username', 'training_load')),
            [('admin', 187), ('test', 1)],
        )

    def test_refused_batch_writes_nothing(self):
        self.sync([ride()])

        with self.assertRaises(PlanError):
            self.sync([ride(icu_training_load=999), ride(id='i1', icu_athlete_id='i9')])

        self.assertEqual(EnduranceEntry.objects.get().training_load, 187)


class EnduranceEntryApiTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.get(username='admin')
        sync_window(
            self.admin,
            ATHLETE,
            OLDEST,
            NEWEST,
            [ride(), ride(id='i2', start_date_local='2040-06-21T08:00:00')],
            [event()],
        )
        sync_window(User.objects.get(username='test'), ATHLETE, OLDEST, NEWEST, [ride(id='i3')], [])

    def test_owner_sees_only_own_rows_with_date_filter_and_links(self):
        self.user_login('admin')

        rows = self.client.get(URL).json()['results']
        self.assertEqual(sorted(r['intervals_id'] for r in rows), ['5001', 'i2', 'i900001'])
        ride_row = next(r for r in rows if r['intervals_id'] == 'i900001')
        self.assertEqual(ride_row['link'], 'https://intervals.icu/activities/i900001')
        self.assertTrue(ride_row['link_exact'])

        dated = self.client.get(URL, {'local_date__gte': '2040-06-22', 'kind': 'completed'}).json()
        self.assertEqual([r['intervals_id'] for r in dated['results']], ['i900001'])

        foreign = EnduranceEntry.objects.get(intervals_id='i3').pk
        self.assertEqual(self.client.get(f'{URL}{foreign}/').status_code, 404)

    def test_no_write_methods_and_login_required(self):
        self.assertEqual(self.client.get(URL).status_code, 403)

        self.user_login('admin')
        pk = EnduranceEntry.objects.filter(user=self.admin).first().pk
        self.assertEqual(self.client.post(URL, {}).status_code, 405)
        self.assertEqual(self.client.patch(f'{URL}{pk}/', {'name': 'x'}).status_code, 405)
        self.assertEqual(self.client.delete(f'{URL}{pk}/').status_code, 405)
        self.assertTrue(EnduranceEntry.objects.filter(pk=pk).exists())
