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

"""Intervals.icu client. Personal API key, HTTP Basic `API_KEY:<key>`. Writes
are single calls, each followed by a GET readback. Error messages never
contain the key."""

# Third Party
import requests


BASE_URL = 'https://intervals.icu/api/v1'
TIMEOUT_S = 30


class IntervalsError(Exception):
    pass


class RateLimited(IntervalsError):
    pass


def _request(method, api_key, path, params=None, body=None, parse=True):
    error = None
    try:
        response = requests.request(
            method,
            f'{BASE_URL}/{path}',
            params=params,
            json=body,
            auth=('API_KEY', api_key),
            timeout=TIMEOUT_S,
        )
    except requests.Timeout:
        error = IntervalsError(f'no response within {TIMEOUT_S}s')
    except requests.RequestException as e:
        # The exception text can echo the request; keep only its type.
        error = IntervalsError(f'request failed ({type(e).__name__})')
    else:
        if response.status_code == 429:
            retry = response.headers.get('Retry-After', '?')
            error = RateLimited(f'rate limited, retry after {retry}s')
        elif response.status_code in (401, 403):
            error = IntervalsError(f'HTTP {response.status_code}, API key rejected')
        elif response.status_code != 200:
            error = IntervalsError(f'HTTP {response.status_code}')
        elif not parse:
            return None
        else:
            try:
                return response.json()
            except ValueError:
                error = IntervalsError('response is not JSON')
    error.args = (f'{method} {path}: {error}'.replace(api_key, '[redacted]'),)
    raise error


def _get(api_key, path, params=None):
    return _request('GET', api_key, path, params)


def get_event(api_key, event_id):
    event = _get(api_key, f'athlete/0/events/{event_id}')
    if not isinstance(event, dict) or event.get('id') is None:
        raise IntervalsError(f'GET athlete/0/events/{event_id}: expected an event')
    return event


def create_event(api_key, payload):
    """POST one event, then return what Intervals actually stored (readback)."""
    created = _request('POST', api_key, 'athlete/0/events', {'upsertOnUid': 'false'}, payload)
    if not isinstance(created, dict) or created.get('id') is None:
        raise IntervalsError('POST athlete/0/events: no event id in response')
    return get_event(api_key, created['id'])


def update_event(api_key, event_id, payload):
    _request('PUT', api_key, f'athlete/0/events/{event_id}', body=payload)
    return get_event(api_key, event_id)


def delete_event(api_key, event_id):
    _request('DELETE', api_key, f'athlete/0/events/{event_id}', parse=False)


def bind_athlete(api_key, athlete_id, need_write=False):
    """The athlete record, if the key belongs to `athlete_id` (and has WRITE
    permission when `need_write`). Call before reading or writing any data."""
    if not api_key or not athlete_id:
        raise IntervalsError('INTERVALS_API_KEY and INTERVALS_ATHLETE_ID must be set')
    athlete = _get(api_key, 'athlete/0')
    if not isinstance(athlete, dict) or str(athlete.get('id')) != str(athlete_id):
        raise IntervalsError('API key belongs to a different athlete than INTERVALS_ATHLETE_ID')
    if need_write and athlete.get('icu_permission') != 'WRITE':
        raise IntervalsError('API key has no WRITE permission on this athlete')
    return athlete


def list_activities(api_key, oldest, newest):
    """Completed activities in the inclusive local-date window. Call bind_athlete first."""
    # Activities take a local date-time; end of day keeps `newest` inclusive.
    rows = _get(
        api_key,
        'athlete/0/activities',
        {'oldest': oldest.isoformat(), 'newest': f'{newest.isoformat()}T23:59:59'},
    )
    if not isinstance(rows, list):
        raise IntervalsError('GET athlete/0/activities: expected a list')
    return rows


def get_activity(api_key, activity_id):
    activity = _get(api_key, f'activity/{activity_id}')
    if not isinstance(activity, dict) or activity.get('id') is None:
        raise IntervalsError(f'GET activity/{activity_id}: expected an activity')
    return activity


def create_manual_activity(api_key, payload):
    """POST one completed manual activity, then return what Intervals stored (readback)."""
    created = _request('POST', api_key, 'athlete/0/activities/manual', body=payload)
    if not isinstance(created, dict) or created.get('id') is None:
        raise IntervalsError('POST athlete/0/activities/manual: no activity id in response')
    return get_activity(api_key, created['id'])


def list_events(api_key, oldest, newest):
    """Calendar events (plans, notes) in the inclusive local-date window."""
    rows = _get(
        api_key,
        'athlete/0/events',
        {'oldest': oldest.isoformat(), 'newest': newest.isoformat()},
    )
    if not isinstance(rows, list):
        raise IntervalsError('GET athlete/0/events: expected a list')
    return rows


def fetch_window(api_key, athlete_id, oldest, newest, need_write=False):
    """Planned-push only: bind the key, then (activities, events) for the window."""
    bind_athlete(api_key, athlete_id, need_write)
    return list_activities(api_key, oldest, newest), list_events(api_key, oldest, newest)
