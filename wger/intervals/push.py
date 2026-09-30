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

Reads routines, never writes gym sessions or logs. Both entrypoints bind the
wger user and API key to the configured athlete themselves. Remote writes go
one at a time with no transaction open: each target is re-read right before a
PUT/DELETE, each write is read back and compared on every payload field, and
its ledger row is committed before the next write. The first failure stops the
run; a rerun reconciles through the planner's adopt rule, never a second POST.
"""

# Standard Library
import hashlib
import json
from contextlib import contextmanager

# Django
from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

# wger
from wger.exercises.models import Exercise
from wger.intervals import client
from wger.intervals.models import IntervalsEventLink
from wger.intervals.outbound import (
    PAYLOAD_FIELDS,
    desired_events,
    differing_fields,
    payload_hash,
    plan_outbound,
)
from wger.intervals.planning import PlanError, window
from wger.manager.models import Routine


ACTIONS = ('create', 'update', 'adopt', 'recreate', 'delete', 'forget', 'conflict')
UNSUPPORTED = ['moving_time', 'icu_training_load', 'structured strength steps']
LINK_FIELDS = ('external_id', 'intervals_event_id', 'pushed_hash', 'date', 'state')
RUN_LOCK = 0x1C5  # pg advisory lock namespace for intervals-push-gym


def gym_occurrences(user, oldest, newest):
    """(routine, WorkoutDayData) for the user's non-template routines in the window."""
    routines = Routine.objects.filter(
        user=user,
        is_template=False,
        deleted_at__isnull=True,
        start__lte=newest,
        end__gte=oldest,
    ).order_by('pk')
    return [(r, o) for r in routines for o in r.date_sequence if oldest <= o.date <= newest]


def _exercise_names(occurrences):
    ids = {c.exercise for _, o in occurrences for s in o.slots_display_mode for c in s.sets}
    return {e.id: e.get_translation().name for e in Exercise.objects.filter(id__in=ids)}


def _config(user):
    """(api_key, athlete_id) if `user` is the wger user paired with the athlete."""
    username = getattr(settings, 'INTERVALS_WGER_USERNAME', '')
    if not username or user.username != username:
        raise PlanError('this wger user is not the one paired with INTERVALS_ATHLETE_ID')
    return getattr(settings, 'INTERVALS_API_KEY', ''), getattr(settings, 'INTERVALS_ATHLETE_ID', '')


def _plan(user, athlete_id, oldest, newest, events, overwrite, recreate):
    occurrences = gym_occurrences(user, oldest, newest)
    wger_url = f'{settings.SITE_URL.rstrip("/")}/{settings.LANGUAGE_CODE}'
    desired = desired_events(occurrences, oldest, newest, _exercise_names(occurrences), wger_url)
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


def preview(user, oldest, newest, overwrite=False, recreate=False):
    """The push plan plus its plan_hash. GET only; no DB or remote writes."""
    oldest, newest = window(oldest, newest)
    api_key, athlete_id = _config(user)
    _, events = client.fetch_window(api_key, athlete_id, oldest, newest)
    return _plan(user, athlete_id, oldest, newest, events, overwrite, recreate)


@contextmanager
def _run_lock(user):
    """One apply per user at a time, held across HTTP without keeping a
    transaction (or row lock) open: a Postgres session advisory lock."""
    if connection.vendor != 'postgresql':
        # ponytail: SQLite (dev/CI only) gets no cross-process run lock.
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [RUN_LOCK, user.pk])
        if not cursor.fetchone()[0]:
            raise PlanError('another intervals-push-gym apply is running for this user')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [RUN_LOCK, user.pk])


def _remote(want):
    return {f: want[f] for f in PAYLOAD_FIELDS}


