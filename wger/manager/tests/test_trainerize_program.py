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
# along with Workout Manager.  If not, see <http://www.gnu.org/licenses/>.

"""Trainerize planned-programme import, synthetic programmes only."""

# Standard Library
import copy
import datetime
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.test import SimpleTestCase

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager import trainerize_program
from wger.manager.models import (
    Routine,
    SlotEntry,
    WorkoutLog,
    WorkoutSession,
)
from wger.manager.trainerize_program import (
    Refused,
    apply,
    normalize,
    preview,
    reconcile,
)


def exercise(source_id, target='10 reps', sets=3, rest=60, **changes):
    return {
        'id': source_id,
        'name': f'Source {source_id}',
        'sets': sets,
        'restTime': rest,
        'target': target,
        'targetDetail': {'type': 10, 'text': target, 'time': None, 'distance': None},
        'superSetID': 0,
        'supersetType': 'none',
        'recordType': 'strength',
        'side': None,
        'note': None,
        'stats': [],
        **changes,
    }


def timed(source_id, seconds, **changes):
    return exercise(
        source_id,
        target='',
        targetDetail={'type': 2, 'text': None, 'time': seconds, 'distance': None},
        **changes,
    )


def workout(workout_id, *exercises, **changes):
    return {
        'id': workout_id,
        'name': f'Day {workout_id}',
        'instructions': 'Synthetic instructions',
        'type': 'workoutRegular',
        'rounds': 1,
        'exercises': list(exercises),
        **changes,
    }


def source(*workouts):
    return {
        'accountID': 700,
        'phases': [
            {'id': 41, 'startDate': '2030-01-06', 'endDate': '2030-02-02', 'workouts': []},
            {
                'id': 42,
                'startDate': '2030-02-03',
                'endDate': '2030-03-02',
                'workouts': list(workouts),
            },
        ],
    }


INPUTS = {
    'account_id': 700,
    'phase_id': 42,
    'start': '2030-02-03',
    'end': '2030-03-02',
    'routine_name': 'Synthetic TZ phase',
    'mapping': {'11': 1, '12': 2, '13': 3},
    'today': datetime.date(2030, 2, 10),
}


def errors(result):
    return [(e['source']['workout'], e['source']['exercise'], e['field']) for e in result['errors']]


