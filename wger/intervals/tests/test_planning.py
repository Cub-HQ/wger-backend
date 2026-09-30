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

"""Intervals inbound mirror planner, synthetic rows only."""

# Standard Library
import datetime

# Django
from django.test import SimpleTestCase

# wger
from wger.intervals.planning import STORED_FIELDS, PlanError, plan


ATHLETE = 'i4242'
OLDEST, NEWEST = '2040-06-20', '2040-06-26'


def ride(**changes):
    row = {
        'id': 'i900001',
        'icu_athlete_id': ATHLETE,
        'type': 'Ride',
        'name': 'Sunday long ride',
        'start_date_local': '2040-06-24T07:15:00',
        'timezone': 'Australia/Sydney',
        'moving_time': 14400,
        'elapsed_time': 15320,
        'distance': 112340.5,
        'icu_training_load': 187,
        'power_load': 187,
        'hr_load': 164,
        'pace_load': None,
        'hr_load_type': 'HRSS',
        'pace_load_type': None,
        'icu_intensity': 71.3,
        'average_heartrate': 131,
        'max_heartrate': 168,
        'icu_rpe': None,
        'feel': None,
        'paired_event_id': 5001,
        'source': 'GARMIN_CONNECT',
    }
    return row | changes


def event(**changes):
    row = {
        'id': 5001,
        'athlete_id': ATHLETE,
        'category': 'WORKOUT',
        'type': 'Ride',
        'name': 'Endurance 4h',
        'start_date_local': '2040-06-24T00:00:00',
        'moving_time': 14400,
        'icu_training_load': 180,
        'load_target': 180,
        'time_target': 14400,
    }
    return row | changes


def stored(entry):
    """What the mirror table would hold after applying a create."""
    return {field: entry[field] for field in STORED_FIELDS}


def run(activities=(), existing=(), selected=None):
    return plan(ATHLETE, OLDEST, NEWEST, list(activities), list(existing), selected)


