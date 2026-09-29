"""
Import WHOOP workout summaries as dated sessions of the user's WHOOP history routine.

Summary only. WHOOP's public v2 /activity/workout record carries strain, heart rate,
energy and heart-rate zone durations. It has no exercises, sets or linked
Strength Trainer workouts, so nothing here creates a WorkoutLog.

plan() is pure: one raw WHOOP v2 workout, the capture's WHOOP account and a snapshot of
the user's existing sessions in; one deterministic plan out:
  'intervals'  cycling/spin, owned by Intervals.icu, not written here
  'present'    already in the WHOOP routine (same start and end instant)
  'ready'      may be written by apply()
  'held'       with reasons; never written
Dedupe is against the database, not a local ledger: WHOOP instants carry milliseconds, so a
WHOOP-routine session with the same start and end is that workout. A session in another
routine that overlaps in time is flagged as a potential duplicate and holds the plan; it is
never merged or deleted here.

percent_recorded: WHOOP's OpenAPI documents 0-100, yet captures exist holding 0-1
fractions. The caller passes the capture's scale (1 or 100) with its evidence. Without one,
a workout with a percent value is held rather than guessed from the value's size.

Notes use the historical WHOOP note lines. A metric WHOOP reported as null gets no line; a
reported zero is written as 0.
"""

# Standard Library
import datetime
import math
from decimal import Decimal
from zoneinfo import ZoneInfo


SYDNEY = ZoneInfo('Australia/Sydney')
UTC = datetime.timezone.utc
MAX_SESSION = datetime.timedelta(hours=5)
# Same set the historical importer routed to Intervals
INTERVALS_SPORTS = {'cycling', 'bike', 'biking', 'spin', 'mountain-biking', 'road-cycling'}
WORKOUT_KEYS = {
    'id',
    'v1_id',
    'user_id',
    'created_at',
    'updated_at',
    'start',
    'end',
    'timezone_offset',
    'sport_name',
    'sport_id',
    'score_state',
    'score',
}
# key, label, unit, may be negative
SCORE_LINES = (
    ('strain', 'Strain', '', False),
    ('average_heart_rate', 'Average heart rate', 'bpm', False),
    ('max_heart_rate', 'Maximum heart rate', 'bpm', False),
    ('kilojoule', 'Energy', 'kJ', False),
    ('percent_recorded', 'Recorded', '%', False),
    ('distance_meter', 'Distance', 'm', False),
    ('altitude_gain_meter', 'Altitude gain', 'm', False),
    ('altitude_change_meter', 'Altitude change', 'm', True),
)
ZONES = tuple(f'zone_{n}_milli' for n in ('zero', 'one', 'two', 'three', 'four', 'five'))


