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

# Standard Library
import datetime
import threading
import uuid
import zoneinfo
from decimal import Decimal
from unittest import (
    TestCase,
    skipUnless,
)
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import F
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

# Third Party
from rest_framework import status
from rest_framework.test import APIClient

# wger
from wger.core.tests.base_testcase import BaseTestCase
from wger.manager import history_integrity
from wger.manager.history_integrity import (
    encode_row,
    encode_value,
    fingerprint,
)
from wger.manager.models import (
    WorkoutLog,
    WorkoutSession,
)


START = datetime.datetime(2026, 3, 1, 6, 30, tzinfo=datetime.UTC)


class CanonicalEncodingTestCase(TestCase):
    def test_decimal_uses_column_scale_and_unsigned_zero(self):
        self.assertEqual(encode_value(Decimal('1.5'), 2), '1.50')
        self.assertEqual(encode_value(Decimal('82.750'), 2), '82.75')
        self.assertEqual(encode_value(Decimal('1E+1'), 3), '10.000')
        self.assertEqual(encode_value(Decimal('0'), 2), '0.00')
        self.assertEqual(encode_value(Decimal('-0.00'), 1), '0.0')

    def test_datetime_is_the_utc_instant(self):
        sydney = START.astimezone(zoneinfo.ZoneInfo('Australia/Sydney'))
        self.assertEqual(encode_value(sydney), '2026-03-01T06:30:00.000000Z')
        self.assertEqual(encode_value(START), encode_value(sydney))
        with self.assertRaises(ValueError):
            encode_value(START.replace(tzinfo=None))

    def test_null_zero_and_types_stay_distinct(self):
        pk = uuid.UUID('AAAAAAAA-0000-0000-0000-00000000000A')
        self.assertEqual(
            encode_row([pk, None, 0, Decimal('0'), '0'], [None, 2, None, 2, None]),
            b'["aaaaaaaa-0000-0000-0000-00000000000a",null,0,"0.00","0"]\n',
        )
        self.assertEqual(encode_row([False, 0, True], [None, None, None]), b'[false,0,true]\n')
        with self.assertRaises(TypeError):
            encode_value(0.5)

    def test_row_framing_is_unambiguous(self):
        self.assertNotEqual(
            encode_row(['a","b'], [None]) + encode_row(['c'], [None]),
            encode_row(['a', 'b'], [None, None]) + encode_row(['c'], [None]),
        )
        self.assertEqual(encode_row(['x\ny'], [None]).count(b'\n'), 1)
        self.assertEqual(encode_row(['café'], [None]), b'["caf\\u00e9"]\n')


