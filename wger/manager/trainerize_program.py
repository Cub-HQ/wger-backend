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
# along with Workout Manager.  If not, see <http://www.gnu.org/licenses/>.

"""
Import one planned Trainerize programme phase as a new wger routine.

normalize() is pure: a saved Trainerize programme snapshot plus reviewed inputs in,
routine spreadsheet rows (see spreadsheet.COLUMNS) or precise errors out. Nothing is
guessed, shortened or dropped: an exercise without a validated mapping, text over a
column limit or a structure wger can't hold is an error naming the source workout and
exercise. An over-long day name or description may be replaced by reviewed text.

preview(), apply() and reconcile() run those rows through spreadsheet.build_plan and
spreadsheet.apply_plan, the validation and single transaction behind the routine
import-preview/import-confirm endpoints. Plans only: workout sessions and logs are
never written here, completed workouts belong to trainerize_history.

Source snapshot:
    {'accountID': int, 'phases': [{'id', 'startDate', 'endDate', 'workouts': [
        {'id', 'name', 'instructions', 'type', 'rounds', 'status', 'exercises': [
            {'id', 'name', 'sets', 'restTime', 'target', 'note', 'superSetID',
             'supersetType', 'recordType', 'side', 'stats',
             'targetDetail': {'type', 'time', 'distance', 'text'}}
            # or the same exercise fields under 'def', as in a dailyWorkout
        ]}]}]}

Inputs: account_id, phase_id, start and end (the reviewed phase window, ISO dates),
routine_name, mapping {source exercise id (str): wger exercise id} (the history
importer's format), reviewed {source workout id: {'name': ..., 'description': ...}}
and today (defaults to the local date).

Live use, inside the web container with no credentials: a driver run through
    docker exec -i <web> python3 manage.py shell < driver.py
calls preview(), shows it for review, then apply() with the reviewed plan_hash.
"""

# Standard Library
import datetime
import re
from decimal import Decimal

# Django
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

# wger
from wger.manager import spreadsheet
from wger.manager.models import (
    Day,
    Routine,
    SlotEntry,
)


TIMED, TEXT = 2, 10
"""Trainerize targetDetail.type values"""

TEXT_TARGET = re.compile(r'(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*([a-z]*)\s*(.*)', re.I)
UNIT_WORDS = {
    **dict.fromkeys(('', 'rep', 'reps'), 'Repetitions'),
    **dict.fromkeys(('s', 'sec', 'secs', 'second', 'seconds'), 'Seconds'),
    **dict.fromkeys(('min', 'mins', 'minute', 'minutes'), 'Minutes'),
}
LIMITS = {
    'routine_name': Routine._meta.get_field('name').max_length,
    'day_name': Day._meta.get_field('name').max_length,
    'day_description': Day._meta.get_field('description').max_length,
    'notes': SlotEntry._meta.get_field('comment').max_length,
}
METRICS = ('reps', 'weight', 'time', 'distance', 'calories', 'level', 'speed')
UNCOMPARED = ('routine_id', 'day_id', 'slot_id', 'entry_id', 'exercise_uuid', 'exercise_name')
COMPARED = [c for c in spreadsheet.COLUMNS if c not in UNCOMPARED]


class Refused(Exception):
    """Nothing was written; `errors` says why"""

    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


def _num(value) -> str:
    return format(Decimal(str(value)).normalize(), 'f')


def _target(ex):
    """(reps, max_reps, rep unit, target text to keep as a note); ValueError if unsupported"""
    detail = ex.get('targetDetail') or {}
    kind = detail.get('type')
    text = (detail.get('text') or ex.get('target') or '').strip()
    if detail.get('distance') is not None:
        raise ValueError('distance targets are not supported')
    if kind == TIMED:
        if detail.get('time') is None:
            raise ValueError('timed target without a time')
        return _num(detail['time']), '', 'Seconds', ''
    if kind not in (TEXT, None):
        raise ValueError(f'target type {kind} is not supported')
    if not text:
        return '', '', 'Repetitions', ''
    match = TEXT_TARGET.fullmatch(text)
    unit = match and UNIT_WORDS.get(match[3].lower())
    if not unit:
        raise ValueError(f'target {text!r} is not a number of reps, seconds or minutes')
    return _num(match[1]), match[2] and _num(match[2]), unit, text if match[4] else ''


