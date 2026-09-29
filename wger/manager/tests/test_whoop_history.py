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

"""WHOOP summary history import, synthetic workouts only."""

# Standard Library
import datetime

# Django
from django.test import SimpleTestCase

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import WorkoutLog, WorkoutSession
from wger.manager.whoop_history import AlreadyPresent, apply, plan, reconcile


ACCOUNT = 777
ROUTINE = 1
START = '2040-06-22T21:50:49.512Z'
END = '2040-06-22T22:40:25.512Z'


def at(value):
    return datetime.datetime.fromisoformat(value)


def workout(**changes):
    score = {
        'strain': 11.7,
        'average_heart_rate': 110,
        'max_heart_rate': 150,
        'kilojoule': 1500.5,
        'percent_recorded': 0.9997749,
        'distance_meter': None,
        'altitude_gain_meter': None,
        'altitude_change_meter': None,
        'zone_durations': {
            'zone_zero_milli': 1000,
            'zone_one_milli': 60000,
            'zone_two_milli': 0,
            'zone_three_milli': 0,
            'zone_four_milli': 0,
            'zone_five_milli': 0,
        },
    }
    score.update(changes.pop('score', {}))
    return {
        'id': 'w-1',
        'v1_id': None,
        'user_id': ACCOUNT,
        'created_at': END,
        'updated_at': END,
        'start': START,
        'end': END,
        'timezone_offset': '+10:00',
        'sport_name': 'functional-fitness',
        'sport_id': 71,
        'score_state': 'SCORED',
        'score': score,
        **changes,
    }


def session(pk, start=START, end=END, routine=ROUTINE, logs=0, notes=''):
    return {
        'id': pk,
        'routine_id': routine,
        'datetime_start': at(start),
        'datetime_end': at(end) if end else None,
        'notes': notes,
        'logs': logs,
    }


def planned(record=None, sessions=(), scale=1):
    return plan(
        record or workout(),
        account=ACCOUNT,
        routine_id=ROUTINE,
        sessions=sessions,
        percent_scale=scale,
    )


