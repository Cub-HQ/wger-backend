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

"""Gym cardio station sets (wger-gym#18): all metrics of one set on one log."""

# Standard Library
from decimal import Decimal

# Django
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddField
from django.db.models import PROTECT
from django.test import SimpleTestCase
from django.urls import reverse

# Third Party
from rest_framework import status

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.core.tests.powersync_base_test import PowerSyncBaseTestCase
from wger.manager.models import WorkoutLog


MIGRATION = ('manager', '0032_workoutlog_cardio_station')


NEW_KEYS = ('duration', 'distance', 'distance_unit', 'level', 'max_speed', 'max_speed_unit')

# Synthetic values. Time-primary sled/rower station: 600 s in repetitions, a kg
# load in weight, and every other metric in its own field.
ROWER = {
    'exercise': 1,
    'routine': 1,
    'repetitions': '600.00',
    'repetitions_unit': 3,
    'repetitions_target': '600.00',
    'distance': '2.100',
    'distance_unit': 6,
    'weight': '20.00',
    'weight_unit': 1,
    'max_speed': '14.20',
    'max_speed_unit': 5,
    'average_speed': '12.60',
    'pace': '142.86',
    'incline': '1.50',
    'level': '5.0',
    'calories': '120.00',
}


class CardioLogApiTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user_login('admin')

    def post(self, data):
        return self.client.post(
            reverse('workoutlog-list'), data=data, content_type='application/json'
        )

    def detail(self, pk):
        return reverse('workoutlog-detail', kwargs={'pk': pk})

    def create_rower(self):
        response = self.post(ROWER)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response.json()['id']

    def assert_rower(self, data):
        for key, value in ROWER.items():
            self.assertEqual(data[key], value, key)

    def test_time_primary_set_round_trips_every_metric_on_one_row(self):
        before = WorkoutLog.objects.count()
        pk = self.create_rower()
        self.assertEqual(WorkoutLog.objects.count(), before + 1)
        self.assert_rower(self.client.get(self.detail(pk)).json())
        self.assertIsNone(self.client.get(self.detail(pk)).json()['duration'])

    def test_distance_primary_keeps_time_secondary_and_target_separate(self):
        response = self.post(
            {
                'exercise': 1,
                'repetitions': '0.50',
                'repetitions_unit': 6,
                'repetitions_target': '0.75',
                'duration': '125.40',
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        data = self.client.get(self.detail(response.json()['id'])).json()
        self.assertEqual(
            (data['repetitions'], data['repetitions_target'], data['duration']),
            ('0.50', '0.75', '125.40'),
        )
        self.assertIsNone(data['distance'])

    def test_legacy_speed_in_weight_still_round_trips_and_is_not_duplicated(self):
        """Pre-#18 imports stored max speed as weight + km/h; that stays valid."""
        response = self.post(
            {
                'exercise': 1,
                'repetitions': '600',
                'repetitions_unit': 3,
                'weight': '14.20',
                'weight_unit': 5,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        pk = response.json()['id']
        data = self.client.get(self.detail(pk)).json()
        self.assertEqual(
            (data['weight'], data['weight_unit'], data['max_speed']), ('14.20', 5, None)
        )

        response = self.client.patch(
            self.detail(pk),
            data={'max_speed': '15.00', 'max_speed_unit': 5},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assertIn('max_speed', response.json())

        # A speed unit without a weight value is not a second max speed.
        response = self.client.patch(
            self.detail(pk),
            data={'weight': None, 'max_speed': '15.00', 'max_speed_unit': 6},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_kg_weight_with_explicit_max_speed_stays_kg(self):
        response = self.post(
            {'exercise': 1, 'weight': '20.00', 'max_speed': '12.00', 'max_speed_unit': 5}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        data = self.client.get(self.detail(response.json()['id'])).json()
        self.assertEqual(
            (data['weight'], data['weight_unit'], data['max_speed'], data['max_speed_unit']),
            ('20.00', 1, '12.00', 5),
        )

    def test_zero_is_a_value_and_omitted_is_null(self):
        response = self.post({'exercise': 1, 'duration': '0', 'level': '0', 'calories': '0'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        data = self.client.get(self.detail(response.json()['id'])).json()
        self.assertEqual(
            (data['duration'], data['level'], data['calories']), ('0.00', '0.0', '0.00')
        )
        self.assertIsNone(data['distance'])
        self.assertIsNone(data['distance_unit'])
        self.assertIsNone(data['repetitions'])

    def test_primary_unit_without_primary_value_does_not_block_secondary(self):
        """A TIME/DISTANCE unit alone is not a logged value; repetitions=0 is.

        This is also how 2:05 is stored for a Minutes slot: 2.083… minutes do
        not fit two decimals, so the seconds go to duration, repetitions stays null.
        """
        response = self.post(
            {'exercise': 1, 'repetitions': None, 'repetitions_unit': 4, 'duration': '125.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        response = self.post(
            {
                'exercise': 1,
                'repetitions': None,
                'repetitions_unit': 6,
                'distance': '2.000',
                'distance_unit': 6,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        response = self.post(
            {'exercise': 1, 'repetitions': '0', 'repetitions_unit': 3, 'duration': '600.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assertIn('duration', response.json())

    def test_invalid_input_is_a_400_and_creates_nothing(self):
        cases = {
            'negative level': {'level': '-1'},
            'negative duration': {'duration': '-5'},
            'distance without unit': {'distance': '1.000'},
            'time unit as distance unit': {'distance': '1.000', 'distance_unit': 3},
            'distance unit alone must still be a distance': {'duration': '10', 'distance_unit': 1},
            'time twice': {'repetitions': '600', 'repetitions_unit': 3, 'duration': '600'},
            'minutes primary and duration': {
                'repetitions': '10',
                'repetitions_unit': 4,
                'duration': '600',
            },
            'distance twice': {
                'repetitions': '0.5',
                'repetitions_unit': 6,
                'distance': '0.5',
                'distance_unit': 6,
            },
            'max speed without unit': {'max_speed': '12.00'},
            'kg as max speed unit': {'max_speed': '12.00', 'max_speed_unit': 1},
            'max speed unit alone must still be a speed': {'duration': '10', 'max_speed_unit': 2},
            'negative max speed': {'max_speed': '-1', 'max_speed_unit': 5},
            'max speed too precise': {'max_speed': '12.005', 'max_speed_unit': 5},
            'max speed twice': {
                'weight': '14',
                'weight_unit': 5,
                'max_speed': '14',
                'max_speed_unit': 5,
            },
            'no metric at all': {'repetitions_unit': 3},
            'level too precise': {'level': '5.25'},
            'level too large': {'level': '1000'},
            'duration too precise': {'duration': '125.401'},
            'distance too precise': {'distance': '2.1005', 'distance_unit': 6},
            'minutes cannot hold every second': {'repetitions': '0.333', 'repetitions_unit': 4},
            'unknown distance unit': {'distance': '1.000', 'distance_unit': 999},
        }
        before = WorkoutLog.objects.count()
        for name, fields in cases.items():
            with self.subTest(name):
                response = self.post({'exercise': 1, **fields})
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.content
                )
        self.assertEqual(WorkoutLog.objects.count(), before)

    def test_invalid_update_is_a_400_and_changes_nothing(self):
        pk = self.create_rower()
        response = self.client.patch(
            self.detail(pk), data={'duration': '600'}, content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        for invalid in ({'distance_unit': None}, {'max_speed_unit': None}, {'max_speed_unit': 1}):
            response = self.client.patch(
                self.detail(pk), data=invalid, content_type='application/json'
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.content)
        self.assert_rower(self.client.get(self.detail(pk)).json())

    def test_old_client_patch_and_put_keep_new_metrics(self):
        pk = self.create_rower()
        response = self.client.patch(
            self.detail(pk), data={'weight': '15.00'}, content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        # A pre-#18 client PUTs every field it knows, none of the new ones.
        old_client = {key: value for key, value in ROWER.items() if key not in NEW_KEYS}
        old_client['calories'] = '130.00'
        response = self.client.put(
            self.detail(pk), data=old_client, content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

        data = self.client.get(self.detail(pk)).json()
        self.assertEqual(
            (
                data['distance'],
                data['distance_unit'],
                data['level'],
                data['max_speed'],
                data['max_speed_unit'],
                data['calories'],
                data['weight'],
                data['weight_unit'],
            ),
            ('2.100', 6, '5.0', '14.20', 5, '130.00', '20.00', 1),
        )

    def test_strength_log_is_unchanged_apart_from_null_new_keys(self):
        response = self.post(
            {'exercise': 1, 'routine': 1, 'repetitions': 8, 'weight': '82.50', 'rir': '1.5'}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        data = self.client.get(self.detail(response.json()['id'])).json()
        self.assertEqual(
            (
                data['repetitions'],
                data['repetitions_unit'],
                data['weight'],
                data['weight_unit'],
                data['rir'],
            ),
            ('8.00', 1, '82.50', 1, '1.5'),
        )
        self.assertEqual({key: data[key] for key in NEW_KEYS}, dict.fromkeys(NEW_KEYS))


class CardioLogPowerSyncTestCase(PowerSyncBaseTestCase):
    table = 'manager_workoutlog'
    user_access = 'admin'

    def test_partial_sync_update_keeps_new_metrics_and_rejects_duplicates(self):
        self.authenticate()
        log = WorkoutLog.objects.get(pk='aaaaaaaa-aaaa-aaaa-aaaa-000000000001')
        log.repetitions = None
        log.repetitions_unit_id = 6
        log.duration = Decimal('125.40')
        log.level = Decimal('3.0')
        log.save()

        response = self.push('PATCH', {'id': str(log.pk), 'weight': 15})
        self.assertEqual(response.json(), {'status': 'ok!'})
        log.refresh_from_db()
        self.assertEqual(
            (log.duration, log.level, log.weight),
            (Decimal('125.40'), Decimal('3.0'), Decimal('15')),
        )

        response = self.push(
            'PATCH',
            {'id': str(log.pk), 'repetitions': '0.50', 'distance': '0.5', 'distance_unit': 6},
        )
        self.assertNotEqual(response.json(), {'status': 'ok!'})
        log.refresh_from_db()
        self.assertIsNone(log.repetitions)
        self.assertIsNone(log.distance)


class CardioMigrationTestCase(SimpleTestCase):
    """
    0032 may only add nullable columns without defaults or data steps, so the
    live rows keep every value and the deploy digest of old columns stays equal.
    The test settings skip migrations; this reads the migration graph instead.
    """

    # makemigrations only reads the (isolated test) database's migration history.
    databases = {'default'}

    def test_0032_is_the_only_leaf_and_purely_additive(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.graph.leaf_nodes('manager'), [MIGRATION])
        migration = loader.get_migration(*MIGRATION)
        self.assertIn(('manager', '0031_workoutsessionrecovery'), migration.dependencies)
        added = {}
        for operation in migration.operations:
            self.assertIsInstance(operation, AddField)
            self.assertEqual(operation.model_name, 'workoutlog')
            self.assertTrue(operation.field.null)
            self.assertFalse(operation.field.has_default())
            added[operation.name] = operation.field
        self.assertEqual(set(added), set(NEW_KEYS))
        self.assertEqual(added['distance_unit'].remote_field.on_delete, PROTECT)

    def test_manager_models_have_no_unmigrated_changes(self):
        # Scoped to manager: exercises/gallery carry pre-existing upstream drift.
        call_command('makemigrations', 'manager', '--check', '--dry-run', verbosity=0)
