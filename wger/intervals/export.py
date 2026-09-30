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
from wger.manager.models import WorkoutSession


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
    lines = [f'{len(recorded)} sets recorded in wger.']
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


def _diff(remote, payload):
    return [field for field, value in payload.items() if _norm(remote.get(field)) != _norm(value)]


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
        if not remote or remote_id != ledger.intervals_activity_id or _diff(remote, payload):
            conflicts.append('deleted/edited in Intervals; no recreate/update support')
        else:
            action = 'unchanged'
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
    api_key, _ = _config(user)
    with _run_lock(user):
        result = _plan(user, session_id, retry_pending, need_write=True)
        if result['plan_hash'] != plan_hash:
            raise PlanError('Intervals or wger data changed since the preview; preview again')
        result['done'], result['failed'] = [], None
        action, payload = result['action'], result['payload']
        try:
            if action == 'conflict':
                raise client.IntervalsError('; '.join(result['conflicts']))
            if action in ('create', 'retry', 'adopt'):
                defaults = {
                    'session_id': result['session'],
                    'pushed_hash': _hash(payload),
                    'pushed_at': timezone.now(),
                }
                if action in ('create', 'retry'):
                    IntervalsActivityLink.objects.update_or_create(
                        user=user,
                        external_id=payload['external_id'],
                        defaults={**defaults, 'intervals_activity_id': None},
                    )
                    remote = client.create_manual_activity(api_key, payload)
                else:
                    remote = client.get_activity(api_key, result['intervals_activity_id'])
                remote_id = str(remote['id'])
                IntervalsActivityLink.objects.update_or_create(
                    user=user,
                    external_id=payload['external_id'],
                    defaults={**defaults, 'intervals_activity_id': remote_id},
                )
                result['intervals_activity_id'] = remote_id
                result['link'] = f'https://intervals.icu/activities/{remote_id}'
                changed = _diff(remote, payload)
                if changed:
                    fields = ', '.join(changed)
                    raise client.IntervalsError(
                        f'activity {remote_id} was stored with different {fields} than sent'
                    )
            result['done'].append({'action': action, 'external_id': payload['external_id']})
        except (client.IntervalsError, DatabaseError) as error:
            result['failed'] = {
                'action': action,
                'external_id': payload['external_id'],
                'error': str(error),
            }
        return result