class PlanTestCase(SimpleTestCase):
    def test_units_timestamps_nulls_and_zeros(self):
        result = planned()
        self.assertEqual(result['status'], 'ready')
        lines = result['notes'].split('\n')
        self.assertEqual(lines[0], 'WHOOP · functional-fitness')
        self.assertIn('Started: 2040-06-23T07:50:49.512000+10:00', lines)
        self.assertIn('Started UTC: 2040-06-22T21:50:49.512000+00:00', lines)
        self.assertEqual(at(result['datetime_start']), at(START))
        self.assertIn('Recorded: 99.97749 %', lines)
        self.assertIn('Energy: 1500.5 kJ', lines)
        self.assertIn('Zone durations zone two: 0 ms (0 min)', lines)
        self.assertFalse(any(line.startswith(('Distance', 'Altitude')) for line in lines))
        for word in ('Sets', 'Reps', 'Weight', 'Exercise'):
            self.assertNotIn(word, result['notes'])

    def test_percent_needs_a_declared_scale(self):
        whole = planned(workout(score={'percent_recorded': 1.0}), scale=1)
        self.assertIn('Recorded: 100 %', whole['notes'].split('\n'))
        on_100 = planned(workout(score={'percent_recorded': 99.5}), scale=100)
        self.assertIn('Recorded: 99.5 %', on_100['notes'].split('\n'))
        # A small value is not taken as a fraction without the capture's scale
        undeclared = planned(scale=None)
        self.assertEqual(undeclared['status'], 'held')
        self.assertIn(
            'percent_recorded scale for this capture is not declared', undeclared['holds']
        )
        # A value that contradicts the declared scale is held, not clipped
        self.assertEqual(planned(workout(score={'percent_recorded': 99.5}))['status'], 'held')
        # No percent reported: nothing to encode, nothing held
        unreported = planned(workout(score={'percent_recorded': None}), scale=None)
        self.assertEqual(unreported['status'], 'ready')

    def test_summary_boundary_account_and_length(self):
        self.assertEqual(planned(workout(score={'exercises': []}))['status'], 'held')
        self.assertEqual(planned(workout(linked_workout='x'))['status'], 'held')
        self.assertEqual(planned(workout(user_id=1))['status'], 'held')
        self.assertEqual(planned(workout(score_state='PENDING_SCORE'))['status'], 'held')
        self.assertEqual(planned(workout(score={'kilojoule': -1}))['status'], 'held')
        self.assertEqual(planned(workout(end='2040-06-23T02:51:00Z'))['status'], 'held')
        self.assertEqual(planned(workout(sport_name='spin'))['status'], 'intervals')

    def test_present_by_exact_instant_in_whoop_routine(self):
        notes = planned()['notes']
        exact = planned(sessions=[session('a', notes=notes)])
        self.assertEqual(
            (exact['status'], exact['session'], exact['notes_current']), ('present', 'a', True)
        )
        legacy = planned(sessions=[session('a', notes='WHOOP · functional-fitness')])
        self.assertEqual((legacy['status'], legacy['notes_current']), ('present', False))
        # Truncated to the second is not proof of the same workout; flagged for review
        truncated = planned(sessions=[session('a', start='2040-06-22T21:50:49Z')])
        self.assertEqual(truncated['status'], 'held')

    def test_other_provider_overlap_is_flagged_same_day_distinct_is_not(self):
        trainerize = session(
            't', routine=2, logs=18, start='2040-06-22T21:50:49Z', end='2040-06-22T22:40:25Z'
        )
        result = planned(sessions=[trainerize])
        self.assertEqual(result['status'], 'held')
        self.assertEqual(
            result['overlaps'],
            [
                {
                    'session': 't',
                    'routine_id': 2,
                    'logs': 18,
                    'start_delta_s': -0.512,
                    'end_delta_s': -0.512,
                }
            ],
        )
        evening = session(
            'e', routine=2, logs=12, start='2040-06-23T08:00:00Z', end='2040-06-23T09:00:00Z'
        )
        touching = session('b', routine=2, start='2040-06-22T20:50:49.512Z', end=START)
        self.assertEqual(planned(sessions=[evening, touching])['status'], 'ready')
        open_inside = session('o', routine=2, start='2040-06-22T22:00:00Z', end=None)
        self.assertEqual(planned(sessions=[open_inside])['status'], 'held')

    def test_reconcile_replaces_a_lost_ledger(self):
        second = workout(id='w-2', start='2040-06-24T21:00:00Z', end='2040-06-24T22:00:00Z')
        orphan = session('z', start='2040-01-01T00:00:00Z', end='2040-01-01T01:00:00Z')
        plans, orphans = reconcile(
            [workout(), second, workout()],
            account=ACCOUNT,
            routine_id=ROUTINE,
            sessions=[session('a'), orphan],
            percent_scale=1,
        )
        self.assertEqual(
            [(p['source_id'], p['status']) for p in plans], [('w-1', 'present'), ('w-2', 'ready')]
        )
        self.assertEqual(orphans, ['z'])
        changed = workout(score={'strain': 12.0})
        plans, _ = reconcile(
            [workout(), changed], account=ACCOUNT, routine_id=ROUTINE, sessions=[], percent_scale=1
        )
        self.assertEqual(plans[0]['status'], 'held')


class ApplyTestCase(WgerTestCase):
    def test_writes_summary_only_and_a_retry_finds_it(self):
        ready = planned()
        logs = WorkoutLog.objects.count()
        pk = apply(ready, user_id=1, day_id=1)
        stored = WorkoutSession.objects.get(pk=pk)
        self.assertEqual((stored.datetime_start, stored.notes), (at(START), ready['notes']))
        self.assertEqual(WorkoutLog.objects.count(), logs)
        # The outcome of the first write was lost: the retry finds it, it does not duplicate
        with self.assertRaises(AlreadyPresent) as raised:
            apply(ready, user_id=1, day_id=1)
        self.assertEqual(raised.exception.sessions, [pk])
        self.assertEqual(WorkoutSession.objects.filter(datetime_start=at(START)).count(), 1)

    def test_refuses_overlap_in_any_routine_held_plans_and_wrong_owner(self):
        WorkoutSession.objects.create(
            user_id=1,
            routine_id=2,
            datetime_start=at('2040-06-22T22:00:00Z'),
            datetime_end=at('2040-06-22T23:00:00Z'),
        )
        count = WorkoutSession.objects.count()
        with self.assertRaises(AlreadyPresent):
            apply(planned(), user_id=1, day_id=1)
        with self.assertRaises(ValueError):
            apply(planned(scale=None), user_id=1, day_id=1)
        later = workout(start='2041-01-01T00:00:00Z', end='2041-01-01T01:00:00Z')
        with self.assertRaises(ValueError):
            apply(planned(later), user_id=2, day_id=1)
        self.assertEqual(WorkoutSession.objects.count(), count)
