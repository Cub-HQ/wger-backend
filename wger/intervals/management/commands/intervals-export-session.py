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
from wger.intervals import client, export
from wger.intervals.planning import PlanError


class Command(BaseCommand):
    help = (
        'Preview one measured completed gym session as an Intervals manual activity. '
        'Write only with --apply --plan-hash H; never exports scheduled sessions.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--session', required=True, help='completed WorkoutSession UUID')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--plan-hash', help='plan_hash printed by the preview')
        parser.add_argument(
            '--retry-pending',
            action='store_true',
            help='explicitly retry an uncertain write with no remote copy',
        )

    def handle(self, *args, **options):
        if options['apply'] and not options['plan_hash']:
            raise CommandError('--apply needs the --plan-hash from a preview with the same flags')
        try:
            user = User.objects.get(username=getattr(settings, 'INTERVALS_WGER_USERNAME', ''))
        except User.DoesNotExist:
            raise CommandError('INTERVALS_WGER_USERNAME does not name a wger user') from None
        try:
            flags = {'retry_pending': options['retry_pending']}
            if options['apply']:
                result = export.apply(user, options['session'], options['plan_hash'], **flags)
            else:
                result = export.preview(user, options['session'], **flags)
        except (client.IntervalsError, PlanError) as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if options['apply'] and result['failed']:
            raise CommandError(result['failed']['error'])
