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

"""Trainerize history import, synthetic workouts only."""

# Standard Library
from decimal import Decimal

# Django
from django.test import SimpleTestCase

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import WorkoutLog, WorkoutSession
from wger.manager.trainerize_history import AlreadyPresent, apply, plan


def stats(**values):
    base = dict.fromkeys(('reps', 'weight', 'time', 'distance', 'calories', 'level', 'speed'))
    return {'id': 0, 'setID': 1, **base, **values}


def exercise(source_id, *sets, rest=30):
    return {
        'def': {'id': source_id, 'name': f'Source {source_id}', 'restTime': rest},
        'stats': list(sets),
    }


def workout(*exercises, **changes):
    return {
        'id': 900001,
        'name': 'S.I.T',
        'date': '2026-01-07',
        'startTime': '2026-01-06 21:35:19',
        'endTime': '2026-01-06 22:34:28',
        'status': 'tracked',
        'exercises': list(exercises),
        **changes,
    }


MAPPING = {'11': 1, '12': 2, '13': 3}


class PlanTestCase(SimpleTestCase):
    def test_simultaneous_cardio_metrics_stay_on_one_set(self):
        result = plan(
            workout(exercise(11, stats(time=20.0, distance=0.16, calories=15.0, speed=0.16))),
            MAPPING,
        )
        self.assertEqual(result['status'], 'ready')
        (log,) = result['logs']
        self.assertEqual(
            (log['repetitions'], log['repetitions_unit'], log['distance'], log['distance_unit']),
            ('20.00', 3, '0.160', 6),
        )
        self.assertEqual(
            (log['calories'], log['max_speed'], log['max_speed_unit']), ('15.00', '0.16', 5)
        )
        self.assertIsNone(log['weight'])

    def test_zero_is_kept_and_empty_sets_are_skipped(self):
        result = plan(
            workout(exercise(12, stats(reps=10, weight=0.0), stats(), stats(reps=8))), MAPPING
        )
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(
            [(log['weight'], log['weight_unit']) for log in result['logs']],
            [('0.00', 1), (None, None)],
        )
        self.assertEqual([log['iteration'] for log in result['logs']], [1, 2])

    def test_zero_only_set_is_held(self):
        result = plan(workout(exercise(12, stats(reps=0, weight=0.0))), MAPPING)
        self.assertEqual(result['status'], 'held')

    def test_omit_incomplete_imports_valid_sets_and_lists_the_rest(self):
        source = workout(
            exercise(12, stats(reps=10, weight=20.0), stats(setID=2, reps=0, weight=0.0)),
            exercise(13, stats(setID=3, weight=11.0), stats(setID=4, reps=6)),
        )
        result = plan(source, MAPPING, omit_incomplete=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(
            [(log['exercise'], log['iteration'], log['repetitions']) for log in result['logs']],
            [(2, 1, '10.00'), (3, 1, '6.00')],
        )
        self.assertEqual(
            [(o['exercise'], o['raw']['setID']) for o in result['omitted']], [(12, 2), (13, 3)]
        )
        self.assertIn(
            'Source 12 [Trainerize 12] set 2: zero-only set is ambiguous (performed or skipped); '
            'source values: reps 0, weight 0.0',
            result['notes'],
        )
        self.assertIn(
            'Source 13 [Trainerize 13] set 3: set without reps, time or distance; '
            'source values: weight 11.0',
            result['notes'],
        )
        # Without the policy the same workout is held
        self.assertEqual(plan(source, MAPPING)['status'], 'held')

    def test_omit_incomplete_keeps_workout_without_usable_sets_held(self):
        result = plan(
            workout(exercise(12, stats(reps=0, weight=0.0))),
            MAPPING,
            omit_incomplete=True,
            clarification='x',
        )
        self.assertEqual((result['status'], result['logs']), ('held', []))
        self.assertIn('no usable sets', result['holds'][0])

    def test_omit_incomplete_does_not_bypass_other_holds(self):
        for source in (
            workout(exercise(12, stats(reps=1)), exercise(99, stats(reps=0))),
            workout(exercise(12, stats(reps=1), stats(reps=0)), endTime='2026-01-08 03:00:00'),
        ):
            self.assertEqual(plan(source, MAPPING, omit_incomplete=True)['status'], 'held')

    def test_value_finer_than_column_is_held_not_rounded(self):
        result = plan(workout(exercise(11, stats(time=20.0, distance=0.1234))), MAPPING)
        self.assertEqual(result['status'], 'held')
        self.assertIn('would be rounded', result['holds'][0])

    def test_strength_set_and_rest_target(self):
        result = plan(workout(exercise(12, stats(reps=12, weight=22.5), rest=90)), MAPPING)
        (log,) = result['logs']
        self.assertEqual(
            (log['repetitions'], log['repetitions_unit'], log['weight']), ('12.00', 1, '22.50')
        )
        self.assertEqual(log['rest_target'], 90)

    def test_unmapped_exercise_with_sets_holds_the_whole_workout(self):
        result = plan(workout(exercise(12, stats(reps=5)), exercise(99, stats(reps=5))), MAPPING)
        self.assertEqual(result['status'], 'held')
        self.assertIn('no validated wger exercise mapping', result['holds'][0])

    def test_order_and_repeated_exercise_continue_iterations(self):
        result = plan(
            workout(
                exercise(12, stats(reps=1)),
                exercise(13, stats(reps=2)),
                exercise(12, stats(reps=3)),
            ),
            MAPPING,
        )
        self.assertEqual(
            [(log['exercise'], log['iteration'], log['repetitions']) for log in result['logs']],
            [(2, 1, '1.00'), (3, 1, '2.00'), (2, 2, '3.00')],
        )
        self.assertIn('Source 12 [Trainerize 12 → wger 2]\nSource 13', result['notes'])

    def test_technique_difference_is_kept_in_provenance(self):
        result = plan(
            workout(exercise(12, stats(reps=5))), MAPPING, differences={'12': 'jumps down'}
        )
        self.assertIn(
            'Source 12 [Trainerize 12 → wger 2] (source differs: jumps down)', result['notes']
        )

    def test_source_times_are_utc_and_long_sessions_hold(self):
        self.assertEqual(
            plan(workout(), MAPPING, clarification='x')['datetime_start'],
            '2026-01-06T21:35:19+00:00',
        )
        long = plan(workout(exercise(12, stats(reps=1)), endTime='2026-01-07 03:00:00'), MAPPING)
        self.assertEqual(long['status'], 'held')

    def test_setless_workout_needs_clarification(self):
        self.assertEqual(plan(workout(exercise(12, stats())), MAPPING)['status'], 'held')
        result = plan(
            workout(exercise(12, stats())), MAPPING, clarification='User confirmed: stretch day'
        )
        self.assertEqual((result['status'], result['logs']), ('ready', []))
        self.assertTrue(result['notes'].endswith('User confirmed: stretch day'))

    def test_scheduled_workout_is_held(self):
        self.assertEqual(
            plan(workout(exercise(12, stats(reps=1)), status='scheduled'), MAPPING)['status'],
            'held',
        )


class ApplyTestCase(WgerTestCase):
    def ready(self, **changes):
        return plan(
            workout(
                exercise(11, stats(time=20.0, distance=0.16, calories=15.0, speed=0.16)),
                exercise(12, stats(reps=12, weight=22.5), stats(reps=13, weight=0.0)),
                **changes,
            ),
            MAPPING,
        )

    def test_writes_every_value_and_refuses_a_second_import(self):
        before = set(WorkoutLog.objects.values_list('pk', flat=True))
        session_id = apply(self.ready(), user_id=1, routine_id=1, day_id=1)
        logs = WorkoutLog.objects.filter(session_id=session_id)
        self.assertEqual(logs.count(), 3)
        rower = logs.get(exercise_id=1)
        self.assertEqual(
            (rower.repetitions, rower.distance, rower.max_speed),
            (Decimal(20), Decimal('0.16'), Decimal('0.16')),
        )
        self.assertEqual(
            set(WorkoutLog.objects.values_list('pk', flat=True)) - before,
            set(logs.values_list('pk', flat=True)),
        )

        with self.assertRaises(AlreadyPresent):
            apply(self.ready(), user_id=1, routine_id=1, day_id=1)
        # Same source moved in time is still caught by its source line
        with self.assertRaises(AlreadyPresent):
            apply(
                self.ready(startTime='2026-02-01 08:00:00', endTime='2026-02-01 09:00:00'),
                user_id=1,
                routine_id=1,
                day_id=1,
            )

    def test_failure_leaves_nothing_behind(self):
        ready = self.ready()
        ready['logs'][-1]['repetitions_unit'] = None  # rejected by WorkoutLog.clean
        sessions, logs = WorkoutSession.objects.count(), WorkoutLog.objects.count()
        with self.assertRaises(Exception):
            apply(ready, user_id=1, routine_id=1, day_id=1)
        self.assertEqual(
            (WorkoutSession.objects.count(), WorkoutLog.objects.count()), (sessions, logs)
        )

    def test_held_plan_and_foreign_routine_are_refused(self):
        with self.assertRaises(ValueError):
            apply(
                plan(workout(exercise(99, stats(reps=1))), MAPPING),
                user_id=1,
                routine_id=1,
                day_id=1,
            )
        with self.assertRaises(ValueError):
            apply(self.ready(), user_id=2, routine_id=1, day_id=1)
