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

"""Outbound gym-plan planner, synthetic routines and events only."""

# Standard Library
import datetime
from types import SimpleNamespace

# Django
from django.test import SimpleTestCase

# wger
from wger.intervals.outbound import desired_events, payload_hash, plan_outbound
from wger.intervals.planning import PlanError
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST
from wger.manager.dataclasses import SetConfigData, SlotData


SITE = 'https://gym.example'
NAMES = {10: 'Squat', 11: 'Bench Press'}
ROUTINE = SimpleNamespace(id=3, name='Strength block')
PUSH = SimpleNamespace(id=7, name='Push', is_rest=False)
REST = SimpleNamespace(id=8, name='', is_rest=True)
SLOTS = [
    SlotData(comment='', sets=[SetConfigData(exercise=10, sets=3, repetitions=5)]),
    SlotData(comment='pause at bottom', sets=[SetConfigData(exercise=11, repetitions=8)]),
]


def day(date, which=PUSH, slots=SLOTS):
    return ROUTINE, SimpleNamespace(
        day=which, date=datetime.date(2040, 6, date), slots_display_mode=slots
    )


def desired(*occurrences):
    return desired_events(occurrences, OLDEST, NEWEST, NAMES, SITE)


def remote(payload, event_id=900, **changes):
    fields = ('category', 'type', 'start_date_local', 'name', 'description', 'external_id')
    return {'id': event_id, 'athlete_id': ATHLETE, **{f: payload[f] for f in fields}} | changes


def link(payload, event_id=900, state='active'):
    return {
        'external_id': payload['external_id'],
        'intervals_event_id': event_id,
        'pushed_hash': payload_hash(payload),
        'date': payload['date'],
        'state': state,
    }


def plan(want, links=(), events=(), **kw):
    return plan_outbound(ATHLETE, OLDEST, NEWEST, want, list(links), list(events), **kw)


def actions(result):
    return {
        a: [i['external_id'] for i in items]
        for a, items in result.items()
        if a != 'skipped' and items
    }


class DesiredEventsTest(SimpleTestCase):
    def test_only_training_days_in_window_become_text_only_weight_training_events(self):
        placeholder = (
            ROUTINE,
            SimpleNamespace(day=None, date=datetime.date(2040, 6, 22), slots_display_mode=[]),
        )

        want = desired(day(21), day(22, REST), placeholder, day(19), day(24))

        self.assertEqual(list(want), ['wger-gym:3:2040-06-21', 'wger-gym:3:2040-06-24'])
        event = want['wger-gym:3:2040-06-21']
        self.assertEqual(
            {k: event[k] for k in ('category', 'type', 'start_date_local', 'name')},
            {
                'category': 'WORKOUT',
                'type': 'WeightTraining',
                'start_date_local': '2040-06-21T00:00:00',
                'name': 'Push',
            },
        )
        lines = event['description'].split('\n')
        self.assertTrue(lines[0].startswith('Squat: 3 '))
        self.assertTrue(
            lines[1].startswith('Bench Press: 8 ') and lines[1].endswith('(pause at bottom)')
        )
        self.assertEqual(lines[-1], 'Open in wger: https://gym.example/en/routine/3/view')
        self.assertFalse({'moving_time', 'icu_training_load'} & event.keys())

    def test_two_training_days_on_one_date_refuse(self):
        with self.assertRaises(PlanError):
            desired(day(21), day(21))


class PlanOutboundTest(SimpleTestCase):
    def test_new_occurrences_create_and_foreign_events_are_never_touched(self):
        want = desired(day(21))
        foreign = {
            'id': 1,
            'athlete_id': ATHLETE,
            'type': 'WeightTraining',
            'name': 'Push',
            'external_id': None,
        }

        result = plan(want, events=[foreign])

        self.assertEqual(actions(result), {'create': ['wger-gym:3:2040-06-21']})
        self.assertEqual(result['skipped']['foreign_events'], 1)

    def test_pushed_and_untouched_is_unchanged_then_edit_in_wger_is_update(self):
        want = desired(day(21))['wger-gym:3:2040-06-21']
        links, events = [link(want)], [remote(want)]
        self.assertEqual(
            actions(plan({want['external_id']: want}, links, events)),
            {'unchanged': [want['external_id']]},
        )

        renamed = desired(day(21, SimpleNamespace(id=7, name='Push heavy', is_rest=False)))

        [update] = plan(renamed, links, events)['update']
        self.assertEqual(
            (update['external_id'], update['intervals_event_id']), (want['external_id'], 900)
        )
        self.assertEqual(update['payload']['name'], 'Push heavy')

    def test_moved_date_deletes_the_old_event_and_creates_the_new_one(self):
        old = desired(day(21))['wger-gym:3:2040-06-21']

        result = plan(desired(day(23)), [link(old)], [remote(old)])

        self.assertEqual(
            actions(result),
            {'create': ['wger-gym:3:2040-06-23'], 'delete': ['wger-gym:3:2040-06-21']},
        )

    def test_edited_in_intervals_is_conflict_unless_overwrite(self):
        want = desired(day(21))
        pushed = want['wger-gym:3:2040-06-21']
        edited = remote(pushed, description='my own notes')

        self.assertEqual(
            plan(want, [link(pushed)], [edited])['conflict'][0]['reason'], 'edited in Intervals'
        )
        self.assertEqual(
            actions(plan(want, [link(pushed)], [edited], overwrite=True)),
            {'update': [pushed['external_id']]},
        )

    def test_crash_after_post_adopts_matching_remote_and_conflicts_otherwise(self):
        want = desired(day(21))
        pushed = want['wger-gym:3:2040-06-21']

        [adopt] = plan(want, events=[remote(pushed, event_id=901)])['adopt']
        self.assertEqual(adopt['intervals_event_id'], 901)
        self.assertEqual(
            actions(plan(want, events=[remote(pushed, name='other')])),
            {'conflict': [pushed['external_id']]},
        )

    def test_remote_deleted_is_recreate_and_undesired_gone_is_forget(self):
        want = desired(day(21))
        pushed = want['wger-gym:3:2040-06-21']

        self.assertEqual(actions(plan(want, [link(pushed)])), {'recreate': [pushed['external_id']]})
        self.assertEqual(actions(plan({}, [link(pushed)])), {'forget': [pushed['external_id']]})
        self.assertEqual(actions(plan({}, [link(pushed, state='deleted')])), {})

    def test_our_prefix_without_ledger_row_is_left_alone_and_duplicates_conflict(self):
        stray = desired(day(25))['wger-gym:3:2040-06-25']
        result = plan({}, events=[remote(stray)])
        self.assertEqual((actions(result), result['skipped']['unlinked_ours']), ({}, 1))

        want = desired(day(21))
        pushed = want['wger-gym:3:2040-06-21']
        doubled = plan(want, [link(pushed)], [remote(pushed), remote(pushed, event_id=902)])
        self.assertEqual(doubled['conflict'][0]['reason'], 'several remote events')

    def test_foreign_athlete_event_refuses_the_plan(self):
        with self.assertRaises(PlanError):
            plan(
                {},
                events=[{'id': 1, 'athlete_id': 'i9999', 'external_id': 'wger-gym:3:2040-06-21'}],
            )
