"""Completed-session export against synthetic HTTP responses; never contacts Intervals."""

# Standard Library
import io
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

# Django
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings

# wger
from wger.core.tests.base_testcase import BaseTestCase
from wger.intervals import export
from wger.intervals.client import IntervalsError
from wger.intervals.models import IntervalsActivityLink, IntervalsEventLink
from wger.intervals.planning import PlanError
from wger.intervals.tests.test_command import KEY, response
from wger.manager.models import WorkoutLog, WorkoutSession


ATHLETE = 'i4242'
START = datetime(2040, 6, 24, 10, tzinfo=timezone.utc)


class FakeCompletedIntervals:
    def __init__(self):
        self.athlete = {'id': ATHLETE, 'icu_permission': 'WRITE', 'timezone': 'UTC'}
        self.activities = {}
        self.events = []
        self.calls = []
        self.fail_post = False
        self.mismatch = False
        self.lost = False
        # PUT behaviour: None applies it; '503' refuses; 'lost' applies then answers 503;
        # 'partial' drops kg_lifted; 'rewrite' stores another description.
        self.put = None
        self.put_done = False
        self.readback = {}  # merged into activity GETs once a PUT has landed
        self.on_get = None  # one-shot edit of the stored row just before an activity GET

    def __call__(self, method, url, params=None, json=None, **kwargs):
        assert kwargs['auth'] == ('API_KEY', KEY) and kwargs['timeout']
        path = url.split('/api/v1/')[1]
        self.calls.append((method, path, json))
        if method == 'POST':
            # The pending ledger row is already committed (autocommit, no outer atomic).
            assert not connection.in_atomic_block and connection.get_autocommit()
            assert IntervalsActivityLink.objects.filter(intervals_activity_id=None).exists()
        if method == 'GET':
            if path == 'athlete/0':
                return response(dict(self.athlete))
            if path == 'athlete/0/activities':
                return response([dict(row) for row in self.activities.values()])
            if path == 'athlete/0/events':
                return response([dict(row) for row in self.events])
            if path.startswith('activity/'):
                if self.lost:
                    return response({}, 404)
                row = self.activities[path.split('/')[-1]]
                if self.on_get:
                    self.on_get(row)
                    self.on_get = None
                return response({**row, **(self.readback if self.put_done else {})})
        if method == 'PUT' and path.startswith('activity/'):
            mode, self.put = self.put, None
            if mode == '503':
                return response({}, 503)
            row = self.activities[path.split('/')[-1]]
            row.update(json)
            if mode == 'partial':
                row['kg_lifted'] = None
            if mode == 'rewrite':
                row['description'] = 'Remote rewrote the description'
            self.put_done = True
            return response({}, 503) if mode == 'lost' else response(dict(row))
        if method == 'POST' and path == 'athlete/0/activities/manual':
            if self.fail_post:
                self.fail_post = False
                return response({}, 503)
            activity_id = f'i{900001 + len(self.activities)}'
            row = {**json, 'id': activity_id, 'icu_athlete_id': ATHLETE}
            if self.mismatch:
                row['description'] = 'Remote changed the recorded sets'
            self.activities[activity_id] = row
            return response({'id': activity_id})
        raise AssertionError(f'Unexpected HTTP request: {method} {path}')

    def writes(self):
        return [(method, path, body) for method, path, body in self.calls if method != 'GET']


