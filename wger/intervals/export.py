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

"""Guarded export of one measured, completed gym session."""

# Standard Library
import datetime
import hashlib
import json
import math
from decimal import Decimal
from uuid import UUID

# Django
from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

# wger
from wger.intervals import client
from wger.intervals.models import IntervalsActivityLink
from wger.intervals.outbound import _norm
from wger.intervals.planning import SESSION_ECHO_PREFIX, PlanError
from wger.intervals.push import _config, _run_lock
from wger.manager.consts import (
    REP_UNIT_MAX_REPS,
    REP_UNIT_REPETITIONS,
    REP_UNIT_TILL_FAILURE,
    WEIGHT_UNIT_KG,
    WEIGHT_UNIT_LB,
)
from wger.manager.models import WorkoutSession
from wger.utils.units import AbstractWeight


UNSUPPORTED = [
    'moving_time',
    'icu_training_load',
    'heart rate',
    'RPE/feel',
    'RiR',
    'structured sets',
]
# The measures _set renders; a log with none of them is not a recorded set.
RECORDED = ('repetitions', 'weight', 'duration', 'distance', 'level')
# Repetition units whose value is a count of repetitions.
REP_COUNTS = (REP_UNIT_REPETITIONS, REP_UNIT_TILL_FAILURE, REP_UNIT_MAX_REPS)
# The activity fields this export owns. Exports made before kg_lifted existed were
# hashed over the first six, raw (see _last_sent).
FIELDS = (
    'type',
    'start_date_local',
    'elapsed_time',
    'name',
    'description',
    'external_id',
    'kg_lifted',
)
LEGACY_FIELDS = FIELDS[:6]


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def _number(value):
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else str(value)


def _set(log):
    parts = []
    if log.repetitions is not None:
        unit = (
            'reps'
            if log.repetitions_unit_id == 1
            else (log.repetitions_unit.name if log.repetitions_unit else '')
        )
        parts.append(f'{_number(log.repetitions)} {unit}'.rstrip())
    if log.weight is not None:
        unit = log.weight_unit.name if log.weight_unit else ''
        parts.append(f'× {_number(log.weight)} {unit}'.rstrip())
    if log.duration is not None:
        parts.append(f'{_number(log.duration)} s')
    if log.distance is not None:
        unit = log.distance_unit.name if log.distance_unit else ''
        parts.append(f'{_number(log.distance)} {unit}'.rstrip())
    if log.level is not None:
        parts.append(f'level {_number(log.level)}')
    return ' '.join(parts)


def _volume(recorded):
    """(description lines, kg total or None): recorded weight × reps and what it leaves out."""
    total, skipped = Decimal(0), {}
    for log in recorded:
        if (
            log.weight is None
            or log.repetitions is None
            or log.weight_unit_id not in (WEIGHT_UNIT_KG, WEIGHT_UNIT_LB)
            or log.repetitions_unit_id not in REP_COUNTS
        ):
            name = log.exercise.get_translation().name
            skipped[name] = skipped.get(name, 0) + 1
            continue
        mode = 'lb' if log.weight_unit_id == WEIGHT_UNIT_LB else 'kg'
        total += AbstractWeight(log.weight, mode).kg * log.repetitions
    counted = len(recorded) - sum(skipped.values())
    if not counted:
        return [
            'Weight lifted: unavailable; no set has both a weight in kg or lb and a rep count.'
        ], None
    total = total.quantize(Decimal('0.1'))
    lines = [
        f'Weight lifted: {_number(total)} kg (also in the Intervals Weight Lifted field)'
        f' = recorded weight × reps over {counted} of {len(recorded)} sets.'
        ' Weights as logged per set; dumbbell and per-side loads are not doubled.'
    ]
    if skipped:
        lines.append(
            'Not counted (no weight in kg or lb, or no rep count): '
            + ', '.join(f'{name} ({n} set{"s" if n > 1 else ""})' for name, n in skipped.items())
        )
    return lines, float(total)