def normalize(
    source,
    *,
    account_id,
    phase_id,
    start,
    end,
    routine_name,
    mapping,
    reviewed=None,
    today=None,
):
    """
    Returns {'ok', 'errors', 'rows', 'reviewed'}: rows only when ok, each with
    'source': {'workout', 'exercise'}; 'reviewed' lists every replaced source text.
    """
    reviewed = {str(k): v for k, v in (reviewed or {}).items()}
    today = today or timezone.localdate()
    errors, rows, replaced = [], [], []

    def err(field, message, workout=None, exercise=None):
        errors.append(
            {
                'source': {'workout': workout, 'exercise': exercise},
                'field': field,
                'message': message,
            }
        )

    def result():
        ok = not errors
        return {'ok': ok, 'errors': errors, 'rows': rows if ok else [], 'reviewed': replaced}

    if source.get('accountID') != account_id:
        err('accountID', f'Snapshot is of account {source.get("accountID")}, not {account_id}.')
    phase = next((p for p in source.get('phases') or [] if p.get('id') == phase_id), None)
    if phase is None:
        err('phase', f'Phase {phase_id} is not in the snapshot.')
        return result()
    if (phase.get('startDate'), phase.get('endDate')) != (start, end):
        err(
            'phase',
            f'Phase {phase_id} runs {phase.get("startDate")} to {phase.get("endDate")}, '
            f'not the reviewed {start} to {end}.',
        )
    elif datetime.date.fromisoformat(end) < today:
        err('phase', f'Phase {phase_id} ended on {end}.')

    workouts = phase.get('workouts') or []
    for key in sorted(set(reviewed) - {str(w.get('id')) for w in workouts}):
        err('reviewed', f'Workout {key} is not in phase {phase_id}.', workout=key)

    routine = {
        'routine_name': routine_name,
        'routine_description': f'Trainerize account {account_id}, phase {phase_id}',
        'start': start,
        'end': end,
    }
    if len(routine_name) > LIMITS['routine_name']:
        err('routine_name', f'{len(routine_name)} characters, at most {LIMITS["routine_name"]}.')
    for day_order, workout in enumerate(workouts, start=1):
        wid = workout.get('id')

        def werr(field, message, exercise=None):
            err(field, message, workout=wid, exercise=exercise)

        if workout.get('status') == 'tracked' or any(
            any(s.get(k) is not None for k in METRICS)
            for item in workout.get('exercises') or []
            for s in item.get('stats') or []
        ):
            werr('status', 'A completed workout is history, import it with trainerize_history.')
            continue
        if workout.get('type', 'workoutRegular') != 'workoutRegular':
            werr('type', f'Workout type {workout["type"]!r} is not supported.')
        if workout.get('rounds', 1) != 1:
            werr('rounds', f'{workout["rounds"]} rounds of the whole workout are not supported.')

        day = {
            'day_order': str(day_order),
            'day_name': workout.get('name') or '',
            'day_description': workout.get('instructions') or '',
            'day_is_rest': 'no',
        }
        for column, key in (('day_name', 'name'), ('day_description', 'description')):
            if key in reviewed.get(str(wid), {}):
                replaced.append({'workout': wid, 'field': column, 'source': day[column]})
                day[column] = reviewed[str(wid)][key]

        slots, closed = [], set()
        for item in workout.get('exercises') or []:
            ex = item.get('def', item)
            eid = ex.get('id')
            label = f'{ex.get("name")} [Trainerize {eid}]'
            group = ex.get('superSetID') or None
            if ex.get('supersetType') == 'circuit':
                werr('supersetType', f'{label}: circuits are not supported.', eid)
            if ex.get('recordType') == 'rest':
                werr('recordType', f'{label}: rest items are not supported.', eid)
            if ex.get('side'):
                werr('side', f'{label}: side-specific ({ex["side"]}) items are not supported.', eid)
            wger_id = mapping.get(str(eid))
            if not wger_id:
                werr('exercise', f'{label}: no validated wger exercise mapping.', eid)
            try:
                reps, max_reps, unit, target_note = _target(ex)
            except ValueError as e:
                werr('target', f'{label}: {e}.', eid)
                continue

            if ex.get('supersetType') == 'superset' and group is None:
                werr('superSetID', f'{label}: superset without a superSetID.', eid)
            # One slot per exercise, consecutive exercises of one superset share a slot
            if group is None or not slots or slots[-1][0] != group:
                if group is not None and group in closed:
                    werr('superSetID', f'{label}: superset {group} is not consecutive.', eid)
                if slots:
                    closed.add(slots[-1][0])
                slots.append((group, []))
            note = (item.get('note') or '').strip()
            slots[-1][1].append(
                {
                    'source': {'workout': wid, 'exercise': eid},
                    'exercise_id': str(wger_id or ''),
                    'set_type': 'normal',
                    'sets': str(ex['sets']) if ex.get('sets') is not None else '',
                    'reps': reps,
                    'max_reps': max_reps or '',
                    'rep_unit': unit,
                    'weight_unit': 'kg',
                    'rest': _num(ex['restTime']) if ex.get('restTime') is not None else '',
                    'notes': '; '.join(filter(None, (target_note, note))),
                }
            )

        day_rows = [
            {**day, 'slot_order': str(s), 'entry_order': str(e), **entry}
            for s, (_, entries) in enumerate(slots, start=1)
            for e, entry in enumerate(entries, start=1)
        ] or [{**day, 'source': {'workout': wid, 'exercise': None}}]
        for column in ('day_name', 'day_description'):
            if len(day[column]) > LIMITS[column]:
                werr(
                    column,
                    f'{len(day[column])} characters, at most {LIMITS[column]}; not truncated. '
                    'Supply reviewed text for it.',
                )
        for row in day_rows:
            if len(row.get('notes') or '') > LIMITS['notes']:
                err(
                    'notes',
                    f'{len(row["notes"])} characters, at most {LIMITS["notes"]}; not truncated.',
                    **row['source'],
                )
        rows.extend({**routine, **row} for row in day_rows)
    return result()


