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

"""Deliberately remove ONE owned planned gym event from Intervals and keep a
tombstone so no planner path ever writes that key again.

Preview is GET only. Apply repeats every check under the push run lock and
refuses unless the preview's removal_hash still matches. Then, in order: commit
`removing` plus the audit snapshot, re-read the event, DELETE, read back its
absence, commit `removed`. Any failure after the first commit leaves the
`removing` tombstone (never recreated); preview again to resume. Routines,
days and workout history are only read.

Recovery (checked 2026-09-30 in the official API spec and developer forum):
Intervals documents undelete for activities only, and no restore, trash or
undo for calendar events. Re-pushing makes a new event id, which is not
recovery. So a DELETE is irreversible and apply needs the exact per-action
approval phrase printed by the preview.
"""

# Standard Library
import hashlib
import json

# Django
from django.utils import timezone

# wger
from wger.intervals import client
from wger.intervals.models import IntervalsEventLink
from wger.intervals.outbound import payload_hash
from wger.intervals.planning import GYM_SPORT, PlanError
from wger.intervals.push import _config, _run_lock
from wger.manager.models import WorkoutSession


# No documented Intervals undo keeps the original event id (see module docstring).
VERIFIED_RECOVERY = None


def _hash(value):
    text = json.dumps(value, sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _identity(event):
    """What must not move between checks. Not the whole event: Intervals
    recomputes derived fields such as icu_ctl without the event being edited."""
    fields = ('id', 'athlete_id', 'external_id', 'updated', 'paired_activity_id')
    return _hash({'payload': payload_hash(event), **{f: event.get(f) for f in fields}})


def _absent(api_key, event_id, external_id, events):
    """True only if neither the date's event list nor a GET by id shows the event."""
    if any(
        str(e.get('id')) == str(event_id) or e.get('external_id') == external_id for e in events
    ):
        return False
    try:
        client.get_event(api_key, event_id)
    except client.IntervalsError as e:
        # ponytail: client reports the status only in its message; use a typed
        # not-found error once client.py grows one.
        if str(e).endswith('HTTP 404'):
            return True
        raise
    return False


def _check(user, api_key, athlete_id, external_id, event_id, need_write=False):
    """(link, plan) after every refusal rule. GET only."""
    link = IntervalsEventLink.objects.filter(user=user, external_id=external_id).first()
    if link is None:
        raise PlanError(f'no ledger row {external_id} for this user')
    if link.state not in ('active', 'removing'):
        raise PlanError(f'ledger row {external_id} is {link.state}; nothing to remove')
    if link.intervals_event_id is None or str(link.intervals_event_id) != str(event_id):
        raise PlanError(
            f'ledger row {external_id} is for event {link.intervals_event_id}, not {event_id}'
        )

    date = link.date
    activities, events = client.fetch_window(api_key, athlete_id, date, date, need_write)
    if any(str(e.get('athlete_id')) != str(athlete_id) for e in events):
        raise PlanError('Intervals returned an event of another athlete')
    if activities:
        raise PlanError(f'Intervals has a completed activity on {date}; not removing')
    if WorkoutSession.objects.filter(user=user, datetime_start__date=date).exists():
        raise PlanError(f'a wger workout session is logged on {date}; not removing')

    snapshot = (link.removal or {}).get('event')
    if _absent(api_key, event_id, external_id, events):
        action, event = 'tombstone', snapshot
    else:
        action, event = 'delete', client.get_event(api_key, event_id)
        carriers = [e.get('id') for e in events if e.get('external_id') == external_id]
        problems = [
            (str(event.get('athlete_id')) != str(athlete_id), 'belongs to another athlete'),
            (event.get('external_id') != external_id, 'carries a different external_id'),
            ([str(c) for c in carriers] != [str(event_id)], f'is not the only event {carriers}'),
            (event.get('category') != 'WORKOUT', 'is not a WORKOUT'),
            (event.get('type') != GYM_SPORT, f'is not {GYM_SPORT}'),
            (str(event.get('start_date_local'))[:10] != date.isoformat(), f'is not on {date}'),
            (event.get('paired_activity_id') is not None, 'is paired with an activity'),
            (payload_hash(event) != link.pushed_hash, 'was edited in Intervals since the push'),
            (snapshot is not None and _identity(event) != _identity(snapshot), 'changed since'),
        ]
        reasons = [reason for failed, reason in problems if failed]
        if reasons:
            raise PlanError(f'event {event_id} {"; ".join(reasons)}; not removing')

    plan = {
        'external_id': external_id,
        'event_id': link.intervals_event_id,
        'ledger_id': str(link.id),
        'ledger_state': link.state,
        'routine_id': link.routine_id,
        'day_id': link.day_id,
        'date': date,
        'pushed_hash': link.pushed_hash,
        'action': action,
        'event_identity': _identity(event) if event else None,
        'verified_recovery': VERIFIED_RECOVERY,
    }
    plan['removal_hash'] = _hash(plan)
    plan['event'] = event
    plan['approval_required'] = (
        f'irreversibly delete Intervals event {event_id} {external_id} {plan["removal_hash"]}'
        if action == 'delete' and VERIFIED_RECOVERY is None
        else None
    )
    return link, plan


def preview(user, external_id, event_id):
    """What apply would do, plus its removal_hash. GET only; no writes."""
    api_key, athlete_id = _config(user)
    return _check(user, api_key, athlete_id, external_id, event_id)[1]


def apply(user, external_id, event_id, removal_hash, approval=None):
    """Remove the event previewed as removal_hash; see the module docstring.

    Raises PlanError before any write, or IntervalsError/DatabaseError after
    the `removing` tombstone is committed (preview again to resume).
    """
    api_key, athlete_id = _config(user)
    with _run_lock(user):
        link, plan = _check(user, api_key, athlete_id, external_id, event_id, need_write=True)
        if plan['removal_hash'] != removal_hash:
            raise PlanError('Intervals or the ledger changed since the preview; preview again')
        if plan['approval_required'] and approval != plan['approval_required']:
            raise PlanError(
                'Intervals has no verified undo for a deleted event; apply needs the exact '
                'irreversible approval phrase printed by the preview'
            )

        now = timezone.now().isoformat()
        audit = link.removal or {
            'requested_at': now,
            'prior_state': link.state,
            'event': plan['event'],
        }
        audit.setdefault('attempts', []).append(
            {
                'at': now,
                'action': plan['action'],
                'removal_hash': removal_hash,
                'approval': approval,
            }
        )
        # One conditional UPDATE: commits the tombstone only if the row is still
        # exactly what was checked.
        claimed = IntervalsEventLink.objects.filter(
            pk=link.pk,
            state=link.state,
            intervals_event_id=link.intervals_event_id,
            pushed_hash=link.pushed_hash,
        ).update(state='removing', removal=audit)
        if claimed != 1:
            raise PlanError('the ledger row changed during this run; preview again')

        if plan['action'] == 'delete':
            if _identity(client.get_event(api_key, event_id)) != plan['event_identity']:
                raise client.IntervalsError(
                    f'event {event_id} changed in Intervals during this run; not deleted'
                )
            client.delete_event(api_key, event_id)

        _, events = client.fetch_window(api_key, athlete_id, link.date, link.date)
        if not _absent(api_key, event_id, external_id, events):
            raise client.IntervalsError(f'event {event_id} is still in Intervals')
        audit.update(removed_at=timezone.now().isoformat(), readback='absent')
        IntervalsEventLink.objects.filter(pk=link.pk, state='removing').update(
            state='removed', removal=audit
        )
    return {**plan, 'ledger_state': 'removed'}
