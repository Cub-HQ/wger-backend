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
import json

# Django
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

# wger
from wger.intervals import client, persistence
from wger.intervals.planning import PlanError, window


class Command(BaseCommand):
    help = (
        'Mirror Intervals.icu rides/runs/swims and planned workouts for one date window. '
        'Previews by default (no writes); --apply --plan-hash H writes that exact preview.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--oldest', required=True, help='first local date, YYYY-MM-DD')
        parser.add_argument('--newest', required=True, help='last local date (inclusive)')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--plan-hash', help='plan_hash printed by the preview')

    def handle(self, *args, **options):
        if options['apply'] and not options['plan_hash']:
            raise CommandError('--apply needs the --plan-hash from a preview of the same window')
        athlete_id = getattr(settings, 'INTERVALS_ATHLETE_ID', '')
        username = getattr(settings, 'INTERVALS_WGER_USERNAME', '')
        try:
            oldest, newest = window(options['oldest'], options['newest'])
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError('INTERVALS_WGER_USERNAME does not name a wger user') from None
        except PlanError as e:
            raise CommandError(str(e)) from None

        try:
            activities, events = client.fetch_window(
                getattr(settings, 'INTERVALS_API_KEY', ''), athlete_id, oldest, newest
            )
            args = (user, athlete_id, oldest, newest, activities, events)
            if options['apply']:
                result = persistence.apply(*args, options['plan_hash'])
            else:
                result = persistence.preview(*args)
        except (client.IntervalsError, PlanError) as e:
            raise CommandError(str(e)) from None

        def brief(entry, **extra):
            return {
                'kind': entry['kind'],
                'intervals_id': entry['intervals_id'],
                'local_date': str(entry['local_date']),
                'sport': entry['sport'],
                'name': entry['name'],
                **extra,
            }

        report = {
            'mode': 'apply' if options['apply'] else 'preview (no writes)',
            'window': [str(oldest), str(newest)],
            'create': [brief(e) for e in result['create']],
            'update': [brief(u['entry'], changed=u['changed']) for u in result['update']],
            'mark_missing': result['mark_missing'],
            'unchanged': len(result['unchanged']),
            'skipped': result['skipped'],
            'plan_hash': result['plan_hash'],
        }
        self.stdout.write(json.dumps(report, indent=2))
