#  This file is part of wger Workout Manager <https://github.com/wger-project>.
#
#  wger Workout Manager is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  wger Workout Manager is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Standard Library
import copy
import datetime
import json
import threading
from unittest import skipUnless

# Django
from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework import status

# wger
from wger.core.tests.base_testcase import (
    BaseTestCase,
    WgerTestCase,
)
from wger.manager import routine_preview
from wger.manager.models import (
    Label,
    RoutinePreview,
)


URL = reverse('routine-preview')

# Weeks 1-4 build, week 5 is the supplied deload, 6-12 a second block
WEIGHTS = ['60', '62.5', '65', '67.5', '50', '62.5', '65', '67.5', '70', '72.5', '75', '77.5']


def config(value, iteration=1):
    return {'iteration': iteration, 'value': value, 'operation': 'r'}


def twelve_weeks(**routine):
    """Mon/Wed/Fri training plus 4 rest days, a superset and every field kind"""
    squat = {
        'exercise': 1,
        'order': 1,
        'comment': 'Pause at the bottom',
        'configs': {
            'sets': [config(4), config(2, 5), config(4, 6)],
            'repetitions': [config('8')],
            'max_repetitions': [config('10')],
            'weight': [config(w, i) for i, w in enumerate(WEIGHTS, 1)],
            'rir': [config('1.5')],
            'rest': [config(180)],
        },
    }
    superset = [
        {
            'exercise': 2,
            'order': 1,
            'weight_unit': 2,
            'weight_rounding': '2.5',
            'configs': {
                'sets': [config(3)],
                'repetitions': [config('12')],
                'weight': [config('41'), config('43.7', 2)],
            },
        },
        {
            'exercise': 3,
            'order': 2,
            'repetition_unit': 3,
            'configs': {
                'sets': [config(3)],
                'repetitions': [config('30')],
                'max_rest': [config(90)],
                'rest': [config(60)],
            },
        },
    ]
    days = [
        {'order': 1, 'name': 'Mon Lower', 'slots': [{'order': 1, 'entries': [squat]}]},
        {'order': 2, 'name': 'Tue rest', 'is_rest': True},
        {
            'order': 3,
            'name': 'Wed Upper',
            'slots': [{'order': 1, 'comment': 'Superset', 'entries': superset}],
        },
        {'order': 4, 'name': 'Thu rest', 'is_rest': True},
        {'order': 5, 'name': 'Fri Lower', 'slots': [{'order': 1, 'entries': [squat]}]},
        {'order': 6, 'name': 'Sat rest', 'is_rest': True},
        {'order': 7, 'name': 'Sun rest', 'is_rest': True},
    ]
    return {
        'version': 1,
        'routine': {
            'name': '12wk Strength',
            'description': 'Synthetic',
            'start': '2026-10-05',
            'end': '2026-12-27',
            'fit_in_week': False,
            **routine,
        },
        'labels': [
            {'start_offset': 0, 'end_offset': 27, 'label': 'Block 1'},
            {'start_offset': 28, 'end_offset': 34, 'label': 'Deload'},
            {'start_offset': 35, 'end_offset': 83, 'label': 'Block 2'},
        ],
        'days': days,
    }


def body(proposal=None, key='key-1', version='v1'):
    return {
        'schema_version': 1,
        'external_version': version,
        'idempotency_key': key,
        'proposal': proposal or twelve_weeks(),
    }


def training_counts():
    """Rows of every manager table except the preview itself"""
    return {
        model._meta.label: model.objects.count()
        for model in apps.get_app_config('manager').get_models()
        if model is not RoutinePreview
    }


def without(data, key):
    return {k: v for k, v in data.items() if k != key}