def _source(user, session_id):
    try:
        session_id = UUID(str(session_id))
        session = WorkoutSession.objects.select_related('day', 'routine').get(
            pk=session_id, user=user
        )
    except (ValueError, WorkoutSession.DoesNotExist):
        raise PlanError('session does not exist or belongs to another user') from None
    if session.time_unknown:
        raise PlanError('session time_unknown: measured start, end and duration are unavailable')
    if session.datetime_end is None:
        raise PlanError('session is open; datetime_end is unavailable')
    duration = session.datetime_end - session.datetime_start
    if duration <= datetime.timedelta(0) or duration > WorkoutSession.max_duration():
        raise PlanError(
            'session duration must be positive and within WorkoutSession.max_duration()'
        )
    logs = list(
        session.logs.select_related(
            'exercise', 'repetitions_unit', 'weight_unit', 'distance_unit'
        ).order_by('date', 'id')
    )
    if any(log.user_id != user.pk for log in logs):
        raise PlanError('session contains logs belonging to another user')
    recorded = [log for log in logs if any(getattr(log, field) is not None for field in RECORDED)]
    if not recorded:
        raise PlanError('session has no recorded logs')
    zone = user.userprofile.zone_info
    start = session.datetime_start.astimezone(zone)
    end = session.datetime_end.astimezone(zone)
    notes = (session.notes or '').splitlines()
    title = next(
        (
            line[len('Original source title: ') :]
            for line in notes
            if line.startswith('Original source title: ')
        ),
        None,
    )
    name = (
        title
        or (session.day.name if session.day else None)
        or (session.routine.name if session.routine else None)
        or 'Gym session'
    )
    groups = {}
    for log in recorded:
        groups.setdefault(log.exercise_id, [log.exercise.get_translation().name, []])[1].append(
            _set(log)
        )
    volume, kg_lifted = _volume(recorded)
    lines = [f'{len(recorded)} sets recorded in wger.', *volume]
    lines.extend(f'{name}: {", ".join(sets)}' for name, sets in groups.values())
    source = next((line for line in notes if line.startswith('Source: ')), None)
    if source:
        lines.append(source)
    lines.extend(
        [
            '',
            f'Open in wger: {settings.SITE_URL.rstrip("/")}/{settings.LANGUAGE_CODE}'
            f'/routine/session/{session.id}',
        ]
    )
    payload = {
        'type': 'WeightTraining',
        'start_date_local': start.strftime('%Y-%m-%dT%H:%M:%S'),
        'elapsed_time': int(duration.total_seconds()),
        'name': name,
        'description': '\n'.join(lines),
        'external_id': f'{SESSION_ECHO_PREFIX}{session.id}',
        'kg_lifted': kg_lifted,
    }
    return session, logs, payload, start, end, zone


def _batch(rows, owner_field, athlete_id, oldest, newest):
    """Validate the entire fetched batch before applying any selection."""
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('id') in (None, ''):
            raise PlanError('remote row without id')
        remote_id = str(row['id'])
        if remote_id in seen:
            raise PlanError(f'duplicate remote id {remote_id}')
        seen.add(remote_id)
        if str(row.get(owner_field)) != str(athlete_id):
            raise PlanError(f'remote {remote_id} belongs to another athlete')
        try:
            start = datetime.datetime.fromisoformat(row.get('start_date_local'))
        except (ValueError, TypeError):
            raise PlanError(f'remote {remote_id} has invalid start_date_local') from None
        if start.tzinfo is not None or not oldest <= start.date() <= newest:
            raise PlanError(f'remote {remote_id} has non-local or out-of-window start_date_local')


def _owned(row):
    """The owned fields as compared and hashed: text as Intervals stores it, kg to 0.1."""
    kg = row.get('kg_lifted')
    if isinstance(kg, (int, float)) and not isinstance(kg, bool) and math.isfinite(kg):
        kg = float(round(kg, 1))
    elif kg is not None:
        # Never equal to a value this export sends (a float or None), so it is a conflict.
        kg = f'invalid: {kg!r}'
    return {**{field: _norm(row.get(field)) for field in LEGACY_FIELDS}, 'kg_lifted': kg}


def _diff(remote, payload):
    theirs, mine = _owned(remote), _owned(payload)
    return [field for field in FIELDS if theirs[field] != mine[field]]


def _last_sent(remote, ledger):
    """Whether the remote still holds exactly what this ledger last sent."""
    if _hash(_owned(remote)) == ledger.pushed_hash:
        return True
    # Exported before kg_lifted: raw six-field hash. A kg_lifted set by anyone
    # since then is not ours, so that remote is not overwritten.
    return (
        remote.get('kg_lifted') is None
        and _hash({field: remote.get(field) for field in LEGACY_FIELDS}) == ledger.pushed_hash
    )


