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
from wger.intervals import client, push
from wger.intervals.planning import PlanError, link, window


class Command(BaseCommand):
    help = (
        'Push planned wger gym days to the Intervals.icu calendar as text-only '
        'WeightTraining events for one date window. Previews by default (GET only, no '
        'writes); --apply --plan-hash H writes that exact preview.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--oldest', required=True, help='first local date, YYYY-MM-DD')
        parser.add_argument('--newest', required=True, help='last local date (inclusive)')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--plan-hash', help='plan_hash printed by the preview')
        parser.add_argument(
            '--overwrite-mirror',
            action='store_true',
            help='replace events edited in Intervals instead of reporting a conflict',
        )
        parser.add_argument(
            '--recreate-missing',
            action='store_true',
            help='re-push events that were deleted in Intervals',
        )

    def handle(self, *args, **options):
        if options['apply'] and not options['plan_hash']:
            raise CommandError('--apply needs the --plan-hash from a preview with the same flags')
        api_key = getattr(settings, 'INTERVALS_API_KEY', '')
        athlete_id = getattr(settings, 'INTERVALS_ATHLETE_ID', '')
        try:
            oldest, newest = window(options['oldest'], options['newest'])
            user = User.objects.get(username=getattr(settings, 'INTERVALS_WGER_USERNAME', ''))
        except User.DoesNotExist:
            raise CommandError('INTERVALS_WGER_USERNAME does not name a wger user') from None
        except PlanError as e:
            raise CommandError(str(e)) from None

        flags = {'overwrite': options['overwrite_mirror'], 'recreate': options['recreate_missing']}
        try:
            _, events = client.fetch_window(
                api_key, athlete_id, oldest, newest, need_write=options['apply']
            )
            common = (athlete_id, oldest, newest, events, settings.SITE_URL)
            if options['apply']:
                result = push.apply(user, api_key, *common, options['plan_hash'], **flags)
            else:
                result = push.preview(user, *common, **flags)
        except (client.IntervalsError, PlanError) as e:
            raise CommandError(str(e)) from None

        def brief(item):
            want = result['desired'].get(item['external_id'])
            extra = {}
            if want:
                # No per-event Intervals URL exists (U1): the day view, flagged not exact.
                day_link = link('planned', None, want['date'])[0]
                extra = {
                    'name': want['name'],
                    'description': want['description'],
                    'intervals_day_link': day_link,
                }
            return {**{k: v for k, v in item.items() if k != 'payload'}, **extra}

        report = {
            'mode': 'apply' if options['apply'] else 'preview (no writes)',
            'window': [str(oldest), str(newest)],
            **{a: [brief(i) for i in result[a]] for a in push.ACTIONS},
            'recreate_skipped': [brief(i) for i in result.get('recreate_skipped', [])],
            'unchanged': len(result['unchanged']),
            'skipped': result['skipped'],
            'unsupported_fields': result['unsupported_fields'],
            'plan_hash': result['plan_hash'],
        }
        if options['apply']:
            report['done'] = result['done']
            report['failed'] = result['failed']
        self.stdout.write(json.dumps(report, indent=2, default=str))
        if options['apply'] and result['failed']:
            raise CommandError(
                f'stopped at {result["failed"]["external_id"]}: {result["failed"]["error"]}. '
                'Completed writes are recorded; preview again to resume.'
            )
