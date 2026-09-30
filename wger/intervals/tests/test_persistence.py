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
from wger.intervals.persistence import apply, preview
from wger.intervals.planning import PlanError
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST, ride
from wger.manager.models import WorkoutLog, WorkoutSession


URL = '/api/v2/endurance-entry/'


def sync_window(user, athlete_id, oldest, newest, activities, selected=None):
    """Preview then apply that exact preview, as the command does."""
    args = (user, athlete_id, oldest, newest, activities)
    return apply(*args, preview(*args, selected=selected)['plan_hash'], selected=selected)


class SyncWindowTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.history = (
            list(WorkoutSession.objects.order_by('pk').values()),
            list(WorkoutLog.objects.order_by('pk').values()),
        )

    def sync(self, activities=(), user=None, selected=None):
        return sync_window(user or self.user, ATHLETE, OLDEST, NEWEST, list(activities), selected)

    def tearDown(self):
        self.assertEqual(
            (
                list(WorkoutSession.objects.order_by('pk').values()),
                list(WorkoutLog.objects.order_by('pk').values()),
            ),
            self.history,
        )
        super().tearDown()

    def test_creates_then_retry_is_a_no_op(self):
        self.sync([ride()])
        first = {e.pk: e.fetched_at for e in EnduranceEntry.objects.all()}

        again = self.sync([ride()])

        self.assertEqual(len(again['unchanged']), 1)
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
        self.sync([ride()])

        self.sync([])
        self.assertEqual(EnduranceEntry.objects.get().upstream_state, 'missing')

        self.sync([ride()])
        self.assertEqual(EnduranceEntry.objects.get().upstream_state, 'present')

    def test_date_moved_into_window_updates_instead_of_duplicating(self):
        sync_window(
            self.user,
            ATHLETE,
            '2040-06-13',
            '2040-06-19',
            [ride(start_date_local='2040-06-19T07:00:00')],
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

    def test_preview_writes_nothing_and_apply_needs_its_exact_hash(self):
        self.sync([ride()])
        args = (self.user, ATHLETE, OLDEST, NEWEST, [ride(icu_training_load=190)])

        stale = preview(*args)['plan_hash']
        self.assertEqual(EnduranceEntry.objects.get().training_load, 187)

        with self.assertRaises(PlanError):
            apply(*args, 'f' * 64)
        changed = (self.user, ATHLETE, OLDEST, NEWEST, [ride(icu_training_load=191)])
        with self.assertRaises(PlanError):
            apply(*changed, stale)
        self.assertEqual(EnduranceEntry.objects.get().training_load, 187)

        apply(*args, stale)
        self.assertEqual(EnduranceEntry.objects.get().training_load, 190)

    def test_exact_import_and_reapply_preserve_verbatim_metrics(self):
        args = (self.user, ATHLETE, OLDEST, NEWEST, [ride(), ride(id='i2')])
        first = preview(*args, selected='i900001')
        self.assertFalse(EnduranceEntry.objects.exists())
        apply(*args, first['plan_hash'], selected='i900001')
        row = EnduranceEntry.objects.get()
        self.assertEqual(
            (
                row.intervals_id,
                row.moving_time_s,
                row.elapsed_time_s,
                row.distance_m,
                row.training_load,
                row.avg_hr,
                row.paired_event_id,
            ),
            ('i900001', 14400, 15320, 112340.5, 187, 131, 5001),
        )
        snapshot = list(EnduranceEntry.objects.values())
        again = preview(*args, selected='i900001')
        self.assertEqual(again['create'] + again['update'] + again['mark_missing'], [])
        self.assertEqual([e['intervals_id'] for e in again['unchanged']], ['i900001'])
        apply(*args, again['plan_hash'], selected='i900001')
        self.assertEqual(list(EnduranceEntry.objects.values()), snapshot)

    def test_hash_binds_mode_and_selector_even_when_actions_are_identical(self):
        self.sync([ride(), ride(id='i2')])
        args = (self.user, ATHLETE, OLDEST, NEWEST, [ride(), ride(id='i2')])
        plans = [preview(*args, selected=selected) for selected in (None, 'i900001', 'i2')]
        for result in plans:
            self.assertEqual(result['create'] + result['update'] + result['mark_missing'], [])
        self.assertEqual(len({result['plan_hash'] for result in plans}), 3)
        snapshot = list(EnduranceEntry.objects.order_by('pk').values())
        for target, source in (('i900001', 0), (None, 1), ('i2', 1)):
            with self.subTest(target=target), self.assertRaises(PlanError):
                apply(*args, plans[source]['plan_hash'], selected=target)
        self.assertEqual(list(EnduranceEntry.objects.order_by('pk').values()), snapshot)

    def test_cross_mode_create_hash_refusal_writes_nothing(self):
        args = (self.user, ATHLETE, OLDEST, NEWEST, [ride()])
        window = preview(*args)
        exact = preview(*args, selected='i900001')
        self.assertEqual(window['create'], exact['create'])
        self.assertNotEqual(window['plan_hash'], exact['plan_hash'])
        for digest, selected in ((window['plan_hash'], 'i900001'), (exact['plan_hash'], None)):
            with self.subTest(selected=selected), self.assertRaises(PlanError):
                apply(*args, digest, selected=selected)
        self.assertFalse(EnduranceEntry.objects.exists())

    def test_exact_and_window_never_modify_legacy_planned_rows(self):
        self.sync([ride(), ride(id='i2')])
        legacy = EnduranceEntry.objects.get(intervals_id='i900001')
        legacy.pk = None
        legacy.kind = 'planned'
        legacy.intervals_id = '5001'
        legacy.save()
        before = list(
            EnduranceEntry.objects.exclude(intervals_id='i900001').order_by('pk').values()
        )

        result = self.sync([ride(icu_training_load=190)], selected='i900001')

        self.assertEqual(result['mark_missing'], [])
        self.assertEqual(
            list(EnduranceEntry.objects.exclude(intervals_id='i900001').order_by('pk').values()),
            before,
        )
        planned = EnduranceEntry.objects.filter(kind='planned')
        planned_before = list(planned.values())
        self.sync([])
        self.assertEqual(list(planned.values()), planned_before)
        self.assertEqual(
            set(
                EnduranceEntry.objects.filter(kind='completed').values_list(
                    'upstream_state', flat=True
                )
            ),
            {'missing'},
        )

    def test_exact_refused_batches_cannot_modify_existing_rows(self):
        self.sync([ride()])
        snapshot = list(EnduranceEntry.objects.values())
        for activities in (
            [],
            [ride(type='WeightTraining')],
            [ride(external_id='wger-gym-session:1:42')],
            [ride(moving_time=None, elapsed_time=None)],
            [ride(), ride(id='i2', icu_athlete_id='i9999')],
            [ride(), ride(id='i2'), ride(id='i2')],
        ):
            args = (self.user, ATHLETE, OLDEST, NEWEST, activities)
            with self.subTest(activities=activities):
                with self.assertRaises(PlanError):
                    preview(*args, selected='i900001')
                with self.assertRaises(PlanError):
                    apply(*args, 'f' * 64, selected='i900001')
                self.assertEqual(list(EnduranceEntry.objects.values()), snapshot)


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
        )
        sync_window(User.objects.get(username='test'), ATHLETE, OLDEST, NEWEST, [ride(id='i3')])

    def test_owner_sees_only_own_rows_with_date_filter_and_links(self):
        self.user_login('admin')

        rows = self.client.get(URL).json()['results']
        self.assertEqual(sorted(r['intervals_id'] for r in rows), ['i2', 'i900001'])
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