def _unchanged_since_fetch(api_key, item):
    """Re-read the target right before a PUT/DELETE; refuse if it moved on."""
    current = client.get_event(api_key, item['intervals_event_id'])
    if (
        current.get('external_id') != item['external_id']
        or payload_hash(current) != item['remote_hash']
    ):
        raise client.IntervalsError(
            f'event {item["intervals_event_id"]} changed in Intervals during this run; '
            'not written, preview again'
        )


def _pending(user, want):
    """Commit ownership of the key before POSTing, so a POST whose readback or
    ledger save fails is adopted by the next run instead of posted again."""
    IntervalsEventLink.objects.update_or_create(
        user=user,
        external_id=want['external_id'],
        defaults={
            'routine_id': want['routine_id'],
            'day_id': want['day_id'],
            'date': want['date'],
            'intervals_event_id': None,
            'pushed_hash': '',
            'state': 'active',
            'pushed_at': timezone.now(),
        },
    )


def _record(user, want, event):
    """Commit the ledger row for a write that happened, then refuse if Intervals
    stored something other than what was sent.

    pushed_hash is what we sent, so a stored-differently event shows up next
    run as a conflict (no write) rather than an update loop; the row keeps the
    event owned, so a rerun never POSTs it again. The run still stops (U2).
    """
    IntervalsEventLink.objects.update_or_create(
        user=user,
        external_id=want['external_id'],
        defaults={
            'routine_id': want['routine_id'],
            'day_id': want['day_id'],
            'date': want['date'],
            'intervals_event_id': event['id'],
            'pushed_hash': payload_hash(want),
            'state': 'active',
            'pushed_at': timezone.now(),
        },
    )
    changed = differing_fields(event, want)
    if changed:
        raise client.IntervalsError(
            f'event {event["id"]} was stored with different {", ".join(changed)} than sent; '
            'review it in Intervals'
        )


def apply(user, oldest, newest, plan_hash, overwrite=False, recreate=False):
    """Re-fetch and recompute; refuse unless it hashes to plan_hash, then write.

    Returns the plan with `done` and `failed` (None or {external_id, action,
    error}). No transaction wraps the run, so every ledger row committed before
    a failure (remote or DB) survives it.
    """
    oldest, newest = window(oldest, newest)
    api_key, athlete_id = _config(user)
    with _run_lock(user):
        _, events = client.fetch_window(api_key, athlete_id, oldest, newest, need_write=True)
        result = _plan(user, athlete_id, oldest, newest, events, overwrite, recreate)
        if result['plan_hash'] != plan_hash:
            raise PlanError('Intervals or wger data changed since the preview; preview again')

        desired, links = result['desired'], IntervalsEventLink.objects.filter(user=user)
        result['done'], result['failed'] = [], None
        steps = [(a, item) for a in ACTIONS if a != 'conflict' for item in result[a]]
        for action, item in steps:
            key = item['external_id']
            want = desired.get(key)
            try:
                if action in ('create', 'recreate'):
                    _pending(user, want)
                    _record(user, want, client.create_event(api_key, _remote(want)))
                elif action == 'update':
                    _unchanged_since_fetch(api_key, item)
                    event_id = item['intervals_event_id']
                    _record(user, want, client.update_event(api_key, event_id, _remote(want)))
                elif action == 'adopt':
                    _record(user, want, client.get_event(api_key, item['intervals_event_id']))
                elif action == 'delete':
                    _unchanged_since_fetch(api_key, item)
                    client.delete_event(api_key, item['intervals_event_id'])
                    links.filter(external_id=key).update(state='deleted', pushed_at=timezone.now())
                elif action == 'forget':
                    links.filter(external_id=key).update(state='deleted')
            except (client.IntervalsError, DatabaseError) as e:
                # Rows committed for earlier steps stay; a rerun adopts a write
                # whose own row failed to save (remote == desired), never re-POSTs.
                result['failed'] = {'external_id': key, 'action': action, 'error': str(e)}
                break
            result['done'].append({'external_id': key, 'action': action})
    return result