def save_natively(client, proposal):
    """
    The proposal saved through the native API, as a client would after approval

    Labels have no API endpoint, so they are the one thing saved directly.
    """

    def post(name, data):
        response = client.post(
            reverse(f'{name}-list'), json.dumps(data), content_type='application/json'
        )
        assert response.status_code == status.HTTP_201_CREATED, (name, response.content)
        return response.json()['id']

    routine = post('routine', proposal['routine'])
    for label in proposal['labels']:
        Label.objects.create(routine_id=routine, **label)
    for raw_day in proposal['days']:
        day = post('day', {'routine': routine, **without(raw_day, 'slots')})
        for raw_slot in raw_day.get('slots', []):
            slot = post('slot', {'day': day, **without(raw_slot, 'entries')})
            for raw_entry in raw_slot['entries']:
                entry = post('slot-entry', {'slot': slot, **without(raw_entry, 'configs')})
                for key, rows in raw_entry['configs'].items():
                    for row in rows:
                        post(f'{key.replace("_", "-")}-config', {'slot_entry': entry, **row})
    return routine


def strip_ids(value):
    """Database ids only exist on the saved routine"""
    if isinstance(value, dict):
        return {
            k: strip_ids(v)
            for k, v in value.items()
            if k not in ('id', 'routine', 'slot_entry_id', 'exercise')
        }
    if isinstance(value, list):
        return [strip_ids(v) for v in value]
    return value


class RoutinePreviewTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user_login('test')

    def post(self, data):
        return self.client.post(URL, data, content_type='application/json')

    def test_schedule_matches_native_readback(self):
        """Every date, iteration, label and target equals the saved routine's readback"""
        proposal = twelve_weeks()
        response = self.post(body(proposal))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        routine = save_natively(self.client, copy.deepcopy(proposal))
        native = self.client.get(
            reverse('routine-date-sequence-display-mode', kwargs={'pk': routine})
        )
        self.assertEqual(strip_ids(response.data['schedule']), strip_ids(native.json()))

        schedule = response.data['schedule']
        self.assertEqual(len(schedule), 84)
        mondays = [d for d in schedule if d['day'] and d['day']['name'] == 'Mon Lower']
        self.assertEqual([d['iteration'] for d in mondays], list(range(1, 13)))
        self.assertEqual(mondays[4]['label'], 'Deload')
        self.assertEqual(mondays[4]['slots'][0]['sets'][0]['sets'], 2)
        self.assertEqual(mondays[5]['slots'][0]['sets'][0]['sets'], 4)
        self.assertEqual(schedule[1]['day']['is_rest'], True)
        self.assertEqual(schedule[-1]['date'], '2026-12-27')

    def test_fit_in_week_pads_to_monday(self):
        proposal = twelve_weeks(fit_in_week=True)
        proposal['days'] = [d for d in proposal['days'] if not d.get('is_rest')]
        for i, day in enumerate(proposal['days'], 1):
            day['order'] = i
        response = self.post(body(proposal))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        routine = save_natively(self.client, copy.deepcopy(proposal))
        native = self.client.get(
            reverse('routine-date-sequence-display-mode', kwargs={'pk': routine})
        )
        self.assertEqual(strip_ids(response.data['schedule']), strip_ids(native.json()))
        padding = [d for d in response.data['schedule'] if d['day'] is None]
        self.assertEqual(len(padding), 4 * 12)

    def test_writes_only_the_preview(self):
        before = training_counts()
        response = self.post(body())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.client.get(response.data['preview_url'])
        self.client.get(reverse('routine-preview-detail', args=[response.data['preview_id']]))
        self.assertEqual(training_counts(), before)
        self.assertEqual(RoutinePreview.objects.count(), 1)

    def test_replay_and_versions(self):
        first = self.post(body())
        again = self.post(body())
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertTrue(again.data['replayed'])
        self.assertEqual(again.data['preview_id'], first.data['preview_id'])

        # Same key, other body
        changed = twelve_weeks()
        changed['days'][0]['name'] = 'Monday'
        conflict = self.post(body(changed))
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(conflict.data['code'], 'idempotency_conflict')

        # New key, same plan: the existing version, not a copy
        same = self.post(body(key='key-2'))
        self.assertEqual(same.data['preview_id'], first.data['preview_id'])

        # New content is a new immutable version, the first is untouched
        second = self.post(body(changed, key='key-3', version='v2'))
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(second.data['plan_hash'], first.data['plan_hash'])
        stored = self.client.get(reverse('routine-preview-detail', args=[first.data['preview_id']]))
        self.assertEqual(stored.data['canonical_proposal'], first.data['canonical_proposal'])
        self.assertEqual(stored.data['plan_hash'], first.data['plan_hash'])
        self.assertEqual(RoutinePreview.objects.count(), 2)

        with self.assertRaises(ValueError):
            RoutinePreview.objects.get(pk=first.data['preview_id']).save()

    def test_hash_ignores_number_spelling(self):
        spelled = twelve_weeks()
        spelled['days'][0]['slots'][0]['entries'][0]['configs']['weight'][1]['value'] = 62.50
        a = self.post(body())
        b = self.post(body(spelled, key='key-2', version='v1'))
        self.assertEqual(a.data['plan_hash'], b.data['plan_hash'])

    def test_isolation(self):
        created = self.post(body())
        detail = reverse('routine-preview-detail', args=[created.data['preview_id']])
        page = created.data['preview_url']
        self.assertTrue(page.endswith(f'/en/routine/preview/{created.data["preview_id"]}/'))

        self.assertEqual(self.client.get(page).status_code, status.HTTP_200_OK)

        # A trainer acting as the owner sees nothing
        session = self.client.session
        session['trainer.identity'] = 3
        session.save()
        self.assertEqual(self.client.get(detail).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(page).status_code, status.HTTP_404_NOT_FOUND)

        self.user_logout()
        self.assertEqual(self.client.get(detail).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.get(page).status_code, status.HTTP_302_FOUND)
        self.assertEqual(self.post(body(key='x')).status_code, status.HTTP_403_FORBIDDEN)

        for other in ('admin', 'trainer1'):
            self.user_login(other)
            self.assertEqual(self.client.get(detail).status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(self.client.get(page).status_code, status.HTTP_404_NOT_FOUND)

    def test_expiry(self):
        created = self.post(body())
        detail = reverse('routine-preview-detail', args=[created.data['preview_id']])
        RoutinePreview.objects.filter(pk=created.data['preview_id']).update(
            expires_at=timezone.now() - datetime.timedelta(seconds=1)
        )
        response = self.client.get(detail)
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertEqual(response.data['code'], 'preview_expired')
        self.assertEqual(
            self.client.get(created.data['preview_url']).status_code, status.HTTP_410_GONE
        )

        # A new POST drops expired rows, so the same key makes a fresh version
        fresh = self.post(body())
        self.assertEqual(fresh.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(fresh.data['preview_id'], created.data['preview_id'])
        self.assertFalse(RoutinePreview.objects.filter(pk=created.data['preview_id']).exists())

    def test_rejects_unsupported_structures(self):
        cases = {
            'days': lambda p: p['days'].pop(),
            'days[0].need_logs_to_advance': lambda p: p['days'][0].update(
                need_logs_to_advance=True
            ),
            'days[0].slots[0].entries[0].configs.weight[1].operation': lambda p: p['days'][0][
                'slots'
            ][0]['entries'][0]['configs']['weight'][1].update(operation='+'),
            'days[0].slots[0].entries[0].configs.rir[0].repeat': lambda p: p['days'][0]['slots'][0][
                'entries'
            ][0]['configs']['rir'][0].update(repeat=True),
            'days[0].slots[0].entries[0].configs.weight[11].iteration': lambda p: p['days'][0][
                'slots'
            ][0]['entries'][0]['configs']['weight'][11].update(iteration=13),
            'days[0].slots[0].entries[0].configs.max_rest': lambda p: p['days'][0]['slots'][0][
                'entries'
            ][0]['configs'].update(max_rest=[config(601)]),
            'days[0].slots[0].entries[0].configs.rir': lambda p: p['days'][0]['slots'][0][
                'entries'
            ][0]['configs'].update(rir=[config('1.2')]),
            'days[0].slots[0].entries[0].exercise': lambda p: p['days'][0]['slots'][0]['entries'][
                0
            ].update(exercise=999999),
            'routine.end': lambda p: p['routine'].update(end='2027-02-03'),
            'routine.surprise': lambda p: p['routine'].update(surprise=1),
            'labels[1].start_offset': lambda p: p['labels'][1].update(start_offset=27),
        }
        before = training_counts()
        for i, (path, change) in enumerate(cases.items()):
            proposal = twelve_weeks()
            change(proposal)
            response = self.post(body(proposal, key=f'bad-{i}'))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, path)
            paths = [e['path'] for e in response.data['errors']]
            self.assertTrue(any(p.startswith(path) for p in paths), (path, paths))
        self.assertEqual(RoutinePreview.objects.count(), 0)
        self.assertEqual(training_counts(), before)

    def test_fit_in_week_needs_monday(self):
        proposal = twelve_weeks(fit_in_week=True, start='2026-10-06', end='2026-12-28')
        proposal['days'] = proposal['days'][:3]
        response = self.post(body(proposal))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['errors'][0]['code'], 'unsupported')

    def test_trainer_logged_in_as_the_owner_cannot_post(self):
        """No lookup or write: new key, replayed key, same plan and reused key all 404"""
        created = self.post(body())
        self.user_logout()
        self.user_login('trainer1')
        r = self.client.post(reverse('core:user:trainer-login', args=[2]))
        self.assertEqual(r.status_code, status.HTTP_302_FOUND)
        self.assertEqual(int(self.client.session['_auth_user_id']), 2)

        changed = twelve_weeks()
        changed['days'][0]['name'] = 'Other'
        for data in (body(key='new'), body(), body(key='same-plan'), body(changed)):
            response = self.post(data)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, data)
            self.assertNotIn(created.data['preview_id'], response.content.decode())
        self.assertEqual(
            [str(pk) for pk in RoutinePreview.objects.values_list('pk', flat=True)],
            [created.data['preview_id']],
        )

    def test_private_headers(self):
        created = self.post(body())
        detail = self.client.get(
            reverse('routine-preview-detail', args=[created.data['preview_id']])
        )
        for response in (created, detail, self.post({})):
            self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_body_bounds(self):
        too_big = json.dumps(body()) + ' ' * routine_preview.MAX_BODY_BYTES
        response = self.client.post(URL, too_big, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        for broken in ({**body(), 'extra': 1}, {**body(), 'schema_version': 2}, [1]):
            response = self.post(broken)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, broken)
        missing = body()
        del missing['idempotency_key']
        self.assertEqual(self.post(missing).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.client.post(URL, 'nope', content_type='application/json').status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        # Deep nesting within the size limit is a bad request, not a server error
        nested = '[' * 100_000 + ']' * 100_000
        response = self.client.post(URL, nested, content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['code'], 'invalid_request')


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locks')
class RoutinePreviewConcurrencyTestCase(BaseTestCase, TransactionTestCase):
    """Parallel POSTs of one owner: one row per key or plan, never over the limit"""

    def race(self, bodies):
        user = User.objects.get(username='test')
        barrier = threading.Barrier(len(bodies))
        results = []

        def post(data):
            try:
                barrier.wait()
                results.append(routine_preview.create(user, data)[1])
            except routine_preview.PreviewError as e:
                results.append(e.code)
            finally:
                connection.close()

        threads = [threading.Thread(target=post, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return sorted(results, key=str)

    def test_same_key(self):
        self.assertEqual(self.race([body()] * 4), [False, True, True, True])
        self.assertEqual(RoutinePreview.objects.count(), 1)

    def test_same_plan_new_keys(self):
        self.assertEqual(
            self.race([body(key=f'k{i}') for i in range(4)]), [False, True, True, True]
        )
        self.assertEqual(RoutinePreview.objects.count(), 1)

    def test_limit(self):
        def plan(i):
            proposal = twelve_weeks()
            proposal['routine']['name'] = f'Plan {i}'
            return body(proposal, key=f'k{i}')

        user = User.objects.get(username='test')
        for i in range(routine_preview.MAX_ACTIVE_PREVIEWS - 2):
            routine_preview.create(user, plan(i))
        results = self.race([plan(100 + i) for i in range(5)])
        self.assertEqual(results, [False, False] + ['too_many_previews'] * 3)
        self.assertEqual(RoutinePreview.objects.count(), routine_preview.MAX_ACTIVE_PREVIEWS)
