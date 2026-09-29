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

"""Pure Intervals.icu -> wger endurance mirror planner (Cub-HQ/wger-gym#6).

No I/O. Input: already-fetched Intervals activities/events for one athlete
and window, plus existing mirror rows as dicts of STORED_FIELDS. Output:
create/update/unchanged/mark_missing proposals. Values are copied verbatim
in source units; nothing is converted, derived or zero-filled, and nothing
here touches gym sessions or sets.
"""

# Standard Library
import datetime


INTERVALS_URL = 'https://intervals.icu'
ECHO_PREFIX = 'wger-gym:'
GYM_SPORT = 'WeightTraining'

# Mirror columns (PLAN.md section 3). Links are computed, never stored or diffed.
STORED_FIELDS = (
    'kind',
    'intervals_id',
    'sport',
    'name',
    'start_local',
    'local_date',
    'timezone',
    'moving_time_s',
    'elapsed_time_s',
    'distance_m',
    'training_load',
    'power_load',
    'hr_load',
    'pace_load',
    'hr_load_type',
    'pace_load_type',
    'intensity',
    'avg_hr',
    'max_hr',
    'rpe',
    'feel',
    'load_target',
    'time_target',
    'paired_event_id',
    'activity_source',
    'upstream_state',
)

# mirror field -> Intervals field, verbatim copies
ACTIVITY_FIELDS = {
    'sport': 'type',
    'name': 'name',
    'timezone': 'timezone',
    'moving_time_s': 'moving_time',
    'elapsed_time_s': 'elapsed_time',
    'distance_m': 'distance',
    'training_load': 'icu_training_load',
    'power_load': 'power_load',
    'hr_load': 'hr_load',
    'pace_load': 'pace_load',
    'hr_load_type': 'hr_load_type',
    'pace_load_type': 'pace_load_type',
    'intensity': 'icu_intensity',
    'avg_hr': 'average_heartrate',
    'max_hr': 'max_heartrate',
    'rpe': 'icu_rpe',
    'paired_event_id': 'paired_event_id',
    'feel': 'feel',
    'activity_source': 'source',
}
EVENT_FIELDS = {
    'sport': 'type',
    'name': 'name',
    'moving_time_s': 'moving_time',
    'distance_m': 'distance',
    'training_load': 'icu_training_load',
    'intensity': 'icu_intensity',
    'load_target': 'load_target',
    'time_target': 'time_target',
}


class PlanError(ValueError):
    """The fetched batch is untrustworthy; refuse the whole plan."""


def _day(value, label):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise PlanError(f'{label} is not an ISO date: {value!r}') from None


def _entry(raw, kind, owner_field, fields, athlete_id, oldest, newest):
    intervals_id = raw.get('id')
    if intervals_id in (None, ''):
        raise PlanError(f'{kind} row without id')
    intervals_id = str(intervals_id)
    if str(raw.get(owner_field)) != athlete_id:
        raise PlanError(f'{kind} {intervals_id} belongs to athlete {raw.get(owner_field)!r}')

    start = raw.get('start_date_local')
    try:
        parsed = datetime.datetime.fromisoformat(start)
    except (TypeError, ValueError):
        raise PlanError(f'{kind} {intervals_id} has invalid start_date_local {start!r}') from None
    if parsed.tzinfo is not None:
        raise PlanError(f'{kind} {intervals_id} start_date_local is not athlete-local: {start!r}')
    # Athlete-local wall clock: the calendar day is the prefix, no tz conversion.
    local_date = parsed.date()
    if not oldest <= local_date <= newest:
        raise PlanError(f'{kind} {intervals_id} on {local_date} is outside {oldest}..{newest}')

    entry = dict.fromkeys(STORED_FIELDS)
    entry.update({mirror: raw.get(source) for mirror, source in fields.items()})
    entry.update(
        kind=kind,
        intervals_id=intervals_id,
        start_local=start,
        local_date=local_date,
        upstream_state='present',
    )
    return entry


def link(kind, intervals_id, local_date):
    """(url, exact). Completed activities have an exact route. Planned events
    have no per-event route (OPEN-QUESTIONS U1), so they get the calendar day
    range, flagged not exact."""
    if kind == 'completed':
        return f'{INTERVALS_URL}/activities/{intervals_id}', True
    day = local_date.isoformat()
    return f'{INTERVALS_URL}/?s={day}&e={day}', False


def _order(entry):
    return entry['local_date'], entry['kind'], entry['intervals_id']


def plan(athlete_id, oldest, newest, activities, events, existing):
    """Diff one fetched window against the existing mirror rows of that user.

    Raises PlanError on a bad window, duplicate or foreign rows, invalid or
    out-of-window dates, or duplicate existing identities.
    """
    athlete_id = str(athlete_id)
    oldest, newest = _day(oldest, 'oldest'), _day(newest, 'newest')
    if oldest > newest:
        raise PlanError(f'oldest {oldest} is after newest {newest}')

    skipped = {'echo': 0, 'weight_training': 0, 'non_workout': 0}
    fetched, seen = {}, set()
    sources = (
        ('completed', activities, 'icu_athlete_id', ACTIVITY_FIELDS),
        ('planned', events, 'athlete_id', EVENT_FIELDS),
    )
    for kind, rows, owner_field, fields in sources:
        for raw in rows:
            # Validate every row, even ones we then skip: a foreign or broken
            # row means the whole fetch is suspect.
            entry = _entry(raw, kind, owner_field, fields, athlete_id, oldest, newest)
            key = (kind, entry['intervals_id'])
            if key in seen:
                raise PlanError(f'duplicate {kind} id {entry["intervals_id"]}')
            seen.add(key)

            if kind == 'planned' and str(raw.get('external_id') or '').startswith(ECHO_PREFIX):
                skipped['echo'] += 1
            elif kind == 'planned' and raw.get('category') != 'WORKOUT':
                skipped['non_workout'] += 1
            elif entry['sport'] == GYM_SPORT:
                # wger owns gym; device-recorded strength stays in Intervals (U8).
                skipped['weight_training'] += 1
            else:
                fetched[key] = entry

    current, stored = {}, set()
    for row in existing:
        key = (row['kind'], str(row['intervals_id']))
        if key in stored:
            raise PlanError(f'duplicate existing mirror row {key}')
        stored.add(key)
        # A fetched row whose date moved into the window is still the same row.
        if (
            key in fetched
            or oldest <= _day(row['local_date'], f'existing {key} local_date') <= newest
        ):
            current[key] = row

    result = {'create': [], 'update': [], 'unchanged': [], 'mark_missing': [], 'skipped': skipped}
    for key, entry in fetched.items():
        entry['link'], entry['link_exact'] = link(*key, entry['local_date'])
        row = current.get(key)
        if row is None:
            result['create'].append(entry)
            continue
        changed = [f for f in STORED_FIELDS if row.get(f) != entry[f]]
        if changed:
            result['update'].append({'entry': entry, 'changed': changed})
        else:
            result['unchanged'].append(entry)

    for key, row in current.items():
        if key not in fetched and row.get('upstream_state') != 'missing':
            # Absent from a later fetch of the same window: flag, never delete.
            result['mark_missing'].append({'kind': key[0], 'intervals_id': key[1]})

    result['create'].sort(key=_order)
    result['unchanged'].sort(key=_order)
    result['update'].sort(key=lambda u: _order(u['entry']))
    result['mark_missing'].sort(key=lambda m: (m['kind'], m['intervals_id']))
    return result
