"""
Import completed Trainerize workouts as dated sessions of an existing history routine.

plan() is pure: one Trainerize dailyWorkout plus validated source-exercise-id → wger
exercise-id mappings in, one deterministic plan out ('ready' or 'held' with reasons).
apply() writes one ready plan as a single transaction and reads every value back.

Live use (inside the web container, under the writer/history custody locks):
    docker exec -i <web> python3 manage.py shell < driver.py
"""

# Standard Library
import datetime
import hashlib
import json
from decimal import Decimal
from zoneinfo import ZoneInfo


SYDNEY = ZoneInfo('Australia/Sydney')
MAX_SESSION = datetime.timedelta(hours=5)
TIME_UNKNOWN = (
    'time not recorded; 06:00 Australia/Sydney is a display anchor, not a measured start; '
    'duration unknown.'
)
TIME_UNRELIABLE = (
    'source start/end times are unreliable; 06:00 Australia/Sydney is a display anchor, '
    'not a measured start; duration unknown.'
)
REPS, SECONDS, KILOMETERS = 1, 3, 6
KG, KMH = 1, 5
METRICS = ('reps', 'weight', 'time', 'distance', 'calories', 'level', 'speed')
# field: decimal places the column stores; anything finer would be rounded
PLACES = {
    'repetitions': 2,
    'weight': 2,
    'distance': 3,
    'calories': 2,
    'level': 1,
    'max_speed': 2,
}


