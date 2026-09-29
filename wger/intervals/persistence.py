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

"""Write one planned Intervals window into a user's EnduranceEntry rows.

Only EnduranceEntry is touched; gym sessions and sets never are.
"""

# Django
from django.db import transaction
from django.utils import timezone

# wger
from wger.intervals.models import EnduranceEntry
from wger.intervals.planning import STORED_FIELDS, plan


def sync_window(user, athlete_id, oldest, newest, activities, events):
    """Plan already-fetched rows against the user's mirror and apply it in
    one transaction. Returns the plan. PlanError leaves the DB untouched."""
    with transaction.atomic():
        # Row locks keep a concurrent run from planning on the same snapshot.
        rows = {
            (row.kind, row.intervals_id): row
            for row in EnduranceEntry.objects.select_for_update().filter(user=user)
        }
        existing = [{f: getattr(row, f) for f in STORED_FIELDS} for row in rows.values()]
        result = plan(athlete_id, oldest, newest, activities, events, existing)

        now = timezone.now()
        EnduranceEntry.objects.bulk_create(
            EnduranceEntry(user=user, fetched_at=now, **{f: e[f] for f in STORED_FIELDS})
            for e in result['create']
        )
        for update in result['update']:
            row = rows[(update['entry']['kind'], update['entry']['intervals_id'])]
            for field in update['changed']:
                setattr(row, field, update['entry'][field])
            row.fetched_at = now
            row.save(update_fields=[*update['changed'], 'fetched_at'])
        for missing in result['mark_missing']:
            row = rows[(missing['kind'], missing['intervals_id'])]
            row.upstream_state = 'missing'
            row.save(update_fields=['upstream_state'])
    return result
