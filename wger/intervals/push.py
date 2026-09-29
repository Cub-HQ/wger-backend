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

"""Preview or apply the wger -> Intervals gym-plan push (PLAN.md 5.4-5.6).

Reads routines, never writes gym sessions or logs. Remote writes go one at a
time; each is read back and its ledger row saved before the next, and the
first failure stops the run so a rerun resumes through the adopt rule.
"""

# Standard Library
import hashlib
import json

# Django
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

# wger
from wger.exercises.models import Exercise
from wger.intervals import client
from wger.intervals.models import IntervalsEventLink
from wger.intervals.outbound import PAYLOAD_FIELDS, desired_events, payload_hash, plan_outbound
from wger.intervals.planning import PlanError, window
from wger.manager.models import Routine


ACTIONS = ('create', 'update', 'adopt', 'recreate', 'delete', 'forget', 'conflict')
UNSUPPORTED = ['moving_time', 'icu_training_load', 'structured strength steps']
LINK_FIELDS = ('external_id', 'intervals_event_id', 'pushed_hash', 'date', 'state')


def gym_occurrences(user, oldest, newest):
    """(routine, WorkoutDayData) for the user's non-template routines in the window."""
    routines = Routine.objects.filter(
        user=user, is_template=False, start__lte=newest, end__gte=oldest
    ).order_by('pk')
    return [(r, o) for r in routines for o in r.date_sequence if oldest <= o.date <= newest]


def _exercise_names(occurrences):
    ids = {c.exercise for _, o in occurrences for s in o.slots_display_mode for c in s.sets}
    return {e.id: e.get_translation().name for e in Exercise.objects.filter(id__in=ids)}


def _plan(user, athlete_id, oldest, newest, events, site_url, overwrite, recreate):
    occurrences = gym_occurrences(user, oldest, newest)
    desired = desired_events(occurrences, oldest, newest, _exercise_names(occurrences), site_url)
    links = IntervalsEventLink.objects.filter(user=user).values(*LINK_FIELDS)
    result = plan_outbound(athlete_id, oldest, newest, desired, list(links), events, overwrite)
    if not recreate:
        # Deleted in Intervals by the user: shown, only re-pushed on request (U9).
        result['recreate_skipped'] = result.pop('recreate')
        result['recreate'] = []
    canonical = json.dumps(
        {'actions': {a: result[a] for a in ACTIONS}, 'overwrite': overwrite, 'recreate': recreate},
        sort_keys=True,
        default=str,
        separators=(',', ':'),
    )
    result['plan_hash'] = hashlib.sha256(canonical.encode()).hexdigest()
    result['desired'] = desired
    result['unsupported_fields'] = UNSUPPORTED
    return result


def preview(user, athlete_id, oldest, newest, events, site_url, overwrite=False, recreate=False):
    """The push plan plus its plan_hash. No DB or remote writes."""
    oldest, newest = window(oldest, newest)
    return _plan(user, athlete_id, oldest, newest, events, site_url, overwrite, recreate)


def _remote(want):
    return {f: want[f] for f in PAYLOAD_FIELDS}


def _save(user, want, event):
    """Record what Intervals stored, immediately after the write succeeded.

    Saved even if Intervals dropped our external_id: the row then shows as
    `recreate` (gated by --recreate-missing), so a rerun never silently POSTs
    a duplicate. The mismatch still stops the run for review (U2).
    """
    IntervalsEventLink.objects.update_or_create(
        user=user,
        external_id=want['external_id'],
        defaults={
            'routine_id': want['routine_id'],
            'day_id': want['day_id'],
            'date': want['date'],
            'intervals_event_id': event['id'],
            'pushed_hash': payload_hash(event),
            'state': 'active',
            'pushed_at': timezone.now(),
        },
    )
    if event.get('external_id') != want['external_id']:
        raise client.IntervalsError(
            f'event {event["id"]} was written but did not keep external_id '
            f'{want["external_id"]} (U2); review it in Intervals before rerunning'
        )


def apply(user, api_key, athlete_id, oldest, newest, events, site_url, plan_hash, **flags):
    """Recompute under the owner lock; refuse unless it hashes to plan_hash.

    Returns the plan with `done` (external_ids written) and `failed` (None or
    {external_id, action, error}). Ledger rows for completed writes are kept
    even when a later write fails.
    """
    oldest, newest = window(oldest, newest)
    overwrite, recreate = flags.get('overwrite', False), flags.get('recreate', False)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        result = _plan(user, athlete_id, oldest, newest, events, site_url, overwrite, recreate)
        if result['plan_hash'] != plan_hash:
            raise PlanError('Intervals or wger data changed since the preview; preview again')

        desired, links = result['desired'], IntervalsEventLink.objects.filter(user=user)
        result['done'], result['failed'] = [], None
        steps = [(a, item) for a in ACTIONS if a != 'conflict' for item in result[a]]
        for action, item in steps:
            key = item['external_id']
            try:
                if action in ('create', 'recreate'):
                    _save(user, desired[key], client.create_event(api_key, _remote(desired[key])))
                elif action == 'update':
                    event = client.update_event(
                        api_key, item['intervals_event_id'], _remote(desired[key])
                    )
                    _save(user, desired[key], event)
                elif action == 'adopt':
                    event = {'id': item['intervals_event_id'], **_remote(desired[key])}
                    _save(user, desired[key], event)
                elif action == 'delete':
                    client.delete_event(api_key, item['intervals_event_id'], key)
                    links.filter(external_id=key).update(state='deleted', pushed_at=timezone.now())
                elif action == 'forget':
                    links.filter(external_id=key).update(state='deleted')
            except client.IntervalsError as e:
                result['failed'] = {'external_id': key, 'action': action, 'error': str(e)}
                break
            result['done'].append({'external_id': key, 'action': action})
    return result