def _identity(remote, activity_id, athlete_id, external_id):
    if (
        str(remote.get('id')) != str(activity_id)
        or str(remote.get('icu_athlete_id')) != str(athlete_id)
        or remote.get('external_id') != external_id
    ):
        raise client.IntervalsError(
            f'activity {activity_id}: Intervals returned a different id, athlete or external_id'
        )


def _plan(user, session_id, retry_pending, need_write=False):
    api_key, athlete_id = _config(user)
    session, logs, payload, start, end, zone = _source(user, session_id)
    athlete = client.bind_athlete(api_key, athlete_id, need_write=need_write)
    if not athlete.get('timezone') or athlete['timezone'] != str(zone):
        raise PlanError(
            'Intervals athlete timezone is missing or differs from the wger profile timezone'
        )
    # Plans and strength are checked on every local date the session touches;
    # activities from the day before too, since they can run into the session.
    # ponytail: an activity starting 2+ days earlier is not fetched (needs >24 h duration).
    oldest, newest = start.date(), end.date()
    touched = {oldest + datetime.timedelta(days=d) for d in range((newest - oldest).days + 1)}
    earliest = oldest - datetime.timedelta(days=1)
    activities = client.list_activities(api_key, earliest, newest)
    events = client.list_events(api_key, earliest, newest)
    _batch(activities, 'icu_athlete_id', athlete_id, earliest, newest)
    _batch(events, 'athlete_id', athlete_id, earliest, newest)
    ledger = IntervalsActivityLink.objects.filter(
        user=user, external_id=payload['external_id']
    ).first()
    copies = [a for a in activities if a.get('external_id') == payload['external_id']]
    checks = {
        'window': [earliest.isoformat(), newest.isoformat()],
        'overlaps': [],
        'unknown_duration': [],
        'same_day_strength': [],
        'planned_events': [],
        'ledger': 'missing'
        if ledger is None
        else ('pending' if ledger.intervals_activity_id is None else ledger.intervals_activity_id),
    }
    conflicts = []
    if len(copies) > 1:
        conflicts.append('multiple remote activities have this external_id')
    for activity in activities:
        local = datetime.datetime.fromisoformat(activity['start_date_local'])
        seconds = activity.get('elapsed_time')
        if seconds is None:
            seconds = activity.get('moving_time')
        if seconds is not None:
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not 0 <= seconds < float('inf')
            ):
                raise PlanError(f'activity {activity["id"]} has invalid duration')
        if activity.get('external_id') == payload['external_id']:
            continue
        remote_start = local.replace(tzinfo=zone).astimezone(datetime.timezone.utc)
        if seconds is None:
            # No duration, so overlap cannot be ruled out: refuse one that starts before
            # the session ends on a touched date or within a max-length session before it.
            # ponytail: fixed look-back; widen if duration-less day-long activities appear.
            if remote_start < session.datetime_end and (
                local.date() in touched
                or remote_start > session.datetime_start - WorkoutSession.max_duration()
            ):
                checks['unknown_duration'].append(str(activity['id']))
        elif remote_start + datetime.timedelta(seconds=seconds) > session.datetime_start:
            if remote_start < session.datetime_end:
                checks['overlaps'].append(str(activity['id']))
        if activity.get('type') == 'WeightTraining' and local.date() in touched:
            checks['same_day_strength'].append(str(activity['id']))
    for event in events:
        if (
            event.get('category') == 'WORKOUT'
            and datetime.datetime.fromisoformat(event['start_date_local']).date() in touched
        ):
            checks['planned_events'].append(str(event['id']))
    for check, reason in (
        ('overlaps', 'overlapping activity'),
        ('unknown_duration', 'activity without duration may overlap'),
        ('same_day_strength', 'same-day WeightTraining activity'),
        ('planned_events', 'same-day planned WORKOUT event'),
    ):
        checks[check].sort()
        if checks[check]:
            conflicts.append(f'{reason}: {", ".join(checks[check])}')
    remote = copies[0] if len(copies) == 1 else None
    remote_id = str(remote['id']) if remote else None
    action = 'create'
    if ledger and ledger.intervals_activity_id is not None:
        if not remote or remote_id != ledger.intervals_activity_id:
            conflicts.append('deleted or replaced in Intervals; no recreate support')
        elif not _diff(remote, payload):
            # A stale ledger hash (interrupted update) is only re-recorded, never re-sent.
            action = 'unchanged' if ledger.pushed_hash == _hash(_owned(payload)) else 'adopt'
        elif _last_sent(remote, ledger):
            action = 'update'
        else:
            conflicts.append('edited in Intervals since the last export; not overwriting')
    elif remote:
        if _diff(remote, payload):
            conflicts.append('remote activity with this external_id has a different payload')
        else:
            action = 'adopt'
    elif ledger:
        if retry_pending:
            action = 'retry'
        else:
            conflicts.append('earlier write outcome unknown; preview with --retry-pending to retry')
    if conflicts:
        action = 'conflict'
    result = {
        'mode': 'session',
        'session': str(session.id),
        'action': action,
        'payload': payload,
        'checks': checks,
        'conflicts': conflicts,
        'unsupported_fields': UNSUPPORTED,
        'intervals_activity_id': remote_id,
        'link': f'https://intervals.icu/activities/{remote_id}' if remote_id else None,
        'update_fields': _diff(remote, payload) if action == 'update' else [],
        # What the update or adopt was reviewed against; apply re-reads and compares it.
        'destination': _owned(remote) if remote else None,
    }
    result['plan_hash'] = _hash(
        {
            'mode': 'session',
            'session': str(session.id),
            'source_ids': [str(log.id) for log in logs],
            'athlete_id': str(athlete_id),
            'timezone': str(zone),
            'payload': payload,
            'action': action,
            'conflicts': conflicts,
            'checks': checks,
            'activity_ids': sorted(str(a['id']) for a in activities),
            'event_ids': sorted(str(e['id']) for e in events),
            'retry_pending': retry_pending,
            'update_fields': result['update_fields'],
            'destination': [remote_id, result['destination']],
            'ledger': [ledger.intervals_activity_id, ledger.pushed_hash] if ledger else None,
        }
    )
    return result


