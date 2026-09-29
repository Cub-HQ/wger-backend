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

"""Pure wger -> Intervals gym-plan planner (PLAN.md sections 4 and 5.4).

Input: planned routine occurrences, the push ledger (link dicts) and the
already-fetched Intervals events for the window. Output: per-occurrence
actions. No I/O; the ledger model and remote writes come later. Only events
whose external_id carries our prefix are ever proposed for change.
"""

# Standard Library
import hashlib
import json

# wger
from wger.intervals.planning import ECHO_PREFIX, GYM_SPORT, PlanError, window


# The fields we write; the remote payload is compared on exactly these.
PAYLOAD_FIELDS = ('category', 'type', 'start_date_local', 'name', 'description', 'external_id')


def external_id(routine_id, date):
    return f'{ECHO_PREFIX}{routine_id}:{date.isoformat()}'


def payload_hash(payload):
    canonical = {f: payload.get(f) for f in PAYLOAD_FIELDS}
    text = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _prescription(slots, exercise_names):
    lines = []
    for slot in slots:
        by_exercise = {}
        for config in slot.sets:
            by_exercise.setdefault(config.exercise, []).append(config.text_repr)
        for exercise, texts in by_exercise.items():
            line = f'{exercise_names[exercise]}: {"; ".join(t for t in texts if t)}'
            lines.append(f'{line} ({slot.comment})' if slot.comment else line)
    return lines


def desired_events(occurrences, oldest, newest, exercise_names, site_url):
    """Intervals payloads for each training occurrence in the window.

    occurrences: (routine, WorkoutDayData) pairs, e.g. from routine.date_sequence.
    Rest days and fit-in-week placeholders are skipped, as in manager/views/ical.py.
    No duration or load is sent: wger has no honest value for either (U7).
    """
    oldest, newest = window(oldest, newest)
    desired = {}
    for routine, occurrence in occurrences:
        day = occurrence.day
        if day is None or day.is_rest or not oldest <= occurrence.date <= newest:
            continue
        key = external_id(routine.id, occurrence.date)
        if key in desired:
            raise PlanError(f'two training days for routine {routine.id} on {occurrence.date}')
        lines = _prescription(occurrence.slots_display_mode, exercise_names)
        # Best existing exact target (OPEN-QUESTIONS U6); no per-date wger route yet.
        lines += ['', f'Open in wger: {site_url.rstrip("/")}/en/routine/{routine.id}/view']
        desired[key] = {
            'category': 'WORKOUT',
            'type': GYM_SPORT,
            'start_date_local': f'{occurrence.date.isoformat()}T00:00:00',
            'name': day.name or routine.name,
            'description': '\n'.join(lines).strip('\n'),
            'external_id': key,
            'routine_id': routine.id,
            'day_id': day.id,
            'date': occurrence.date,
        }
    return desired


def plan_outbound(athlete_id, oldest, newest, desired, links, remote_events, overwrite=False):
    """Diff desired payloads against the ledger and fetched remote events.

    links: dicts with external_id, intervals_event_id, pushed_hash, date, state.
    Returns {action: [items]} for create, update, adopt, conflict, recreate,
    delete, forget, unchanged, plus counts of remote events we never touch.
    """
    oldest, newest = window(oldest, newest)
    athlete_id = str(athlete_id)

    ours = {}
    foreign = 0
    for event in remote_events:
        if str(event.get('athlete_id')) != athlete_id:
            raise PlanError(
                f'event {event.get("id")} belongs to athlete {event.get("athlete_id")!r}'
            )
        ext = str(event.get('external_id') or '')
        if ext.startswith(ECHO_PREFIX):
            ours.setdefault(ext, []).append(event)
        else:
            foreign += 1

    active = {}
    for link in links:
        if link['state'] != 'active' or not oldest <= link['date'] <= newest:
            continue
        if link['external_id'] in active:
            raise PlanError(f'duplicate ledger row {link["external_id"]}')
        active[link['external_id']] = link

    result = {
        a: []
        for a in (
            'create',
            'update',
            'adopt',
            'conflict',
            'recreate',
            'delete',
            'forget',
            'unchanged',
        )
    }

    def remote_for(key):
        found = ours.get(key, [])
        if len(found) > 1:
            return 'duplicate'
        return found[0] if found else None

    for key in sorted(desired.keys() | active.keys() | ours.keys()):
        want, link, remote = desired.get(key), active.get(key), remote_for(key)
        if remote == 'duplicate':
            result['conflict'].append({'external_id': key, 'reason': 'several remote events'})
            continue
        remote_hash = payload_hash(remote) if remote else None

        if want and not link:
            if remote is None:
                result['create'].append({'external_id': key, 'payload': want})
            elif remote_hash == payload_hash(want):
                # Crash after POST, before the ledger row was saved.
                result['adopt'].append({'external_id': key, 'intervals_event_id': remote['id']})
            else:
                result['conflict'].append({'external_id': key, 'reason': 'unlinked remote differs'})
        elif want and link:
            if remote is None:
                result['recreate'].append({'external_id': key, 'payload': want})
            elif remote_hash != link['pushed_hash'] and not overwrite:
                result['conflict'].append({'external_id': key, 'reason': 'edited in Intervals'})
            elif remote_hash == payload_hash(want):
                result['unchanged'].append({'external_id': key})
            else:
                result['update'].append(
                    {'external_id': key, 'intervals_event_id': remote['id'], 'payload': want}
                )
        elif link:
            if remote is None:
                result['forget'].append({'external_id': key})
            else:
                result['delete'].append({'external_id': key, 'intervals_event_id': remote['id']})
        # else: our prefix but no ledger row and not desired: never touched.

    result['skipped'] = {
        'foreign_events': foreign,
        'unlinked_ours': sum(1 for k in ours if k not in desired and k not in active),
    }
    return result
