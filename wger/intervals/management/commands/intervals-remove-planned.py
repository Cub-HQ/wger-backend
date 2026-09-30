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
from django.db import DatabaseError

# wger
from wger.intervals import client, removal
from wger.intervals.planning import PlanError


class Command(BaseCommand):
    help = (
        'Deliberately remove ONE planned gym event that intervals-push-gym put in Intervals, '
        'and keep its ledger row as a tombstone so it is never pushed again. Previews by '
        'default (GET only); --apply --removal-hash H [--approve PHRASE] does that exact removal.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--external-id', required=True, help='e.g. wger-gym:7:2040-01-02')
        parser.add_argument('--event-id', required=True, help='the Intervals event id')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--removal-hash', help='removal_hash printed by the preview')
        parser.add_argument(
            '--approve', help='the exact approval_required phrase printed by the preview'
        )

    def handle(self, *args, **options):
        if options['apply'] and not options['removal_hash']:
            raise CommandError('--apply needs the --removal-hash from a preview')
        try:
            user = User.objects.get(username=getattr(settings, 'INTERVALS_WGER_USERNAME', ''))
        except User.DoesNotExist:
            raise CommandError('INTERVALS_WGER_USERNAME does not name a wger user') from None

        ids = options['external_id'], options['event_id']
        try:
            if options['apply']:
                result = removal.apply(user, *ids, options['removal_hash'], options['approve'])
            else:
                result = removal.preview(user, *ids)
        except PlanError as e:
            raise CommandError(f'{e}. Nothing was written.') from None
        except (client.IntervalsError, DatabaseError) as e:
            raise CommandError(
                f'{e}. The ledger row may now be a `removing` tombstone; preview again to resume.'
            ) from None

        mode = 'apply' if options['apply'] else 'preview (no writes)'
        self.stdout.write(json.dumps({'mode': mode, **result}, indent=2, default=str))
