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

"""GET-only Intervals.icu client for the inbound mirror. Personal API key,
HTTP Basic `API_KEY:<key>`. Error messages never contain the key."""

# Third Party
import requests


BASE_URL = 'https://intervals.icu/api/v1'
TIMEOUT_S = 30


class IntervalsError(Exception):
    pass


class RateLimited(IntervalsError):
    pass


def _get(api_key, path, params=None):
    error = None
    try:
        response = requests.get(
            f'{BASE_URL}/{path}',
            params=params,
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
        else:
            try:
                return response.json()
            except ValueError:
                error = IntervalsError('response is not JSON')
    error.args = (f'GET {path}: {error}'.replace(api_key, '[redacted]'),)
    raise error


def fetch_window(api_key, athlete_id, oldest, newest):
    """Bind the key to `athlete_id`, then return (activities, events) for the
    inclusive local-date window. Refuses before reading data if the key belongs
    to another athlete."""
    if not api_key or not athlete_id:
        raise IntervalsError('INTERVALS_API_KEY and INTERVALS_ATHLETE_ID must be set')
    athlete = _get(api_key, 'athlete/0')
    if not isinstance(athlete, dict) or str(athlete.get('id')) != str(athlete_id):
        raise IntervalsError('API key belongs to a different athlete than INTERVALS_ATHLETE_ID')

    # Activities take a local date-time; end of day keeps `newest` inclusive.
    activities = _get(
        api_key,
        'athlete/0/activities',
        {'oldest': oldest.isoformat(), 'newest': f'{newest.isoformat()}T23:59:59'},
    )
    events = _get(
        api_key,
        'athlete/0/events',
        {'oldest': oldest.isoformat(), 'newest': newest.isoformat()},
    )
    for label, rows in (('activities', activities), ('events', events)):
        if not isinstance(rows, list):
            raise IntervalsError(f'GET athlete/0/{label}: expected a list')
    return activities, events