def preview(user, session_id, retry_pending=False):
    """Fetch and preview one session without writing locally or remotely."""
    return _plan(user, session_id, retry_pending)


def apply(user, session_id, plan_hash, retry_pending=False):
    """Recompute under the shared writer lock, then apply the approved plan."""
    # The pending ledger row must be committed before the POST so an uncertain
    # write is remembered; inside an enclosing transaction it would not be.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise PlanError('apply needs autocommit; do not call it inside a transaction')
    api_key, athlete_id = _config(user)
    with _run_lock(user):
        result = _plan(user, session_id, retry_pending, need_write=True)
        if result['plan_hash'] != plan_hash:
            raise PlanError('Intervals or wger data changed since the preview; preview again')
        result['done'], result['failed'] = [], None
        action, payload = result['action'], result['payload']
        external_id, target = payload['external_id'], result['intervals_activity_id']
        defaults = {'session_id': result['session'], 'pushed_hash': _hash(_owned(payload))}

        def record(remote_id):
            IntervalsActivityLink.objects.update_or_create(
                user=user,
                external_id=external_id,
                defaults={
                    **defaults,
                    'intervals_activity_id': remote_id,
                    'pushed_at': timezone.now(),
                },
            )
            if remote_id:
                result['intervals_activity_id'] = remote_id
                result['link'] = f'https://intervals.icu/activities/{remote_id}'

        def check(remote):
            changed = _diff(remote, payload)
            if changed:
                raise client.IntervalsError(
                    f'activity {remote["id"]} was stored with different {", ".join(changed)}'
                    ' than sent'
                )

        try:
            if action == 'conflict':
                raise client.IntervalsError('; '.join(result['conflicts']))
            if action in ('create', 'retry'):
                record(None)
                remote = client.create_manual_activity(api_key, payload)
                _identity(remote, remote['id'], athlete_id, external_id)
                # Saved even if the readback differs, so it is never posted again.
                record(str(remote['id']))
                check(remote)
            elif action in ('adopt', 'update'):
                current = client.get_activity(api_key, target)
                _identity(current, target, athlete_id, external_id)
                if _owned(current) != result['destination']:
                    raise client.IntervalsError(
                        f'activity {target} changed since the preview; preview again'
                    )
                if action == 'update':
                    # Only the differing fields are sent. The last proven ledger stays
                    # until the readback matches: an update that landed unrecorded
                    # previews as adopt, one that landed partly as a conflict.
                    current = client.update_activity(
                        api_key,
                        target,
                        {field: payload[field] for field in result['update_fields']},
                    )
                    _identity(current, target, athlete_id, external_id)
                check(current)
                record(target)
            result['done'].append({'action': action, 'external_id': external_id})
        except (client.IntervalsError, DatabaseError) as error:
            result['failed'] = {'action': action, 'external_id': external_id, 'error': str(error)}
        return result
