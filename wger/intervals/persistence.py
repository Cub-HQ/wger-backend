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

"""Preview or apply one planned Intervals window for a user's EnduranceEntry
rows. Only EnduranceEntry is touched; gym sessions and sets never are."""

# Standard Library
import hashlib
import json

# Django
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

# wger
from wger.intervals.models import EnduranceEntry
from wger.intervals.planning import STORED_FIELDS, PlanError, plan


def _plan(rows, athlete_id, oldest, newest, activities, events):
    existing = [{f: getattr(row, f) for f in STORED_FIELDS} for row in rows]
    result = plan(athlete_id, oldest, newest, activities, events, existing)
    actions = {k: result[k] for k in ('create', 'update', 'mark_missing')}
    canonical = json.dumps(actions, sort_keys=True, default=str, separators=(',', ':'))
    result['plan_hash'] = hashlib.sha256(canonical.encode()).hexdigest()
    return result


def preview(user, athlete_id, oldest, newest, activities, events):
    """The plan plus its `plan_hash`. Makes no writes."""
    rows = EnduranceEntry.objects.filter(user=user)
    return _plan(rows, athlete_id, oldest, newest, activities, events)


def apply(user, athlete_id, oldest, newest, activities, events, plan_hash):
    """Recompute the plan under the owner's lock and write it only if it still
    hashes to `plan_hash`, in one transaction. Any PlanError writes nothing."""
    with transaction.atomic():
        # Locking the owner also serialises a first sync that has no rows yet.
        User.objects.select_for_update().get(pk=user.pk)
        rows = {(r.kind, r.intervals_id): r for r in EnduranceEntry.objects.filter(user=user)}
        result = _plan(rows.values(), athlete_id, oldest, newest, activities, events)
        if result['plan_hash'] != plan_hash:
            raise PlanError('Intervals or wger data changed since the preview; preview again')

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