class HistoryIntegrityTestCase(BaseTestCase, TransactionTestCase):
    """
    Autocommit, so fingerprint() opens its own transaction as it does in production
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.other = User.objects.get(username='test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse('workoutsession-integrity')

    def session(self, user=None, **kwargs):
        return WorkoutSession.objects.create(
            user=user or self.user, datetime_start=START, notes='private note', **kwargs
        )

    def log(self, session, **kwargs):
        values = {'weight': Decimal('80'), 'repetitions': Decimal('5'), 'date': START}
        values.update(kwargs)
        return WorkoutLog.objects.create(
            user=session.user, session=session, exercise_id=1, **values
        )

    def test_route_does_not_shadow_session_detail(self):
        self.assertEqual(self.url, '/api/v2/workoutsession/integrity/')
        pk = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000001'
        response = self.client.get(reverse('workoutsession-detail', args=[pk]))
        self.assertEqual(response.data['id'], pk)

    def test_requires_login_and_is_get_only(self):
        self.assertIn(APIClient().get(self.url).status_code, (401, 403))
        for method in ('post', 'put', 'patch', 'delete'):
            response = getattr(self.client, method)(self.url, {})
            self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_owner_only_and_no_owner_selector(self):
        response = self.client.get(self.url, {'user': self.other.pk, 'user_id': self.other.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected = {**fingerprint(self.user.pk), 'as_of': response.json()['as_of']}
        self.assertEqual(response.json(), expected)
        tables = response.json()['tables']
        self.assertEqual(tables['workout_session']['count'], 4)
        self.assertEqual(tables['workout_log']['count'], 4)

        foreign = self.session(user=self.other)
        self.log(foreign)
        WorkoutSession.objects.filter(user=self.other).update(notes='changed')
        after = self.client.get(self.url).json()
        self.assertEqual(after['snapshot_id'], response.json()['snapshot_id'])

    def test_response_is_bounded_and_leaks_no_content(self):
        small = self.client.get(self.url)
        for _ in range(30):
            self.log(self.session(), weight=Decimal('123.45'))
        response = self.client.get(self.url)
        body = response.content
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertLess(len(body), 2048)
        self.assertLessEqual(len(body) - len(small.content), 2)
        for secret in (b'private note', b'Notes come here', b'123.45', b'bbbbbbbb-'):
            self.assertNotIn(secret, body)

    def test_insertion_order_does_not_matter(self):
        ids = [uuid.UUID(int=n) for n in (3, 1, 2)]

        def build(order):
            WorkoutSession.objects.filter(user=self.user).delete()
            for pk in order:
                self.log(self.session(id=pk), id=uuid.UUID(int=pk.int + 100), weight=pk.int)
            return fingerprint(self.user.pk)

        first = build(ids)
        second = build(reversed(ids))
        self.assertEqual(first['snapshot_id'], second['snapshot_id'])
        self.assertEqual(first['tables'], second['tables'])

    def test_add_edit_delete_and_null_versus_zero_change_digest(self):
        session = self.session()
        log = self.log(session)
        seen = {fingerprint(self.user.pk)['snapshot_id']}

        def changed(label):
            snapshot = fingerprint(self.user.pk)['snapshot_id']
            self.assertNotIn(snapshot, seen, label)
            seen.add(snapshot)

        WorkoutLog.objects.filter(pk=log.pk).update(calories=Decimal('0'))
        changed('null to zero')
        WorkoutLog.objects.filter(pk=log.pk).update(calories=Decimal('0.01'))
        changed('decimal edit')
        WorkoutSession.objects.filter(pk=session.pk).update(
            datetime_start=START + datetime.timedelta(microseconds=1)
        )
        changed('timestamp edit')
        WorkoutSession.objects.filter(pk=session.pk).update(
            datetime_end=F('datetime_start'), time_unknown=True
        )
        changed('time unknown flag')
        self.log(session, weight=None, repetitions=None, duration=Decimal('600'))
        changed('log added')
        WorkoutLog.objects.filter(pk=log.pk).delete()
        changed('log deleted')
        session.delete()
        changed('session deleted')

    def test_every_concrete_field_is_declared(self):
        for _, model, fields in history_integrity.TABLES:
            self.assertEqual(sorted(fields), sorted(f.attname for f in model._meta.concrete_fields))
        narrowed = [(name, model, fields[:-1]) for name, model, fields in history_integrity.TABLES]
        with patch.object(history_integrity, 'TABLES', narrowed):
            with self.assertRaises(ImproperlyConfigured):
                fingerprint(self.user.pk)

    def test_issues_no_writes(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.url)
        statements = [q['sql'].lstrip().split()[0].upper() for q in queries.captured_queries]
        self.assertTrue(statements)
        self.assertTrue(
            # DECLARE: PostgreSQL server-side cursors for the streamed SELECTs
            set(statements)
            <= {'SELECT', 'SET', 'BEGIN', 'COMMIT', 'SAVEPOINT', 'RELEASE', 'DECLARE'},
            statements,
        )

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL snapshot isolation')
    def test_concurrent_commit_between_table_scans_is_not_seen(self):
        session = self.session()
        self.log(session)
        before = fingerprint(self.user.pk)
        real_table = history_integrity._table

        def commit_elsewhere():
            try:
                new = self.session()
                self.log(new)
                WorkoutSession.objects.filter(pk=session.pk).update(notes='edited')
            finally:
                connection.close()

        def table_then_mutate(model, fields, user_id):
            result = real_table(model, fields, user_id)
            if model is WorkoutSession:
                writer = threading.Thread(target=commit_elsewhere)
                writer.start()
                writer.join()
            return result

        with patch.object(history_integrity, '_table', side_effect=table_then_mutate):
            during = fingerprint(self.user.pk)

        self.assertEqual(during['tables'], before['tables'])
        self.assertEqual(during['snapshot_id'], before['snapshot_id'])
        after = fingerprint(self.user.pk)
        self.assertEqual(
            after['tables']['workout_log']['count'], before['tables']['workout_log']['count'] + 1
        )
        self.assertNotEqual(after['snapshot_id'], before['snapshot_id'])
