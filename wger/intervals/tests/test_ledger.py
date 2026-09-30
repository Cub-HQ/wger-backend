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

"""IntervalsEventLink push ledger: identity, retry and ownership. Synthetic only."""

# Standard Library
import datetime

# Django
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.intervals.models import IntervalsEventLink
from wger.intervals.outbound import external_id, payload_hash, plan_outbound
from wger.intervals.tests.test_outbound import day, desired, remote
from wger.intervals.tests.test_planning import ATHLETE, NEWEST, OLDEST
from wger.manager.models import Day, Routine, WorkoutLog, WorkoutSession


LINK_FIELDS = ('external_id', 'intervals_event_id', 'pushed_hash', 'date', 'state')


class IntervalsEventLinkTest(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.get(username='admin')
        self.routine = Routine.objects.get(pk=1)
        self.want = desired(day(21))
        self.payload = self.want[external_id(3, datetime.date(2040, 6, 21))]

    def record(self, user=None, payload=None, **fields):
        payload = payload or self.payload
        return IntervalsEventLink.objects.create(
            user=user or self.admin,
            external_id=payload['external_id'],
            routine=self.routine,
            day=Day.objects.filter(routine=self.routine).first(),
            date=payload['date'],
            intervals_event_id=900,
            pushed_hash=payload_hash(payload),
            pushed_at=timezone.now(),
            **fields,
        )

    def links(self, user=None):
        return list(IntervalsEventLink.objects.filter(user=user or self.admin).values(*LINK_FIELDS))

    def plan(self, want, events):
        return plan_outbound(ATHLETE, OLDEST, NEWEST, want, self.links(), events)

    def test_one_ledger_row_per_user_and_occurrence(self):
        self.record()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.record()

        self.record(user=User.objects.get(username='test'))
        self.assertEqual(
            IntervalsEventLink.objects.filter(external_id=self.payload['external_id']).count(), 2
        )

    def test_retry_after_recorded_create_is_unchanged_not_a_second_post(self):
        self.record()

        result = self.plan(self.want, [remote(self.payload)])

        self.assertEqual(
            [i['external_id'] for i in result['unchanged']], [self.payload['external_id']]
        )
        self.assertEqual(result['create'] + result['adopt'] + result['update'], [])

    def test_retry_after_unrecorded_create_adopts_the_remote_event(self):
        result = self.plan(self.want, [remote(self.payload, event_id=901)])

        self.assertEqual(
            result['adopt'],
            [{'external_id': self.payload['external_id'], 'intervals_event_id': 901}],
        )
        self.assertEqual(result['create'], [])

    def test_ledger_row_survives_routine_deletion_so_its_event_can_be_deleted(self):
        link = self.record()

        WorkoutLog.objects.filter(routine=self.routine).delete()
        WorkoutSession.objects.filter(routine=self.routine).delete()
        self.routine.delete()

        link.refresh_from_db()
        self.assertEqual((link.routine, link.day), (None, None))
        result = self.plan({}, [remote(self.payload)])
        [delete] = result['delete']
        self.assertEqual(
            (delete['external_id'], delete['intervals_event_id']),
            (self.payload['external_id'], 900),
        )

    def test_deleted_row_is_history_and_gives_no_remote_ownership(self):
        self.record(state='deleted')

        result = self.plan({}, [remote(self.payload)])

        self.assertEqual(result['delete'] + result['forget'], [])
        self.assertEqual(result['skipped']['unlinked_ours'], 1)

    def test_ledger_is_per_owner(self):
        self.record(user=User.objects.get(username='test'))

        result = self.plan(self.want, [])

        self.assertEqual(
            [i['external_id'] for i in result['create']], [self.payload['external_id']]
        )