def _instant(value):
    moment = datetime.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('naive timestamp')
    return moment.astimezone(UTC)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _score(workout, percent_scale, holds):
    """Validate the summary; returns the metric note lines."""
    score = workout.get('score')
    if workout.get('score_state') != 'SCORED' or not isinstance(score, dict):
        holds.append(f'score_state {workout.get("score_state")!r}: no scored summary')
        return []
    extra = set(score) - {key for key, *_ in SCORE_LINES} - {'zone_durations'}
    if extra:
        holds.append(f'unrecognised score fields {sorted(extra)}: summary boundary changed')
    lines = []
    for key, label, unit, signed in SCORE_LINES:
        value = score.get(key)
        if value is None:
            continue
        if not _number(value) or (value < 0 and not signed):
            holds.append(f'{key} {value!r} is not a valid measurement')
            continue
        if key == 'percent_recorded':
            if percent_scale not in (1, 100):
                holds.append('percent_recorded scale for this capture is not declared')
                continue
            if value > percent_scale:
                holds.append(f'percent_recorded {value} exceeds declared scale {percent_scale}')
                continue
            value = format((Decimal(repr(value)) * (100 // percent_scale)).normalize(), 'f')
        lines.append(f'{label}: {value} {unit}'.rstrip())
    zones = score.get('zone_durations')
    if zones is not None:
        if not isinstance(zones, dict) or set(zones) != set(ZONES):
            holds.append(f'zone_durations keys {sorted(zones or ())} are not the WHOOP zones')
            return lines
        for key in ZONES:
            value = zones[key]
            if value is None:
                continue
            if type(value) is not int or value < 0:
                holds.append(f'{key} {value!r} is not a duration in milliseconds')
                continue
            label = 'Zone durations ' + key.removesuffix('_milli').replace('_', ' ')
            lines.append(f'{label}: {value} ms ({value / 60000:g} min)')
    return lines


def _overlaps(start, end, session):
    """Shared time, or the same start. An open session counts from its start instant."""
    other_start, other_end = session['datetime_start'], session['datetime_end']
    if other_start == start:
        return True
    if other_end is None:
        return start < other_start < end
    return max(start, other_start) < min(end, other_end)


def plan(workout, *, account, routine_id, sessions, percent_scale=None):
    """
    account: the WHOOP user_id the capture was authorised for.
    routine_id: the WHOOP history routine.
    sessions: every session of the wger user, as dicts with id, routine_id,
        datetime_start, datetime_end (aware), notes and logs (count).
    percent_scale: 1 or 100, the documented percent_recorded encoding of this capture.
    """
    holds = []
    sport = workout.get('sport_name')
    result = {
        'source_id': workout.get('id'),
        'sport': sport,
        'status': 'held',
        'holds': holds,
        'session': None,
        'notes_current': None,
        'overlaps': [],
        'datetime_start': None,
        'datetime_end': None,
        'notes': None,
        'routine_id': routine_id,
    }
    if workout.get('user_id') != account:
        holds.append(f'WHOOP account {workout.get("user_id")!r} is not the capture account')
    if not isinstance(sport, str) or not sport.strip():
        holds.append('source sport unresolved')
    elif sport.strip().lower() in INTERVALS_SPORTS and not holds:
        result['status'] = 'intervals'
        return result
    extra = set(workout) - WORKOUT_KEYS
    if extra:
        holds.append(f'unrecognised workout fields {sorted(extra)}: summary boundary changed')
    try:
        start, end = _instant(workout['start']), _instant(workout['end'])
    except (KeyError, TypeError, ValueError):
        holds.append('valid aware start and end required')
        _score(workout, percent_scale, holds)
        return result
    if not start <= end <= start + MAX_SESSION:
        holds.append(f'source session {start}..{end} is negative or longer than 5 hours')
    lines = [
        f'WHOOP · {sport}',
        f'Started: {start.astimezone(SYDNEY).isoformat()}',
        f'Duration: {(end - start).total_seconds() / 60:g} min',
        f'Ended: {end.astimezone(SYDNEY).isoformat()}',
        f'Started UTC: {start.isoformat()}',
        f'Ended UTC: {end.isoformat()}',
        *_score(workout, percent_scale, holds),
    ]
    notes = '\n'.join(lines)
    result.update(datetime_start=start.isoformat(), datetime_end=end.isoformat(), notes=notes)

    present = []
    for session in sessions:
        if session['routine_id'] == routine_id and (
            session['datetime_start'],
            session['datetime_end'],
        ) == (start, end):
            present.append(session)
        elif start <= end and _overlaps(start, end, session):
            session_end = session['datetime_end'] or session['datetime_start']
            result['overlaps'].append(
                {
                    'session': str(session['id']),
                    'routine_id': session['routine_id'],
                    'logs': session['logs'],
                    'start_delta_s': (session['datetime_start'] - start).total_seconds(),
                    'end_delta_s': (session_end - end).total_seconds(),
                }
            )
    if len(present) > 1:
        holds.append(f'several WHOOP sessions at this instant: {[str(s["id"]) for s in present]}')
    elif present:
        result.update(
            status='present',
            session=str(present[0]['id']),
            notes_current=present[0]['notes'] == notes,
        )
        return result
    for overlap in result['overlaps']:
        where = 'WHOOP routine' if overlap['routine_id'] == routine_id else 'another routine'
        holds.append(
            f'potential duplicate: session {overlap["session"]} in {where} overlaps; '
            'flagged, not merged'
        )
    if not holds:
        result['status'] = 'ready'
    return result


def reconcile(workouts, *, account, routine_id, sessions, percent_scale=None):
    """
    Plan a whole capture against the database, replacing any local import ledger.
    Returns (plans, orphans): orphans are WHOOP-routine sessions no source workout matches.
    """
    sessions = list(sessions)
    by_id = {}
    for workout in workouts:
        by_id.setdefault(workout.get('id'), []).append(workout)
    plans = []
    for source_id, copies in by_id.items():
        result = plan(
            copies[0],
            account=account,
            routine_id=routine_id,
            sessions=sessions,
            percent_scale=percent_scale,
        )
        if any(copy != copies[0] for copy in copies):
            result['status'] = 'held'
            result['holds'].append(f'source id {source_id} captured with differing content')
        plans.append(result)
    matched = {p['session'] for p in plans if p['session']}
    orphans = [
        str(s['id'])
        for s in sessions
        if s['routine_id'] == routine_id and str(s['id']) not in matched
    ]
    return plans, orphans


class AlreadyPresent(Exception):
    def __init__(self, message, sessions):
        super().__init__(message)
        self.sessions = sessions


def apply(ready, *, user_id, day_id):
    """
    Write one ready plan atomically. Under a user row lock it refuses when any session of
    the user overlaps, so a retry after a write whose outcome was lost finds the session
    (AlreadyPresent.sessions) instead of writing it twice.
    """
    # Django
    from django.contrib.auth.models import User
    from django.db import transaction
    from django.db.models import Q

    # wger
    from wger.manager.models import Day, WorkoutSession

    if ready['status'] != 'ready':
        raise ValueError(f'plan {ready["source_id"]} is {ready["status"]}')
    start = datetime.datetime.fromisoformat(ready['datetime_start'])
    end = datetime.datetime.fromisoformat(ready['datetime_end'])
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        day = Day.objects.select_related('routine').get(pk=day_id)
        if day.routine_id != ready['routine_id']:
            raise ValueError('day is not in the WHOOP routine the plan was made for')
        if day.routine.user_id != user.pk:
            raise ValueError('WHOOP routine belongs to another user')
        clash = WorkoutSession.objects.filter(user=user).filter(
            Q(datetime_start=start)
            | Q(datetime_start__lt=end, datetime_end__gt=start)
            | Q(datetime_start__lt=end, datetime_start__gt=start, datetime_end__isnull=True)
        )
        found = [str(pk) for pk in clash.values_list('pk', flat=True)]
        if found:
            raise AlreadyPresent(f'{ready["source_id"]}: session(s) {found} already there', found)
        session = WorkoutSession(
            user=user,
            routine=day.routine,
            day=day,
            notes=ready['notes'],
            datetime_start=start,
            datetime_end=end,
        )
        session.clean()
        session.save()
        stored = WorkoutSession.objects.get(pk=session.pk)
        if (stored.notes, stored.datetime_start, stored.datetime_end, stored.day_id) != (
            ready['notes'],
            start,
            end,
            day.pk,
        ) or stored.logs.exists():
            raise RuntimeError('session readback differs')
    return str(session.pk)