def fingerprint(workout):
    return hashlib.sha256(
        json.dumps(workout, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def _utc(value):
    return datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)


def _log(stats, holds, name, omitted=None, source_id=None):
    """
    One performed set. Primary measure in repetitions, the rest in their own fields.
    omitted: when a list, an ambiguous set (zero-only, or no reps/time/distance) is recorded
    there with its source identity and raw values instead of holding the workout.
    """
    values = {k: stats.get(k) for k in METRICS}
    if any(v is not None and (type(v) not in (int, float) or v < 0) for v in values.values()):
        holds.append(f'{name}: non-numeric or negative value {values}')
        return None
    reps, time, distance = values['reps'], values['time'], values['distance']
    if reps is not None and (time is not None or distance is not None):
        holds.append(f'{name}: reps together with time/distance cannot be stored losslessly')
        return None
    ambiguous = None
    if reps is None and time is None and distance is None:
        ambiguous = 'set without reps, time or distance'
    elif all(v in (None, 0) for v in values.values()):
        ambiguous = 'zero-only set is ambiguous (performed or skipped)'
    if ambiguous:
        if omitted is None:
            holds.append(f'{name}: {ambiguous} {values}')
        else:
            omitted.append(
                {'exercise': source_id, 'name': name, 'raw': stats, 'reason': ambiguous}
            )
        return None
    if time is not None:
        log = {'repetitions': time, 'repetitions_unit': SECONDS, 'distance': distance}
    elif distance is not None:
        log = {'repetitions': distance, 'repetitions_unit': KILOMETERS, 'distance': None}
    else:
        log = {'repetitions': reps, 'repetitions_unit': REPS, 'distance': None}
    log.update(
        distance_unit=KILOMETERS if log['distance'] is not None else None,
        weight=values['weight'],
        weight_unit=KG if values['weight'] is not None else None,
        calories=values['calories'],
        level=values['level'],
        max_speed=values['speed'],
        max_speed_unit=KMH if values['speed'] is not None else None,
    )
    for field, places in PLACES.items():
        if log[field] is not None:
            exact = Decimal(repr(log[field]))
            if exact != exact.quantize(Decimal(1).scaleb(-places)):
                holds.append(f'{name}: {field} {log[field]} would be rounded')
                return None
            log[field] = str(exact.quantize(Decimal(1).scaleb(-places)))
    return log


def plan(
    workout,
    mapping,
    *,
    clarification=None,
    differences=None,
    omit_incomplete=False,
    unreliable_time=False,
):
    """
    mapping: {source exercise id (str): wger exercise id}, validated provenance only.
    clarification: user-confirmed note for a completed workout with no logged sets.
    differences: {source exercise id (str): source-vs-catalog technique difference to keep}.
    omit_incomplete: user-approved policy: import the valid sets and list each zero-only or
    measure-less set in the notes and plan['omitted'] instead of holding the workout.
    A workout left with no usable set stays held.
    unreliable_time: user-approved policy: a source session that is open, negative or longer
    than the maximum is anchored like a date-only workout (duration unknown) instead of held;
    the original source times stay in the notes as provenance, never as the duration.
    """
    differences = differences or {}
    holds, logs, provenance = [], [], []
    omitted = [] if omit_incomplete else None
    iterations = {}
    if workout.get('status') != 'tracked':
        holds.append(f'status {workout.get("status")!r} is not a completed workout')
    for exercise in workout.get('exercises') or []:
        source_id, name = exercise['def']['id'], exercise['def']['name']
        rest = exercise['def'].get('restTime')
        performed = [
            s for s in exercise.get('stats') or [] if any(s.get(k) is not None for k in METRICS)
        ]
        wger_id = mapping.get(str(source_id))
        line = f'{name} [Trainerize {source_id}'
        provenance.append(f'{line} → wger {wger_id}]' if wger_id else f'{line}]')
        if wger_id and differences.get(str(source_id)):
            provenance[-1] += f' (source differs: {differences[str(source_id)]})'
        if not performed:
            continue
        if not wger_id:
            holds.append(f'{line}]: no validated wger exercise mapping')
            continue
        for stats in performed:
            log = _log(stats, holds, name, omitted, source_id)
            if log:
                # Counted per exercise, so a repeated exercise continues its set numbers
                iterations[wger_id] = iterations.get(wger_id, 0) + 1
                logs.append(
                    {
                        'exercise': wger_id,
                        'iteration': iterations[wger_id],
                        'rest_target': rest,
                        **log,
                    }
                )

    start = end = None
    notes_tail = []
    if workout.get('startTime'):
        start = _utc(workout['startTime'])
        end = _utc(workout['endTime']) if workout.get('endTime') else None
        if end is None or not start <= end <= start + MAX_SESSION:
            if unreliable_time:
                notes_tail.append(
                    'Original source times (UTC, unreliable, not the workout duration): '
                    f'start {workout["startTime"]}, end {workout.get("endTime")}'
                )
                start = end = None
            else:
                holds.append(
                    f'source session {start}..{end} is open, negative or longer than 5 hours'
                )
    time_unknown = start is None
    if time_unknown:
        start = end = datetime.datetime.combine(
            datetime.date.fromisoformat(workout['date']), datetime.time(6), SYDNEY
        )
        notes_tail.append(TIME_UNRELIABLE if notes_tail else TIME_UNKNOWN)

    if not logs and not holds:
        if omitted:
            holds.append('no usable sets: every recorded set is zero-only or has no measure')
        elif clarification:
            notes_tail.append(clarification)
        else:
            holds.append('completed workout without logged sets needs user clarification')
    notes = '\n'.join(
        [
            f'Original source title: {workout["name"]}',
            f'Source: Trainerize workout {workout["id"]}',
            '',
            'Original source exercises:',
            *provenance,
            *_omission_lines(omitted),
            *notes_tail,
        ]
    )
    return {
        'source_id': workout['id'],
        'source_date': workout['date'],
        'fingerprint': fingerprint(workout),
        'status': 'held' if holds else 'ready',
        'holds': holds,
        'datetime_start': start.isoformat(),
        'datetime_end': end.isoformat() if end else None,
        # Anchored at the display time: the session's time and duration are unknown
        'time_unknown': time_unknown,
        'notes': notes,
        'logs': logs,
        'omitted': omitted or [],
    }


def _omission_lines(omitted):
    if not omitted:
        return []
    lines = ['', 'Omitted source sets (recorded without a usable value; not imported):']
    for o in omitted:
        raw = ', '.join(f'{k} {o["raw"][k]}' for k in METRICS if o['raw'].get(k) is not None)
        lines.append(
            f'{o["name"]} [Trainerize {o["exercise"]}] set {o["raw"].get("setID")}: '
            f'{o["reason"]}; source values: {raw}'
        )
    return lines


LOG_FIELDS = (
    'exercise_id',
    'iteration',
    'rest_target',
    'repetitions',
    'repetitions_unit_id',
    'distance',
    'distance_unit_id',
    'weight',
    'weight_unit_id',
    'calories',
    'level',
    'max_speed',
    'max_speed_unit_id',
)


def _row(log):
    """Plan log → comparable model field values."""
    out = {}
    for field in LOG_FIELDS:
        value = log[field.removesuffix('_id')]
        out[field] = Decimal(value) if field in PLACES and value is not None else value
    return out


class AlreadyPresent(Exception):
    pass


def apply(ready, *, user_id, routine_id, day_id):
    """Write one ready plan atomically; refuse if a session already occupies its time."""
    # Django
    from django.contrib.auth.models import User
    from django.db import transaction
    from django.db.models import Q

    # wger
    from wger.manager.models import Day, WorkoutLog, WorkoutSession

    if ready['status'] != 'ready':
        raise ValueError(f'plan {ready["source_id"]} is held')
    start = datetime.datetime.fromisoformat(ready['datetime_start'])
    end = datetime.datetime.fromisoformat(ready['datetime_end'])
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        day = Day.objects.select_related('routine').get(pk=day_id, routine_id=routine_id)
        if day.routine.user_id != user.pk:
            raise ValueError('history routine belongs to another user')
        # One history routine per provider: a session there at an overlapping time, or
        # one naming this source workout, means it may already be imported.
        clash = WorkoutSession.objects.filter(user=user, routine_id=routine_id).filter(
            Q(datetime_start=start)
            | Q(datetime_start__lte=end, datetime_end__gte=start)
            | Q(notes__contains=f'Source: Trainerize workout {ready["source_id"]}\n')
        )
        if clash.exists():
            found = [str(s.pk) for s in clash]
            raise AlreadyPresent(f'{ready["source_id"]}: session(s) {found} already present')
        # Unknown time needs WorkoutSession.time_unknown; a model without the column
        # refuses the plan here (TypeError) instead of storing a measured-looking time.
        unknown = {'time_unknown': True} if ready['time_unknown'] else {}
        session = WorkoutSession(
            user=user,
            routine=day.routine,
            day=day,
            notes=ready['notes'],
            datetime_start=start,
            datetime_end=end,
            **unknown,
        )
        session.clean()
        session.save()
        for log in ready['logs']:
            WorkoutLog(
                user=user,
                routine=day.routine,
                session=session,
                date=end,
                **{field: value for field, value in _row(log).items()},
            ).save()

        stored = WorkoutSession.objects.get(pk=session.pk)
        if (
            stored.notes,
            stored.datetime_start,
            stored.datetime_end,
            stored.day_id,
            getattr(stored, 'time_unknown', False),
        ) != (ready['notes'], start, end, day.pk, ready['time_unknown']):
            raise RuntimeError('session readback differs')
        order = ('exercise_id', 'iteration')
        rows = list(WorkoutLog.objects.filter(session=session).order_by(*order).values(*LOG_FIELDS))
        wanted = sorted(
            (_row(log) for log in ready['logs']), key=lambda r: (r['exercise_id'], r['iteration'])
        )
        if rows != wanted:
            raise RuntimeError(f'log readback differs: {rows} != {wanted}')
    return str(session.pk)
