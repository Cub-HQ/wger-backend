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
# Ledger states of a deliberate removal (removal.py): the key is never written again.
REMOVAL_STATES = ('removing', 'removed')


def external_id(routine_id, date):
    return f'{ECHO_PREFIX}{routine_id}:{date.isoformat()}'


def _norm(value):
    """Compare text the way Intervals may store it: LF line ends, no trailing blanks."""
    if not isinstance(value, str):
        return value
    return '\n'.join(line.rstrip() for line in value.replace('\r\n', '\n').split('\n')).strip()


def differing_fields(a, b):
    return [f for f in PAYLOAD_FIELDS if _norm(a.get(f)) != _norm(b.get(f))]


def payload_hash(payload):
    canonical = {f: _norm(payload.get(f)) for f in PAYLOAD_FIELDS}
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


def desired_events(occurrences, oldest, newest, exercise_names, wger_url):
    """Intervals payloads for each training occurrence in the window.

    wger_url: site root plus language prefix, e.g. https://gym.example/en-au.

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
        # Exact read-only view of this planned occurrence (OPEN-QUESTIONS U6).
        lines += [
            '',
            f'Open in wger: {wger_url.rstrip("/")}/routine/{routine.id}/view'
            f'?day={day.id}&date={occurrence.date.isoformat()}',
        ]
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
    A link with no intervals_event_id is pending: saved just before its POST, so
    a POST that landed without its ledger save is adopted, never re-posted.
    A `removing`/`removed` link is a deliberate removal (removal.py): its key is
    only listed under `removed`, never created, recreated, adopted or written,
    whatever the flags.
    Returns {action: [items]} for create, update, adopt, conflict, recreate,
    delete, forget, unchanged, removed, plus counts of remote events we never touch.
    """
    oldest, newest = window(oldest, newest)
    athlete_id = str(athlete_id)

    ours = {}
    by_id = {}
    foreign = 0
    for event in remote_events:
        if str(event.get('athlete_id')) != athlete_id:
            raise PlanError(
                f'event {event.get("id")} belongs to athlete {event.get("athlete_id")!r}'
            )
        by_id[str(event.get('id'))] = event
        ext = str(event.get('external_id') or '')
        if ext.startswith(ECHO_PREFIX):
            ours.setdefault(ext, []).append(event)
        else:
            foreign += 1

    active, removed = {}, {}
    for link in links:
        if not oldest <= link['date'] <= newest:
            continue
        if link['state'] in REMOVAL_STATES:
            removed[link['external_id']] = link
            continue
        if link['state'] != 'active':
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
            'removed',
        )
    }

    def remote_for(key):
        found = ours.get(key, [])
        if len(found) > 1:
            return 'duplicate'
        return found[0] if found else None

    for key in sorted(desired.keys() | active.keys() | ours.keys() | removed.keys()):
        want, link, remote = desired.get(key), active.get(key), remote_for(key)
        if key in removed:
            result['removed'].append(
                {
                    'external_id': key,
                    'state': removed[key]['state'],
                    'remote_present': remote is not None,
                }
            )
            continue
        if remote == 'duplicate':
            result['conflict'].append({'external_id': key, 'reason': 'several remote events'})
            continue
        remote_hash = payload_hash(remote) if remote else None
        pending = link and link['intervals_event_id'] is None
        moved = (
            remote and link and not pending and str(remote['id']) != str(link['intervals_event_id'])
        )
        # Our ledger event is still there but no longer carries our key (U2):
        # it is not gone, so never recreate (a duplicate) or forget it (an orphan).
        if remote is None and link and not pending and str(link['intervals_event_id']) in by_id:
            result['conflict'].append(
                {'external_id': key, 'reason': 'ledger event lost external_id'}
            )
            continue

        if want and (not link or pending):
            if remote is None:
                result['create'].append({'external_id': key, 'payload': want})
            elif pending or remote_hash == payload_hash(want):
                # Our POST landed but its ledger save did not.
                result['adopt'].append({'external_id': key, 'intervals_event_id': remote['id']})
            else:
                result['conflict'].append({'external_id': key, 'reason': 'unlinked remote differs'})
        elif want and link:
            if remote is None:
                result['recreate'].append({'external_id': key, 'payload': want})
            elif moved:
                result['conflict'].append({'external_id': key, 'reason': 'not the ledger event'})
            elif remote_hash == payload_hash(want):
                if remote_hash != link['pushed_hash']:
                    # Our PUT landed but its ledger save did not: record only.
                    result['adopt'].append({'external_id': key, 'intervals_event_id': remote['id']})
                else:
                    result['unchanged'].append({'external_id': key})
            elif remote_hash != link['pushed_hash'] and not overwrite:
                result['conflict'].append({'external_id': key, 'reason': 'edited in Intervals'})
            else:
                result['update'].append(
                    {
                        'external_id': key,
                        'intervals_event_id': remote['id'],
                        'remote_hash': remote_hash,
                        'payload': want,
                    }
                )
        elif link:
            if remote is None:
                result['forget'].append({'external_id': key})
            elif pending or moved:
                result['conflict'].append({'external_id': key, 'reason': 'not the ledger event'})
            elif remote_hash != link['pushed_hash'] and not overwrite:
                result['conflict'].append({'external_id': key, 'reason': 'edited in Intervals'})
            else:
                result['delete'].append(
                    {
                        'external_id': key,
                        'intervals_event_id': remote['id'],
                        'remote_hash': remote_hash,
                    }
                )
        # else: our prefix but no ledger row and not desired: never touched.

    result['skipped'] = {
        'foreign_events': foreign,
        'unlinked_ours': sum(
            1 for k in ours if k not in desired and k not in active and k not in removed
        ),
    }
    return result
