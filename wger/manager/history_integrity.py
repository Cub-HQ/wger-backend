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

"""
Owner-scoped, read-only fingerprint of the stored workout history.

Canonicalization ``wger-history-integrity-v2`` (v2 added ``time_unknown`` to the
session fields; v1 snapshot ids are not comparable with v2):

* Scope: the ``WorkoutSession`` and ``WorkoutLog`` rows whose ``user_id`` is the
  requesting user. Routines, days, slots, configs, exercises, session recoveries
  and health/measurement records are not part of this fingerprint.
* Rows are read in ascending ``id`` order, which equals the lexicographic order of
  the canonical UUID strings.
* Each row is a JSON array (RFC 8259) of its values in the declared field order,
  without whitespace, non-ASCII escaped as ``\\uXXXX`` (UTF-16 surrogate pairs),
  followed by one ``\\n``. JSON string escaping keeps the framing unambiguous.
* Values: null -> ``null``; UUID -> lowercase hyphenated string; datetime -> UTC
  string ``YYYY-MM-DDTHH:MM:SS.ffffffZ``; decimal -> fixed-point string with
  exactly the column's decimal places, zero never negative; integer and foreign
  key ids -> JSON number; boolean -> ``true``/``false``; text -> JSON string,
  bytes as stored.
* A table's ``sha256`` is the SHA-256 of its concatenated row lines (the empty
  string for no rows). ``snapshot_id`` is the SHA-256 of the compact, key-sorted
  JSON of ``canonicalization``, ``schema_version`` and every table's ``fields``,
  ``count`` and ``sha256``. ``as_of`` is observation time and is not hashed.

Any change to the declared fields, their order or the encoding requires a new
canonicalization name. ``fingerprint`` refuses to run when a model's concrete
fields differ from the declared ones, so an added column can't silently compare
equal under this contract.

Consistency: counts and digests of both tables come from one snapshot. On
PostgreSQL that is a ``REPEATABLE READ, READ ONLY`` transaction opened just for
this read (no locks are taken; concurrent writers are neither blocked nor seen).
On SQLite, which the test settings use, both scans run in one transaction.
"""

# Standard Library
import datetime
import decimal
import hashlib
import json
import uuid

# Django
from django.core.exceptions import ImproperlyConfigured
from django.db import (
    connection,
    transaction,
)
from django.utils import timezone

# wger
from wger.manager.models import (
    WorkoutLog,
    WorkoutSession,
)


SCHEMA_VERSION = 2
CANONICALIZATION = 'wger-history-integrity-v2'
HASH_ALGORITHM = 'sha256'
CHUNK_SIZE = 2000

TABLES = (
    (
        'workout_session',
        WorkoutSession,
        (
            'id',
            'user_id',
            'routine_id',
            'day_id',
            'datetime_start',
            'datetime_end',
            'notes',
            'impression',
            'time_unknown',
        ),
    ),
    (
        'workout_log',
        WorkoutLog,
        (
            'id',
            'date',
            'user_id',
            'next_log_id',
            'session_id',
            'exercise_id',
            'routine_id',
            'slot_entry_id',
            'iteration',
            'repetitions_unit_id',
            'repetitions',
            'repetitions_target',
            'weight_unit_id',
            'weight',
            'weight_target',
            'average_speed',
            'pace',
            'incline',
            'calories',
            'duration',
            'distance',
            'distance_unit_id',
            'level',
            'max_speed',
            'max_speed_unit_id',
            'rir',
            'rir_target',
            'rest',
            'rest_target',
        ),
    ),
)

SCOPE = {
    'owner': 'authenticated user only',
    'models': ['manager.WorkoutSession', 'manager.WorkoutLog'],
    'excluded': [
        'routines, days, slots and their configs',
        'exercises',
        'session recoveries',
        'health and measurement records',
    ],
}


def _utc(value):
    if timezone.is_naive(value):
        raise ValueError('naive datetime in workout history')
    return value.astimezone(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def encode_value(value, decimal_places=None):
    if value is None or type(value) in (int, str, bool):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return _utc(value)
    if isinstance(value, decimal.Decimal):
        value = value.quantize(decimal.Decimal(1).scaleb(-decimal_places))
        return format(abs(value) if value == 0 else value, 'f')
    raise TypeError(f'no canonical encoding for {type(value).__name__}')


def encode_row(values, places):
    row = [encode_value(value, dp) for value, dp in zip(values, places, strict=True)]
    return json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'


def _table(model, fields, user_id):
    actual = {field.attname for field in model._meta.concrete_fields}
    if actual != set(fields):
        raise ImproperlyConfigured(
            f'{model.__name__} fields changed; {CANONICALIZATION} no longer covers them'
        )
    places = [getattr(model._meta.get_field(name), 'decimal_places', None) for name in fields]
    rows = (
        model.objects.filter(user_id=user_id)
        .order_by('pk')
        .values_list(*fields)
        .iterator(chunk_size=CHUNK_SIZE)
    )
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(encode_row(row, places))
        count += 1
    return {
        'fields': list(fields),
        'order': 'id',
        'count': count,
        HASH_ALGORITHM: digest.hexdigest(),
    }


def fingerprint(user_id):
    """
    Counts and digests of the user's sessions and logs, read from one snapshot
    """
    if connection.vendor == 'postgresql':
        if connection.in_atomic_block:
            # A savepoint inherits the outer transaction's isolation and snapshot.
            raise RuntimeError('history fingerprint needs its own transaction')
        snapshot = 'postgresql repeatable read, read only'
    elif connection.vendor == 'sqlite':
        snapshot = 'sqlite single transaction'
    else:
        raise NotImplementedError(f'no snapshot support for {connection.vendor}')

    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        as_of = timezone.now()
        tables = {name: _table(model, fields, user_id) for name, model, fields in TABLES}

    snapshot_id = hashlib.sha256(
        json.dumps(
            {
                'canonicalization': CANONICALIZATION,
                'schema_version': SCHEMA_VERSION,
                'tables': tables,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()
    return {
        'schema_version': SCHEMA_VERSION,
        'hash_algorithm': HASH_ALGORITHM,
        'canonicalization': CANONICALIZATION,
        'scope': SCOPE,
        'snapshot': snapshot,
        'tables': tables,
        'snapshot_id': snapshot_id,
        'as_of': _utc(as_of),
    }