class NormalizeTestCase(SimpleTestCase):
    def test_supported_programme_becomes_rows(self):
        result = normalize(
            source(
                workout(
                    501,
                    exercise(11, target='8-12 reps', sets=4, rest=90),
                    exercise(12, superSetID=7, supersetType='superset'),
                    timed(13, 30.0, superSetID=7, supersetType='superset', note='Slow'),
                    exercise(11, target='10 reps per side'),
                ),
                workout(502),
            ),
            **INPUTS,
        )
        self.assertTrue(result['ok'], result['errors'])
        picked = [
            {
                k: row.get(k, '')
                for k in (
                    'day_order',
                    'slot_order',
                    'entry_order',
                    'exercise_id',
                    'sets',
                    'reps',
                    'max_reps',
                    'rep_unit',
                    'rest',
                    'notes',
                )
            }
            for row in result['rows']
        ]
        self.assertEqual(
            picked,
            [
                {
                    'day_order': '1',
                    'slot_order': '1',
                    'entry_order': '1',
                    'exercise_id': '1',
                    'sets': '4',
                    'reps': '8',
                    'max_reps': '12',
                    'rep_unit': 'Repetitions',
                    'rest': '90',
                    'notes': '',
                },
                # Superset: one slot, two entries; the timed target in seconds
                {
                    'day_order': '1',
                    'slot_order': '2',
                    'entry_order': '1',
                    'exercise_id': '2',
                    'sets': '3',
                    'reps': '10',
                    'max_reps': '',
                    'rep_unit': 'Repetitions',
                    'rest': '60',
                    'notes': '',
                },
                {
                    'day_order': '1',
                    'slot_order': '2',
                    'entry_order': '2',
                    'exercise_id': '3',
                    'sets': '3',
                    'reps': '30',
                    'max_reps': '',
                    'rep_unit': 'Seconds',
                    'rest': '60',
                    'notes': 'Slow',
                },
                # Target wording beyond the number is kept verbatim as the note
                {
                    'day_order': '1',
                    'slot_order': '3',
                    'entry_order': '1',
                    'exercise_id': '1',
                    'sets': '3',
                    'reps': '10',
                    'max_reps': '',
                    'rep_unit': 'Repetitions',
                    'rest': '60',
                    'notes': '10 reps per side',
                },
                # A day without exercises is one empty row
                {
                    'day_order': '2',
                    'slot_order': '',
                    'entry_order': '',
                    'exercise_id': '',
                    'sets': '',
                    'reps': '',
                    'max_reps': '',
                    'rep_unit': '',
                    'rest': '',
                    'notes': '',
                },
            ],
        )
        self.assertEqual({r['start'] for r in result['rows']}, {'2030-02-03'})

    def test_account_phase_and_window_are_reviewed_inputs(self):
        tz = source(workout(501, exercise(11)))
        self.assertTrue(normalize(tz, **INPUTS)['ok'])
        self.assertEqual(
            errors(normalize(tz, **{**INPUTS, 'account_id': 701})), [(None, None, 'accountID')]
        )
        self.assertEqual(
            errors(normalize(tz, **{**INPUTS, 'phase_id': 43})), [(None, None, 'phase')]
        )
        moved = normalize(tz, **{**INPUTS, 'end': '2030-03-09'})
        self.assertEqual(errors(moved), [(None, None, 'phase')])
        self.assertIn('not the reviewed 2030-02-03 to 2030-03-09', moved['errors'][0]['message'])
        # The earlier phase of the same snapshot imports with its own window
        earlier = {
            **INPUTS,
            'phase_id': 41,
            'start': '2030-01-06',
            'end': '2030-02-02',
            'today': datetime.date(2030, 1, 10),
        }
        self.assertTrue(normalize(source(), **earlier)['ok'])

    def test_finished_phase_is_refused(self):
        tz = source(workout(501, exercise(11)))
        self.assertTrue(normalize(tz, **{**INPUTS, 'today': datetime.date(2030, 3, 2)})['ok'])
        ended = normalize(tz, **{**INPUTS, 'today': datetime.date(2030, 3, 3)})
        self.assertEqual(ended['errors'][0]['message'], 'Phase 42 ended on 2030-03-02.')

    def test_missing_mapping_names_every_exercise(self):
        result = normalize(
            source(workout(501, exercise(11), exercise(99)), workout(502, exercise(98))),
            **INPUTS,
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['rows'], [])
        self.assertEqual(errors(result), [(501, 99, 'exercise'), (502, 98, 'exercise')])
        self.assertEqual(
            result['errors'][0]['message'],
            'Source 99 [Trainerize 99]: no validated wger exercise mapping.',
        )

    def test_long_text_is_refused_not_truncated(self):
        long_day = workout(501, exercise(11, note='n' * 101), instructions='i' * 1008)
        result = normalize(source(long_day), **INPUTS)
        self.assertEqual(errors(result), [(501, None, 'day_description'), (501, 11, 'notes')])
        self.assertEqual(
            [e['message'] for e in result['errors']],
            [
                '1008 characters, at most 1000; not truncated. Supply reviewed text for it.',
                '101 characters, at most 100; not truncated.',
            ],
        )
        # Exactly at the limits is fine, and so is target wording plus note up to 100
        at_limit = workout(
            501,
            exercise(11, target='10 reps each', note='n' * 86),
            instructions='i' * 1000,
            name='d' * 20,
        )
        self.assertTrue(normalize(source(at_limit), **INPUTS)['ok'])
        over = workout(501, exercise(11, target='10 reps each', note='n' * 87))
        self.assertEqual(errors(normalize(source(over), **INPUTS)), [(501, 11, 'notes')])

    def test_reviewed_text_replaces_and_is_reported(self):
        long_day = workout(501, exercise(11), instructions='i' * 1008, name='n' * 21)
        reviewed = {'501': {'description': 'Reviewed shorter text', 'name': 'Mobility'}}
        result = normalize(source(long_day), **INPUTS, reviewed=reviewed)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['rows'][0]['day_description'], 'Reviewed shorter text')
        self.assertEqual(result['rows'][0]['day_name'], 'Mobility')
        self.assertEqual(
            result['reviewed'],
            [
                {'workout': 501, 'field': 'day_name', 'source': 'n' * 21},
                {'workout': 501, 'field': 'day_description', 'source': 'i' * 1008},
            ],
        )
        stray = normalize(source(long_day), **INPUTS, reviewed={**reviewed, '999': {}})
        self.assertIn((999, None, 'reviewed'), [(int(w), x, f) for w, x, f in errors(stray)])

    def test_unsupported_structures_are_explicit(self):
        result = normalize(
            source(
                workout(501, exercise(11, supersetType='circuit', superSetID=3)),
                workout(502, exercise(11, side='left')),
                workout(503, timed(11, 30.0, recordType='rest')),
                workout(504, exercise(11), type='workoutInterval'),
                workout(505, exercise(11), rounds=3),
                workout(506, exercise(11, targetDetail={'type': 10, 'text': 'AMRAP'})),
                workout(507, exercise(11, targetDetail={'type': 2, 'time': None})),
                workout(508, exercise(11, targetDetail={'type': 4, 'distance': 1.5})),
                workout(
                    509,
                    exercise(11, superSetID=5, supersetType='superset'),
                    exercise(12),
                    exercise(13, superSetID=5, supersetType='superset'),
                ),
                workout(510, exercise(11, supersetType='superset')),
            ),
            **INPUTS,
        )
        self.assertEqual(
            errors(result),
            [
                (501, 11, 'supersetType'),
                (502, 11, 'side'),
                (503, 11, 'recordType'),
                (504, None, 'type'),
                (505, None, 'rounds'),
                (506, 11, 'target'),
                (507, 11, 'target'),
                (508, 11, 'target'),
                (509, 13, 'superSetID'),
                (510, 11, 'superSetID'),
            ],
        )
        self.assertEqual(result['rows'], [])

    def test_completed_workouts_are_history_not_plan(self):
        done = workout(501, exercise(11), status='tracked')
        logged = workout(502, exercise(12, stats=[{'id': 0, 'setID': 1, 'reps': 10}]))
        blank_stats = workout(503, exercise(13, stats=[{'id': 0, 'setID': 1, 'reps': None}]))
        result = normalize(source(done, logged, blank_stats), **INPUTS)
        self.assertEqual(errors(result), [(501, None, 'status'), (502, None, 'status')])

    def test_same_input_same_rows(self):
        tz = source(workout(501, exercise(11), timed(12, 45.0)))
        self.assertEqual(normalize(tz, **INPUTS), normalize(copy.deepcopy(tz), **INPUTS))