class PlanTest(SimpleTestCase):
    def test_four_hour_ride_is_one_verbatim_completed_entry_with_exact_link(self):
        result = run([ride()])

        [entry] = result['create']
        self.assertEqual(entry['kind'], 'completed')
        self.assertEqual(entry['intervals_id'], 'i900001')
        self.assertEqual(entry['local_date'], datetime.date(2040, 6, 24))
        self.assertEqual(entry['start_local'], '2040-06-24T07:15:00')
        self.assertEqual(entry['sport'], 'Ride')
        self.assertEqual(entry['moving_time_s'], 14400)
        self.assertEqual(entry['elapsed_time_s'], 15320)
        self.assertEqual(entry['distance_m'], 112340.5)
        self.assertEqual(
            (entry['training_load'], entry['power_load'], entry['hr_load'], entry['hr_load_type']),
            (187, 187, 164, 'HRSS'),
        )
        self.assertEqual(entry['intensity'], 71.3)
        self.assertEqual((entry['avg_hr'], entry['max_hr']), (131, 168))
        self.assertEqual(entry['paired_event_id'], 5001)
        self.assertEqual(entry['link'], 'https://intervals.icu/activities/i900001')
        self.assertTrue(entry['link_exact'])
        self.assertEqual(result['update'] + result['unchanged'] + result['mark_missing'], [])

    def test_strava_stub_keeps_every_missing_metric_null(self):
        stub = {
            'id': 'i900002',
            'icu_athlete_id': ATHLETE,
            'type': 'Ride',
            'name': 'Morning Ride',
            'start_date_local': '2040-06-22T06:00:00',
            'source': 'STRAVA',
        }

        [entry] = run([stub])['create']

        self.assertEqual(entry['activity_source'], 'STRAVA')
        for field in (
            'moving_time_s',
            'elapsed_time_s',
            'distance_m',
            'training_load',
            'power_load',
            'hr_load',
            'pace_load',
            'intensity',
            'avg_hr',
            'max_hr',
            'rpe',
            'feel',
        ):
            self.assertIsNone(entry[field], field)

    def test_local_date_is_the_athlete_wall_clock_day(self):
        late = ride(start_date_local='2040-06-20T23:30:00')
        early = ride(id='i900003', start_date_local='2040-06-26T00:05:00')

        entries = run([late, early])['create']

        self.assertEqual(
            [e['local_date'] for e in entries],
            [datetime.date(2040, 6, 20), datetime.date(2040, 6, 26)],
        )

    def test_rows_outside_window_or_with_bad_dates_refuse_the_batch(self):
        for start in (
            '2040-06-27T00:00:00',
            '2040-06-19T23:59:59',
            '24/06/2040',
            None,
            '2040-06-24T07:15:00Z',
        ):
            with self.subTest(start=start), self.assertRaises(PlanError):
                run([ride(start_date_local=start)])
        with self.assertRaises(PlanError):
            plan(ATHLETE, NEWEST, OLDEST, [], [])

    def test_retry_of_same_fetch_is_unchanged_and_order_independent(self):
        activities = [ride(), ride(id='i900004', start_date_local='2040-06-21T08:00:00')]
        first = run(activities)
        existing = [stored(e) for e in first['create']]

        again = run(reversed(activities), reversed(existing))

        self.assertEqual(again['create'] + again['update'] + again['mark_missing'], [])
        self.assertEqual(again['unchanged'], first['create'])
        self.assertEqual(run(reversed(activities)), first)

    def test_changed_value_is_one_update_naming_only_that_field(self):
        existing = [stored(e) for e in run([ride()])['create']]

        result = run([ride(icu_training_load=190)], existing=existing)

        [update] = result['update']
        self.assertEqual(update['changed'], ['training_load'])
        self.assertEqual(update['entry']['training_load'], 190)
        self.assertEqual(result['create'] + result['unchanged'], [])

    def test_record_absent_from_refetch_is_marked_missing_not_deleted(self):
        existing = [stored(e) for e in run([ride()])['create']]
        outside = existing[0] | {
            'intervals_id': 'i800000',
            'local_date': datetime.date(2040, 5, 1),
        }
        planned = existing[0] | {'kind': 'planned', 'intervals_id': '5001'}

        result = run(existing=existing + [outside, planned])

        self.assertEqual(result['mark_missing'], [{'kind': 'completed', 'intervals_id': 'i900001'}])
        self.assertEqual(result['unchanged'], [])
        flagged = [existing[0] | {'upstream_state': 'missing'}, planned]
        self.assertEqual(run(existing=flagged)['mark_missing'], [])
        self.assertEqual(
            [u['changed'] for u in run([ride()], flagged)['update']], [['upstream_state']]
        )

    def test_session_echo_and_gym_rows_are_counted_not_mirrored(self):
        result = run(
            [
                ride(id='i1', type='WeightTraining'),
                ride(id='i2', external_id='wger-gym-session:3:42'),
                ride(id='i3', type='WeightTraining', external_id='wger-gym-session:3:43'),
            ]
        )

        self.assertEqual(result['create'], [])
        self.assertEqual(result['skipped'], {'echo': 2, 'weight_training': 1})

    def test_exact_selects_only_one_completed_ride(self):
        result = run([ride(), ride(id='i2')], selected='i900001')
        self.assertEqual((result['mode'], result['selected']), ('exact', 'i900001'))
        self.assertEqual(result['create'], run([ride()])['create'])
        window = run([ride()])
        self.assertEqual((window['mode'], window['selected']), ('window', None))

    def test_exact_refuses_absent_gym_echo_and_durationless_stub(self):
        for activities in (
            [],
            [ride(type='WeightTraining')],
            [ride(external_id='wger-gym-session:3:42')],
            [ride(moving_time=None, elapsed_time=None)],
        ):
            with self.subTest(activities=activities), self.assertRaises(PlanError):
                run(activities, selected='i900001')
        for changes in ({'moving_time': None}, {'elapsed_time': None}, {'moving_time': 0}):
            with self.subTest(changes=changes):
                self.assertEqual(len(run([ride(**changes)], selected='i900001')['create']), 1)

    def test_entire_fetch_is_validated_before_selection_or_skipping(self):
        for changes in (
            {'icu_athlete_id': 'i9999'},
            {'start_date_local': '2040-06-27T00:00:00'},
            {'start_date_local': None},
        ):
            for skipped in ({}, {'type': 'WeightTraining'}, {'external_id': 'wger-gym-session:x'}):
                for selected in (None, 'i900001'):
                    with self.subTest(changes=changes, skipped=skipped, selected=selected):
                        with self.assertRaises(PlanError):
                            run([ride(), ride(id='i2', **(changes | skipped))], selected=selected)

    def test_duplicate_identities_refuse_even_unselected_or_skipped_rows(self):
        for skipped in ({}, {'type': 'WeightTraining'}, {'external_id': 'wger-gym-session:x'}):
            for selected in (None, 'i900001'):
                with self.subTest(skipped=skipped, selected=selected), self.assertRaises(PlanError):
                    run(
                        [ride(), ride(id='i2', **skipped), ride(id='i2', **skipped)],
                        selected=selected,
                    )
        existing = stored(run([ride()])['create'][0])
        with self.assertRaises(PlanError):
            run(existing=[existing, dict(existing)])

    def test_exact_leaves_unselected_missing_and_planned_rows_alone(self):
        entries = run([ride(), ride(id='i2')])['create']
        existing = [stored(entry) for entry in entries]
        existing.append(existing[0] | {'kind': 'planned', 'intervals_id': '5001'})
        result = run([ride()], existing, selected='i900001')
        self.assertEqual(result['mark_missing'], [])
        self.assertEqual(result['unchanged'], run([ride()])['create'])
        self.assertEqual(result['create'] + result['update'], [])
