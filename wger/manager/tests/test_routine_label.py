"""Owner-only routine label API (wger-gym#26)"""

# Django
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.test import APIClient

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import (
    Label,
    Routine,
    RoutineRecovery,
    SlotEntry,
    WorkoutLog,
    WorkoutSession,
)


URL = '/api/v2/routine-label/'

# Routine 1 (admin) runs 2024-03-01..2024-06-01, so its last offset is 92


class RoutineLabelTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.get(username='admin')
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        entry = SlotEntry.objects.filter(slot__day__routine_id=1).first()
        session = WorkoutSession.objects.create(
            user=self.user, routine_id=1, day=entry.slot.day, datetime_start=timezone.now()
        )
        self.log = WorkoutLog.objects.create(
            user=self.user,
            session=session,
            routine_id=1,
            slot_entry=entry,
            exercise_id=1,
            repetitions=5,
            weight=100,
        )

    def history(self):
        return list(WorkoutLog.objects.values()), list(WorkoutSession.objects.values())

    def create(self, start, end, label='Deload', routine=1, client=None):
        return (client or self.api).post(
            URL,
            {'routine': routine, 'start_offset': start, 'end_offset': end, 'label': label},
            format='json',
        )

    def test_crud_and_readback(self):
        before = self.history()
        r = self.create(28, 34)
        self.assertEqual(r.status_code, 201, r.content)
        pk = r.json()['id']
        self.assertEqual(
            r.json(),
            {
                'id': pk,
                'routine': 1,
                'start_offset': 28,
                'end_offset': 34,
                'label': 'Deload',
                'comment': '',
            },
        )

        # Zero-based and inclusive: start + 28 days .. start + 34 days
        sequence = self.api.get(
            reverse('routine-date-sequence-display-mode', kwargs={'pk': 1})
        ).json()
        labelled = [d['date'] for d in sequence if d['label'] == 'Deload']
        self.assertEqual(
            (labelled[0], labelled[-1], len(labelled)), ('2024-03-29', '2024-04-04', 7)
        )

        r = self.api.patch(f'{URL}{pk}/', {'label': 'Test week', 'end_offset': 30}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Label.objects.get(pk=pk).end_offset, 30)

        listed = self.api.get(f'{URL}?routine=1').json()
        self.assertEqual([row['id'] for row in listed['results']], [pk])
        self.assertIn('count', listed)

        self.assertEqual(self.api.delete(f'{URL}{pk}/').status_code, 204)
        self.assertFalse(Label.objects.filter(pk=pk).exists())
        self.assertEqual(self.history(), before)

    def test_ranges_and_overlaps(self):
        self.assertEqual(self.create(0, 27, 'Block 1').status_code, 201)
        self.assertEqual(self.create(28, 28, 'Single day').status_code, 201)
        self.assertEqual(self.create(29, 92, 'Block 2').status_code, 201)

        refused = {
            'end_offset': [(5, 4), (90, 93)],
            'start_offset': [(27, 27), (20, 40), (-1, 3)],
        }
        for field, spans in refused.items():
            for start, end in spans:
                r = self.create(start, end, 'Bad')
                self.assertEqual(r.status_code, 400, (start, end, r.content))
                self.assertIn(field, r.json(), (start, end))
        self.assertEqual(self.create(0, 1, 'x' * 36).status_code, 400)
        self.assertEqual(Label.objects.filter(routine_id=1).count(), 3)

        # An update must not overlap its neighbours, but may keep its own span
        single = Label.objects.get(label='Single day')
        r = self.api.patch(f'{URL}{single.pk}/', {'end_offset': 29}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.api.patch(f'{URL}{single.pk}/', {'comment': 'Test'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

        # Moving a label to another routine is checked against that routine
        r = self.api.patch(f'{URL}{single.pk}/', {'routine': 2}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_only_the_owner(self):
        pk = self.create(0, 6).json()['id']
        other = APIClient()
        other.force_authenticate(User.objects.get(username='test'))
        self.assertEqual(other.get(f'{URL}{pk}/').status_code, 404)
        self.assertEqual(other.get(f'{URL}?routine=1').json()['count'], 0)
        self.assertEqual(
            other.patch(f'{URL}{pk}/', {'label': 'Mine'}, format='json').status_code, 404
        )
        self.assertEqual(other.delete(f'{URL}{pk}/').status_code, 404)
        self.assertEqual(self.create(10, 12, client=other).status_code, 403)

        # Public templates are no exception
        public = Routine.objects.get(pk=5)
        self.assertTrue(public.is_public)
        self.assertEqual(self.create(0, 0, routine=5).status_code, 403)

        self.assertEqual(APIClient().get(URL).status_code, 403)
        self.assertEqual(Label.objects.get(pk=pk).label, 'Deload')

    def test_no_trainer_logged_in_as_the_owner(self):
        """A trainer's real trainer-login session neither reads nor writes the member's labels"""
        Routine.objects.filter(pk=2).update(end='2024-03-31')
        pk = Label.objects.create(routine_id=2, start_offset=0, end_offset=6, label='Deload').pk
        self.user_login('trainer1')
        r = self.client.post(reverse('core:user:trainer-login', args=[2]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), 2)

        new = {'routine': 2, 'start_offset': 10, 'end_offset': 12, 'label': 'Mine'}
        responses = (
            self.client.get(URL),
            self.client.get(f'{URL}{pk}/'),
            self.client.post(URL, new, content_type='application/json'),
            self.client.patch(f'{URL}{pk}/', {'label': 'x'}, content_type='application/json'),
            self.client.delete(f'{URL}{pk}/'),
        )
        self.assertEqual([r.status_code for r in responses], [404] * 5)
        self.assertEqual(list(Label.objects.values_list('pk', 'label')), [(pk, 'Deload')])
        self.assertFalse(RoutineRecovery.objects.exists())

    def test_writes_are_recorded_and_undoable(self):
        before = self.history()
        rev = self.api.get('/api/v2/routine/1/revision/').json()['revision']
        r = self.create(0, 6)
        recovery_id = r['X-Routine-Recovery-Id']
        self.assertEqual(RoutineRecovery.objects.get(pk=recovery_id).operation, 'edit')

        current = self.api.get('/api/v2/routine/1/revision/').json()['revision']
        self.assertEqual(r['X-Routine-Revision'], current)
        restored = self.api.post(
            f'/api/v2/routine/recoveries/{recovery_id}/restore/',
            {'expected_revision': current, 'idempotency_key': 'undo'},
            format='json',
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        self.assertFalse(Label.objects.filter(routine_id=1).exists())
        self.assertEqual(self.api.get('/api/v2/routine/1/revision/').json()['revision'], rev)
        self.assertEqual(self.history(), before)

        # A trashed routine takes no label writes
        self.api.post(
            '/api/v2/routine/1/trash/',
            {'expected_revision': rev, 'idempotency_key': 'trash'},
            format='json',
        )
        r = self.create(0, 6)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()['code'], 'routine_trashed')