def history():
    return (
        sorted(WorkoutSession.objects.values_list(), key=str),
        sorted(WorkoutLog.objects.values_list(), key=str),
    )


class ApplyTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='test')
        self.tz = source(
            workout(
                501,
                exercise(11, target='8-12 reps', sets=4, rest=90, note='Pause at the bottom'),
                exercise(12, superSetID=7, supersetType='superset'),
                timed(13, 30.0, superSetID=7, supersetType='superset'),
            ),
            workout(502),
        )

    def test_preview_apply_reconcile(self):
        before = history()
        self.assertEqual(reconcile(self.tz, self.user, **INPUTS)['status'], 'absent')
        shown = preview(self.tz, self.user, **INPUTS)
        self.assertTrue(shown['ok'], shown['errors'])
        self.assertEqual(shown['diff']['create'], {'days': 2, 'slots': 2, 'entries': 3})
        self.assertFalse(Routine.objects.filter(name='Synthetic TZ phase').exists())

        routine = apply(self.tz, self.user, shown['plan_hash'], **INPUTS)
        self.assertEqual(routine.user, self.user)
        self.assertEqual(
            (routine.start, routine.end), (datetime.date(2030, 2, 3), datetime.date(2030, 3, 2))
        )
        superset = routine.days.get(order=1).slots.get(order=2)
        self.assertEqual(
            [(e.exercise_id, e.repetition_unit.name) for e in superset.entries.order_by('order')],
            [(2, 'Repetitions'), (3, 'Seconds')],
        )
        self.assertEqual(reconcile(self.tz, self.user, **INPUTS)['status'], 'match')
        self.assertEqual(history(), before)

        # Applying the same preview again is refused, nothing more is written
        with self.assertRaises(Refused):
            apply(self.tz, self.user, shown['plan_hash'], **INPUTS)
        self.assertEqual(Routine.objects.filter(name='Synthetic TZ phase').count(), 1)

        # A later edit shows up as a difference
        SlotEntry.objects.filter(slot__day__routine=routine, exercise_id=1).update(comment='x')
        drift = reconcile(self.tz, self.user, **INPUTS)
        self.assertEqual(drift['status'], 'differs')
        self.assertEqual(
            drift['differences'],
            [{'row': 2, 'column': 'notes', 'expected': 'Pause at the bottom', 'actual': 'x'}],
        )

    def test_changed_source_or_mapping_is_refused(self):
        plan_hash = preview(self.tz, self.user, **INPUTS)['plan_hash']
        moved = {**INPUTS, 'mapping': {**INPUTS['mapping'], '11': 4}}
        with self.assertRaises(Refused) as refused:
            apply(self.tz, self.user, plan_hash, **moved)
        self.assertEqual(
            refused.exception.errors, [{'message': 'The source or data changed since the preview.'}]
        )
        with self.assertRaises(Refused) as refused:
            apply(source(workout(501, exercise(99))), self.user, plan_hash, **INPUTS)
        self.assertEqual(refused.exception.errors[0]['source'], {'workout': 501, 'exercise': 99})
        self.assertFalse(Routine.objects.filter(name='Synthetic TZ phase').exists())

    def test_spreadsheet_errors_trace_to_source(self):
        # A mapping to an exercise that doesn't exist passes normalize, the plan refuses it
        tz = source(workout(501, exercise(11), exercise(12)))
        shown = preview(tz, self.user, **{**INPUTS, 'mapping': {'11': 1, '12': 999999}})
        self.assertFalse(shown['ok'])
        self.assertEqual(
            [(e['source'], e['column']) for e in shown['errors']],
            [({'workout': 501, 'exercise': 12}, 'exercise_id')],
        )

    def test_failed_apply_writes_nothing(self):
        plan_hash = preview(self.tz, self.user, **INPUTS)['plan_hash']
        before = (Routine.objects.count(), SlotEntry.objects.count(), history())
        with (
            patch.object(trainerize_program, '_differences', return_value=[{'row': 2}]),
            self.assertRaises(Refused),
        ):
            apply(self.tz, self.user, plan_hash, **INPUTS)
        self.assertEqual((Routine.objects.count(), SlotEntry.objects.count(), history()), before)