@override_settings(
    INTERVALS_API_KEY=KEY,
    INTERVALS_ATHLETE_ID=ATHLETE,
    INTERVALS_WGER_USERNAME='admin',
    TIME_ZONE='UTC',
)
class CompletedSessionExportTest(BaseTestCase, TransactionTestCase):
    """Autocommit, like the command: apply refuses to run inside a transaction."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.user.userprofile.time_zone = 'UTC'
        self.user.userprofile.save(update_fields=['time_zone'])
        self.session = WorkoutSession.objects.create(
            user=self.user,
            datetime_start=START,
            datetime_end=START + timedelta(minutes=45),
        )
        self.log = WorkoutLog.objects.create(
            user=self.user,
            session=self.session,
            exercise_id=1,
            date=START,
            repetitions=14,
            weight='87.5',
            repetitions_unit_id=1,
            weight_unit_id=1,
        )
        IntervalsEventLink.objects.create(
            user=self.user,
            external_id='wger-gym:synthetic:2040-06-24',
            date=START.date(),
            intervals_event_id=7000,
            pushed_hash='a' * 64,
            pushed_at=START,
        )
        self.fake = FakeCompletedIntervals()
        patcher = mock.patch('wger.intervals.client.requests.request', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def history(self):
        return tuple(
            list(model.objects.order_by('pk').values())
            for model in (WorkoutSession, WorkoutLog, IntervalsEventLink)
        )

    def invoke(self, function, *args, **kwargs):
        before = self.history()
        try:
            return function(*args, **kwargs)
        finally:
            self.assertEqual(self.history(), before)

    def preview(self, **kwargs):
        return self.invoke(export.preview, self.user, self.session.pk, **kwargs)

    def apply(self, plan, **kwargs):
        return self.invoke(export.apply, self.user, self.session.pk, plan['plan_hash'], **kwargs)

    def command(self, *args):
        out = io.StringIO()
        self.invoke(
            call_command,
            'intervals-export-session',
            '--session',
            str(self.session.pk),
            *args,
            stdout=out,
        )
        return json.loads(out.getvalue())

    def test_preview_then_one_manual_post_then_unchanged(self):
        plan = self.command()
        self.assertEqual(plan['action'], 'create')
        self.assertEqual(plan['conflicts'], [])
        self.assertIn('checks', plan)
        self.assertIn('unsupported_fields', plan)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)
        payload = plan['payload']
        self.assertEqual(
            set(payload),
            {
                'type',
                'start_date_local',
                'elapsed_time',
                'name',
                'description',
                'external_id',
                'kg_lifted',
            },
        )
        self.assertEqual(payload['kg_lifted'], 1225.0)
        self.assertEqual(payload['type'], 'WeightTraining')
        self.assertEqual(payload['start_date_local'], '2040-06-24T10:00:00')
        self.assertEqual(payload['elapsed_time'], 2700)
        self.assertIn(str(self.session.pk), payload['external_id'])
        self.assertIn('14 reps × 87.5 kg', payload['description'])

        applied = self.command('--apply', '--plan-hash', plan['plan_hash'])
        self.assertIsNone(applied['failed'])
        self.assertEqual(len(applied['done']), 1)
        self.assertEqual(self.fake.writes(), [('POST', 'athlete/0/activities/manual', payload)])
        activity_id = applied['intervals_activity_id']
        self.assertIn(str(activity_id), applied['link'])
        self.assertIn(('GET', f'activity/{activity_id}', None), self.fake.calls)
        self.assertEqual(IntervalsActivityLink.objects.get().intervals_activity_id, activity_id)
        again = self.preview()
        self.assertEqual(again['action'], 'unchanged')
        self.apply(again)
        self.assertEqual(len(self.fake.writes()), 1)

    def test_payload_preserves_recorded_units_and_zero_values_not_targets(self):
        WorkoutLog.objects.create(
            user=self.user,
            session=self.session,
            exercise_id=1,
            date=START + timedelta(minutes=1),
            repetitions=0,
            weight=0,
            repetitions_unit_id=1,
            weight_unit_id=2,
            duration=30,
            distance=2,
            distance_unit_id=6,
            level=0,
            rir=0,
            repetitions_target=99,
            weight_target=999,
        )
        plan = self.preview()
        description = plan['payload']['description']
        for recorded in (
            '14 reps × 87.5 kg',
            '0 reps × 0 lb',
            '30 s',
            '2 Kilometers',
            'level 0',
        ):
            self.assertIn(recorded, description)
        # RiR is self-rated effort: stored, but never sent.
        self.assertNotIn('rir', description.lower())
        self.assertIn('RiR', plan['unsupported_fields'])
        self.assertNotIn('× 999 lb', description)
        self.assertNotIn('99 reps', description)
        self.assertEqual(self.fake.writes(), [])

    def test_invalid_sessions_never_write(self):
        for changes in (
            {'time_unknown': True, 'datetime_end': START},
            {'datetime_end': None},
        ):
            with self.subTest(changes=changes):
                WorkoutSession.objects.filter(pk=self.session.pk).update(**changes)
                with self.assertRaises(PlanError):
                    self.preview()
                WorkoutSession.objects.filter(pk=self.session.pk).update(
                    time_unknown=False, datetime_end=START + timedelta(minutes=45)
                )
        empty = WorkoutSession.objects.create(
            user=self.user,
            datetime_start=START + timedelta(days=1),
            datetime_end=START + timedelta(days=1, minutes=30),
        )
        with self.assertRaises(PlanError):
            self.invoke(export.preview, self.user, empty.pk)
        WorkoutLog.objects.filter(pk=self.log.pk).update(
            repetitions=None, weight=None, repetitions_target=12, weight_target=80
        )
        with self.assertRaises(PlanError):
            self.preview()
        WorkoutLog.objects.filter(pk=self.log.pk).update(repetitions=14, weight='87.5')
        other = User.objects.create_user(username='synthetic-export-other')
        with self.assertRaises(PlanError):
            self.invoke(export.preview, other, self.session.pk)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_wrong_athlete_missing_write_and_timezone_mismatch_refuse(self):
        self.fake.athlete['id'] = 'i9999'
        with self.assertRaises(IntervalsError):
            self.preview()
        self.fake.athlete['id'] = ATHLETE
        plan = self.preview()
        self.fake.athlete['icu_permission'] = 'READ'
        with self.assertRaises(IntervalsError):
            self.apply(plan)
        self.fake.athlete['icu_permission'] = 'WRITE'
        self.fake.athlete['timezone'] = 'Australia/Sydney'
        with self.assertRaises(PlanError):
            self.preview()
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_conflicts_block_overlap_strength_plans_and_duplicate_external_ids(self):
        payload = self.preview()['payload']
        ride = {
            'id': 'i8001',
            'type': 'Ride',
            'start_date_local': '2040-06-24T10:15:00',
            'elapsed_time': 1800,
            'icu_athlete_id': ATHLETE,
        }
        strength = {
            **ride,
            'type': 'WeightTraining',
            'start_date_local': '2040-06-24T18:00:00',
        }
        planned = {
            'id': 7001,
            'category': 'WORKOUT',
            'type': 'WeightTraining',
            'start_date_local': '2040-06-24T00:00:00',
            'athlete_id': ATHLETE,
        }
        duplicates = [
            {**payload, 'id': activity_id, 'icu_athlete_id': ATHLETE}
            for activity_id in ('i8002', 'i8003')
        ]
        for activities, events in (
            ([ride], []),
            ([strength], []),
            ([], [planned]),
            (duplicates, []),
        ):
            with self.subTest(activities=activities, events=events):
                self.fake.activities = {row['id']: row for row in activities}
                self.fake.events = events
                plan = self.preview()
                self.assertEqual(plan['action'], 'conflict')
                self.assertTrue(plan['conflicts'])
                self.apply(plan)
                self.assertEqual(self.fake.writes(), [])
                self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_ambiguous_or_touched_date_candidates_conflict_but_prior_day_plan_does_not(self):
        walk = {'id': 'i8101', 'type': 'Walk', 'icu_athlete_id': ATHLETE}
        # Session 23:30 → 00:30: both local dates are touched.
        WorkoutSession.objects.filter(pk=self.session.pk).update(
            datetime_start=START.replace(hour=23, minute=30),
            datetime_end=START.replace(hour=23, minute=30) + timedelta(hours=1),
        )
        for activities, events, expected in (
            # No duration and started before the session ended: overlap unknown.
            ([{**walk, 'start_date_local': '2040-06-24T23:40:00'}], [], 'conflict'),
            ([{**walk, 'start_date_local': '2040-06-24T21:00:00'}], [], 'conflict'),
            # Previous-day activity running into the session.
            (
                [{**walk, 'start_date_local': '2040-06-23T23:00:00', 'elapsed_time': 90000}],
                [],
                'conflict',
            ),
            # Strength or a planned workout on the second touched date.
            (
                [{**walk, 'type': 'WeightTraining', 'start_date_local': '2040-06-25T18:00:00'}],
                [],
                'conflict',
            ),
            (
                [],
                [
                    {
                        'id': 7101,
                        'category': 'WORKOUT',
                        'start_date_local': '2040-06-25T00:00:00',
                        'athlete_id': ATHLETE,
                    }
                ],
                'conflict',
            ),
            # Not ambiguous: after the session, or a plan the day before only.
            ([{**walk, 'start_date_local': '2040-06-25T01:00:00'}], [], 'create'),
            (
                [{**walk, 'start_date_local': '2040-06-23T12:00:00', 'elapsed_time': 600}],
                [
                    {
                        'id': 7102,
                        'category': 'WORKOUT',
                        'start_date_local': '2040-06-23T00:00:00',
                        'athlete_id': ATHLETE,
                    }
                ],
                'create',
            ),
        ):
            with self.subTest(activities=activities, events=events):
                self.fake.activities = {row['id']: row for row in activities}
                self.fake.events = events
                plan = self.preview()
                self.assertEqual(plan['action'], expected, plan['conflicts'])
                self.assertEqual(plan['checks']['window'], ['2040-06-23', '2040-06-25'])
                if expected == 'conflict':
                    self.apply(plan)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_apply_inside_a_transaction_refuses_before_any_request(self):
        plan = self.preview()
        calls = len(self.fake.calls)
        with self.assertRaises(PlanError), transaction.atomic():
            self.apply(plan)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_entire_remote_batch_is_validated_before_adopting_a_match(self):
        payload = self.preview()['payload']
        match = {**payload, 'id': 'i8001', 'icu_athlete_id': ATHLETE}
        for collection, owner_field in (('activities', 'icu_athlete_id'), ('events', 'athlete_id')):
            unrelated = {
                'id': 'i8002',
                owner_field: ATHLETE,
                'type': 'Ride',
                'category': 'NOTE',
                'start_date_local': '2040-06-24T18:00:00',
                'elapsed_time': 60,
            }
            for invalid in (
                {owner_field: 'i9999'},
                {'start_date_local': 'invalid'},
                {'start_date_local': '2040-06-25T18:00:00'},
            ):
                with self.subTest(collection=collection, invalid=invalid):
                    self.fake.activities = {'i8001': match}
                    self.fake.events = []
                    row = {**unrelated, **invalid}
                    if collection == 'activities':
                        self.fake.activities['i8002'] = row
                    else:
                        self.fake.events = [row]
                    with self.assertRaises(PlanError):
                        self.preview()
                    self.assertEqual(self.fake.writes(), [])
                    self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_matching_remote_is_adopted_without_post_or_existing_ledger(self):
        payload = self.preview()['payload']
        self.fake.activities['i8001'] = {**payload, 'id': 'i8001', 'icu_athlete_id': ATHLETE}
        plan = self.preview()
        self.assertEqual(plan['action'], 'adopt')
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)
        applied = self.apply(plan)
        self.assertIsNone(applied['failed'])
        self.assertEqual(applied['intervals_activity_id'], 'i8001')
        self.assertEqual(IntervalsActivityLink.objects.get().intervals_activity_id, 'i8001')
        self.assertEqual(self.preview()['action'], 'unchanged')
        self.assertEqual(self.fake.writes(), [])

    def test_503_requires_explicit_pending_retry(self):
        plan = self.preview()
        self.fake.fail_post = True
        failed = self.apply(plan)
        self.assertIsNotNone(failed['failed'])
        self.assertEqual(failed['done'], [])
        self.assertIsNone(IntervalsActivityLink.objects.get().intervals_activity_id)
        pending = self.preview()
        self.assertEqual(pending['action'], 'conflict')
        self.assertTrue(pending['conflicts'])
        self.apply(pending)
        self.assertEqual(len(self.fake.writes()), 1)
        retry = self.preview(retry_pending=True)
        self.assertEqual(retry['action'], 'retry')
        applied = self.apply(retry, retry_pending=True)
        self.assertIsNone(applied['failed'])
        self.assertEqual(len(self.fake.activities), 1)
        self.assertEqual(len(self.fake.writes()), 2)
        self.assertEqual(self.preview()['action'], 'unchanged')

    def test_readback_mismatch_saves_identity_and_never_reposts(self):
        plan = self.preview()
        self.fake.mismatch = True
        failed = self.apply(plan)
        self.assertIsNotNone(failed['failed'])
        self.assertEqual(failed['done'], [])
        link = IntervalsActivityLink.objects.get()
        self.assertIn(link.intervals_activity_id, self.fake.activities)
        for retry in (False, True):
            again = self.preview(retry_pending=retry)
            self.assertEqual(again['action'], 'conflict')
            self.apply(again, retry_pending=retry)
        self.assertEqual(len(self.fake.writes()), 1)

    def test_readback_404_is_a_recorded_failure_that_never_reposts(self):
        plan = self.preview()
        self.fake.lost = True
        failed = self.apply(plan)
        self.assertEqual(failed['failed']['error'], 'GET activity/i900001: HTTP 404')
        self.assertEqual(failed['done'], [])
        self.assertEqual(len(self.fake.writes()), 1)
        self.assertIsNone(IntervalsActivityLink.objects.get().intervals_activity_id)

    def test_log_mutation_invalidates_hash_and_command_requires_hash(self):
        plan = self.preview()
        WorkoutLog.objects.filter(pk=self.log.pk).update(repetitions=15)
        with self.assertRaises(PlanError):
            self.apply(plan)
        self.assertNotEqual(self.preview()['plan_hash'], plan['plan_hash'])
        with self.assertRaises(CommandError):
            self.command('--apply')
        with self.assertRaises(CommandError):
            self.command('--apply', '--plan-hash', plan['plan_hash'])
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(IntervalsActivityLink.objects.count(), 0)

    def test_weight_lifted_sums_recorded_kg_and_lb_and_states_what_is_left_out(self):
        base = {'user': self.user, 'session': self.session, 'date': START, 'exercise_id': 1}
        for fields in (
            # 10 × 20 lb = 90.718 kg: converted, per-dumbbell weight not doubled.
            {'repetitions': 10, 'weight': 20, 'repetitions_unit_id': 1, 'weight_unit_id': 2},
            # Zero load is a recorded weight: counted, adds nothing.
            {'repetitions': 15, 'weight': 0, 'repetitions_unit_id': 1, 'weight_unit_id': 1},
            # Never invented: no weight, no reps, body weight, or seconds instead of reps.
            {'repetitions': 14, 'repetitions_unit_id': 1},
            {'weight': 40, 'weight_unit_id': 1, 'weight_target': 40, 'repetitions_target': 8},
            {'repetitions': 8, 'weight': 10, 'repetitions_unit_id': 1, 'weight_unit_id': 3},
            {'repetitions': 30, 'weight': 20, 'repetitions_unit_id': 3, 'weight_unit_id': 1},
        ):
            WorkoutLog.objects.create(**base, **fields)
        name = self.log.exercise.get_translation().name
        # 14 × 87.5 kg + 90.718 kg = 1315.718 kg over 3 of 7 sets.
        plan = self.preview()
        self.assertEqual(plan['payload']['kg_lifted'], 1315.7)
        lines = plan['payload']['description'].splitlines()
        self.assertEqual(
            lines[1],
            'Weight lifted: 1315.7 kg (also in the Intervals Weight Lifted field)'
            ' = recorded weight × reps over 3 of 7 sets.'
            ' Weights as logged per set; dumbbell and per-side loads are not doubled.',
        )
        self.assertEqual(
            lines[2], f'Not counted (no weight in kg or lb, or no rep count): {name} (4 sets)'
        )
        WorkoutLog.objects.filter(pk=self.log.pk).update(weight=None)
        WorkoutLog.objects.filter(weight_unit_id__in=(1, 2)).update(weight_unit_id=3)
        plan = self.preview()
        self.assertIsNone(plan['payload']['kg_lifted'])
        self.assertEqual(
            plan['payload']['description'].splitlines()[1],
            'Weight lifted: unavailable; no set has both a weight in kg or lb and a rep count.',
        )
        self.assertEqual(self.fake.writes(), [])

    def exported(self, legacy=False):
        """An activity exported before this feature: no weight line, kg_lifted empty.

        legacy: the ledger holds the pre-feature hash of the six raw fields."""
        with mock.patch('wger.intervals.export._volume', return_value=([], None)):
            self.apply(self.preview())
        link = IntervalsActivityLink.objects.get()
        row = self.fake.activities[link.intervals_activity_id]
        if legacy:
            link.pushed_hash = export._hash({field: row[field] for field in export.LEGACY_FIELDS})
            link.save()
        return link, row

    def test_existing_export_is_updated_in_place_once_then_never_again(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                IntervalsActivityLink.objects.all().delete()
                self.fake.activities, self.fake.calls = {}, []
                old, before = self.exported(legacy)
                before = dict(before)
                activity_id = old.intervals_activity_id
                plan = self.preview()
                self.assertEqual(plan['action'], 'update')
                self.assertEqual(plan['update_fields'], ['description', 'kg_lifted'])
                applied = self.command('--apply', '--plan-hash', plan['plan_hash'])
                self.assertIsNone(applied['failed'])
                self.assertEqual(applied['intervals_activity_id'], activity_id)
                put = {
                    'description': plan['payload']['description'],
                    'kg_lifted': 1225.0,
                }
                self.assertEqual(self.fake.writes()[-1], ('PUT', f'activity/{activity_id}', put))
                self.assertEqual(list(self.fake.activities), [activity_id])
                after = self.fake.activities[activity_id]
                self.assertIn('Weight lifted: 1225 kg (', after['description'])
                for field in ('type', 'start_date_local', 'elapsed_time', 'name', 'external_id'):
                    self.assertEqual(after[field], before[field])
                link = IntervalsActivityLink.objects.get()
                self.assertEqual(link.intervals_activity_id, activity_id)
                self.assertEqual(link.pushed_hash, export._hash(export._owned(plan['payload'])))
                again = self.preview()
                self.assertEqual(again['action'], 'unchanged')
                self.apply(again)
                self.assertEqual(len(self.fake.writes()), 2)

    def test_update_never_overwrites_a_remote_edit_or_recreates_a_lost_activity(self):
        for legacy in (False, True):
            IntervalsActivityLink.objects.all().delete()
            self.fake.activities = {}
            link, row = self.exported(legacy)
            writes = len(self.fake.writes())
            for change in (
                {'name': 'Renamed in Intervals'},
                {'elapsed_time': 60},
                # Weight Lifted typed in Intervals is not ours, whatever the ledger age.
                {'kg_lifted': 999.0},
            ):
                with self.subTest(legacy=legacy, change=change):
                    self.fake.activities = {link.intervals_activity_id: {**row, **change}}
                    plan = self.preview()
                    self.assertEqual(plan['action'], 'conflict')
                    self.assertIn('edited in Intervals', plan['conflicts'][0])
                    self.assertIsNotNone(self.apply(plan)['failed'])
            for activities in ({}, {'i8009': {**row, 'id': 'i8009'}}):
                with self.subTest(legacy=legacy, activities=activities):
                    self.fake.activities = activities
                    plan = self.preview()
                    self.assertEqual(plan['action'], 'conflict')
                    self.apply(plan)
            self.assertEqual(len(self.fake.writes()), writes)

    def test_destination_is_re_read_and_must_match_the_preview_before_put(self):
        link, row = self.exported()
        activity_id = link.intervals_activity_id
        for edit in (
            lambda r: r.update(name='Edited between list and GET'),
            lambda r: r.update(kg_lifted=500.0),
            lambda r: r.update(icu_athlete_id='i9999'),
            lambda r: r.update(external_id='someone-else'),
        ):
            with self.subTest(edit=edit):
                self.fake.activities = {activity_id: dict(row)}
                plan = self.preview()
                self.assertEqual(plan['action'], 'update')
                self.fake.on_get = edit
                failed = self.apply(plan)
                self.assertIsNotNone(failed['failed'])
                self.assertEqual(IntervalsActivityLink.objects.get().pushed_hash, link.pushed_hash)
        self.assertEqual([w for w in self.fake.writes() if w[0] == 'PUT'], [])

    def test_changed_destination_or_ledger_invalidates_an_approved_update(self):
        link, row = self.exported()
        plan = self.preview()
        # Another export in between: remote and ledger moved together, still 'update'.
        row['name'] = 'Renamed and re-exported'
        IntervalsActivityLink.objects.filter(pk=link.pk).update(
            pushed_hash=export._hash(export._owned(row))
        )
        moved = self.preview()
        self.assertEqual(moved['action'], 'update')
        with self.assertRaises(PlanError):
            self.apply(plan)
        self.assertEqual([w for w in self.fake.writes() if w[0] == 'PUT'], [])

    def test_wrong_readback_identity_or_body_keeps_the_last_proven_ledger(self):
        # A 1 kg total, so a boolean readback (True == 1.0 in Python) must still fail.
        WorkoutLog.objects.filter(pk=self.log.pk).update(repetitions=1, weight=1)
        link, row = self.exported()
        for put, readback in (
            (None, {'id': 'i8888'}),
            (None, {'icu_athlete_id': 'i9999'}),
            (None, {'external_id': 'someone-else'}),
            (None, {'kg_lifted': True}),
            (None, {'kg_lifted': float('inf')}),
            (None, {'kg_lifted': '1.0'}),
            ('rewrite', {}),
            ('partial', {}),
        ):
            with self.subTest(put=put, readback=readback):
                self.fake.activities = {link.intervals_activity_id: dict(row)}
                self.fake.put, self.fake.readback, self.fake.put_done = put, readback, False
                failed = self.apply(self.preview())
                self.assertIsNotNone(failed['failed'])
                self.assertEqual(
                    IntervalsActivityLink.objects.get().intervals_activity_id,
                    link.intervals_activity_id,
                )
                self.assertEqual(IntervalsActivityLink.objects.get().pushed_hash, link.pushed_hash)
                self.fake.readback = {}
                if put:
                    # What landed is neither ours nor what was sent: never overwritten.
                    self.assertEqual(self.preview()['action'], 'conflict')
        self.assertEqual(len(self.fake.activities), 1)

    def test_uncertain_or_unrecorded_update_reconciles_without_a_second_write(self):
        link, row = self.exported()
        plan = self.preview()
        self.fake.put = '503'
        self.assertIsNotNone(self.apply(plan)['failed'])
        self.assertEqual(IntervalsActivityLink.objects.get().pushed_hash, link.pushed_hash)
        self.assertEqual(self.preview()['action'], 'update')
        for outcome in ('lost', 'database'):
            with self.subTest(outcome=outcome):
                self.fake.activities[link.intervals_activity_id] = dict(row)
                plan = self.preview()
                self.assertEqual(plan['action'], 'update')
                if outcome == 'lost':
                    self.fake.put = 'lost'
                    failed = self.apply(plan)
                else:
                    with mock.patch.object(
                        IntervalsActivityLink.objects,
                        'update_or_create',
                        side_effect=DatabaseError('database unavailable'),
                    ):
                        failed = self.apply(plan)
                self.assertIsNotNone(failed['failed'])
                self.assertEqual(IntervalsActivityLink.objects.get().pushed_hash, link.pushed_hash)
                # The PUT landed unrecorded: adopt what Intervals holds, never re-send.
                puts = len(self.fake.writes())
                adopt = self.preview()
                self.assertEqual(adopt['action'], 'adopt')
                self.assertIsNone(self.apply(adopt)['failed'])
                self.assertEqual(
                    IntervalsActivityLink.objects.get().pushed_hash,
                    export._hash(export._owned(plan['payload'])),
                )
                self.assertEqual(self.preview()['action'], 'unchanged')
                self.assertEqual(len(self.fake.writes()), puts)
                IntervalsActivityLink.objects.filter(pk=link.pk).update(
                    pushed_hash=link.pushed_hash
                )
        self.assertEqual(len(self.fake.activities), 1)