def _upload(rows):
    grid = [[row.get(column) for column in spreadsheet.COLUMNS] for row in rows]
    return SimpleUploadedFile('trainerize-program.csv', spreadsheet.write_csv(grid))


def _build(rows, user):
    """spreadsheet.build_plan of the rows, each error traced back to its source"""
    plan = spreadsheet.build_plan(_upload(rows), user, 'create')
    for error in plan.errors:
        error['source'] = rows[error['row'] - 2]['source'] if error['row'] else None
    return plan


def preview(source, user, **inputs):
    """The import-preview JSON of the normalized rows plus replaced texts. Writes nothing."""
    result = normalize(source, **inputs)
    if not result['ok']:
        return {'ok': False, 'plan_hash': None, 'errors': result['errors'], 'reviewed': []}
    return {**_build(result['rows'], user).preview(), 'reviewed': result['reviewed']}


def _cell(value) -> str:
    if value is None:
        return ''
    return _num(value) if isinstance(value, Decimal) else str(value)


def _differences(routine, rows):
    """Every planned cell the routine's export doesn't hold"""
    live = [dict(zip(spreadsheet.COLUMNS, row)) for row in spreadsheet.export_rows(routine)]
    out = [
        {
            'row': number,
            'column': column,
            'expected': _cell(want.get(column)),
            'actual': _cell(have[column]),
        }
        for number, (want, have) in enumerate(zip(rows, live), start=2)
        for column in COMPARED
        if _cell(want.get(column)) != _cell(have[column])
    ]
    if len(live) != len(rows):
        out.append({'row': None, 'column': 'rows', 'expected': len(rows), 'actual': len(live)})
    return out


def apply(source, user, plan_hash, **inputs) -> Routine:
    """
    Create the previewed routine in one transaction under the owner lock.

    Raises Refused, with nothing written, if the source, mappings or the user's
    routines changed since the preview or the routine doesn't read back as planned.
    """
    result = normalize(source, **inputs)
    if not result['ok']:
        raise Refused(result['errors'])
    with transaction.atomic():
        get_user_model()._default_manager.select_for_update().get(pk=user.pk)
        plan = _build(result['rows'], user)
        if not plan.ok:
            raise Refused(plan.errors)
        if plan.hash != plan_hash:
            raise Refused([{'message': 'The source or data changed since the preview.'}])
        routine = spreadsheet.apply_plan(plan, user)
        differences = _differences(routine, result['rows'])
        if differences:
            raise Refused(differences)
    return routine


def reconcile(source, user, **inputs):
    """Whether the user's routine of this name holds exactly the normalized plan"""
    result = normalize(source, **inputs)
    if not result['ok']:
        return {'status': 'invalid', 'errors': result['errors']}
    routine = Routine.objects.filter(user=user, name__iexact=inputs['routine_name']).first()
    if routine is None:
        return {'status': 'absent'}
    differences = _differences(routine, result['rows'])
    return {
        'status': 'differs' if differences else 'match',
        'routine': routine.pk,
        'differences': differences,
    }
