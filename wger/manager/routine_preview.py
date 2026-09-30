#  This file is part of wger Workout Manager <https://github.com/wger-project>.
#
#  wger Workout Manager is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  wger Workout Manager is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Owner-private routine previews (Cub-HQ/wger-gym#26)

A preview validates a proposed native routine, resolves its full calendar with
the native scheduling and config code, and stores the canonical proposal and
schedule as one immutable ``RoutinePreview`` row. It builds unsaved model
instances only: no routine, day, slot, entry, config, session or log is ever
written, not even temporarily.

Only explicit weekly targets are supported, so that an iteration is always a
calendar week: see ``_weekly_cycle`` and the config row checks.
"""

# Standard Library
import datetime
import hashlib
import json
from decimal import (
    Decimal,
    InvalidOperation,
)

# Django
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import (
    IntegrityError,
    models,
    transaction,
)
from django.http import Http404
from django.urls import reverse
from django.utils import (
    timezone,
    translation,
)

# wger
from wger.core.models import (
    RepetitionUnit,
    WeightUnit,
)
from wger.exercises.models import Exercise
from wger.manager.consts import (
    REP_UNIT_REPETITIONS,
    WEIGHT_UNIT_KG,
)
from wger.manager.models import (
    Day,
    Label,
    MaxRepetitionsConfig,
    MaxRestConfig,
    MaxRiRConfig,
    MaxSetsConfig,
    MaxWeightConfig,
    RepetitionsConfig,
    RestConfig,
    RiRConfig,
    RoutinePreview,
    SetsConfig,
    Slot,
    SlotEntry,
    WeightConfig,
)
from wger.manager.models.routine import resolve_date_sequence


SCHEMA_VERSION = 1
PROPOSAL_VERSION = 1
PREVIEW_LIFETIME = datetime.timedelta(days=14)
MAX_BODY_BYTES = 256 * 1024
MAX_ACTIVE_PREVIEWS = 50
MAX_ENTRIES = 150
"""Entries over the whole program, bounds the size of the stored schedule"""

STATUS = 'proposal_awaiting_approval'

# Proposal key -> (config model, field name of the native config walk)
CONFIG_MODELS = {
    'sets': (SetsConfig, 'sets'),
    'max_sets': (MaxSetsConfig, 'maxsets'),
    'repetitions': (RepetitionsConfig, 'repetitions'),
    'max_repetitions': (MaxRepetitionsConfig, 'maxrepetitions'),
    'weight': (WeightConfig, 'weight'),
    'max_weight': (MaxWeightConfig, 'maxweight'),
    'rir': (RiRConfig, 'rir'),
    'max_rir': (MaxRiRConfig, 'maxrir'),
    'rest': (RestConfig, 'rest'),
    'max_rest': (MaxRestConfig, 'maxrest'),
}

# The routine-recovery ``replacement`` v1 keys
_BODY_KEYS = {'schema_version', 'external_version', 'idempotency_key', 'proposal'}
_PROPOSAL_KEYS = {'version', 'routine', 'labels', 'days'}
_ROUTINE_KEYS = {'name', 'description', 'start', 'end', 'fit_in_week'}
_LABEL_KEYS = {'start_offset', 'end_offset', 'label', 'comment'}
_DAY_KEYS = {
    'order',
    'type',
    'name',
    'description',
    'is_rest',
    'need_logs_to_advance',
    'config',
    'slots',
}
_SLOT_KEYS = {'order', 'comment', 'config', 'entries'}
_ENTRY_KEYS = {
    'exercise',
    'order',
    'type',
    'comment',
    'repetition_unit',
    'repetition_rounding',
    'weight_unit',
    'weight_rounding',
    'class_name',
    'config',
    'configs',
}
_CONFIG_KEYS = {'iteration', 'value', 'operation', 'step', 'repeat', 'requirements'}


class PreviewError(Exception):
    """A refused request; ``status`` and ``code`` map onto the API error body"""

    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def body(self):
        return {'detail': self.detail, 'code': self.code, **self.extra}


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), cls=DjangoJSONEncoder)


def _sha(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _decimal_str(value: Decimal | None) -> str | None:
    """One spelling per number, so equal plans hash equally ("62.50" == "62.5")"""
    return None if value is None else format(value.normalize(), 'f')


class _Errors(list):
    def add(self, path, error, code='invalid'):
        self.append({'path': path, 'code': code, 'error': error})

    def unsupported(self, path, error):
        self.add(path, error, 'unsupported')


def _object(value, allowed, path, errors, required=()):
    """The dict, or None after recording why not; unknown keys are refused"""
    if not isinstance(value, dict):
        errors.add(path, 'must be an object')
        return None
    for key in sorted(set(value) - allowed):
        errors.unsupported(f'{path}.{key}', 'unsupported field')
    for key in required:
        if key not in value:
            errors.add(f'{path}.{key}', 'required')
    return value


def _list(value, path, errors, required=False):
    if value is None and not required:
        return []
    if not isinstance(value, list):
        errors.add(path, 'must be a list')
        return []
    return value


def _int(value, path, errors, minimum=0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.add(path, f'must be a whole number >= {minimum}')
        return None
    return value


def _decimal(value, path, errors) -> Decimal | None:
    """Numbers and numeric strings; floats go through str so 60.1 stays 60.1"""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        errors.add(path, 'must be a number')
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        errors.add(path, 'must be a number')
        return None
    if not result.is_finite():
        errors.add(path, 'must be a number')
        return None
    return result


def _clean(instance, path, errors, fields):
    """
    The model's own field validators, for the given fields only

    Empty values are the model defaults a native save stores as they are.
    Valid decimals get the column's places, as a saved and re-read row has
    them, since the display text shows them ("62.50 kg").
    """
    exclude = [
        f.name
        for f in instance._meta.concrete_fields
        if f.name not in fields or getattr(instance, f.attname) in (None, '')
    ]
    try:
        instance.full_clean(exclude=exclude, validate_unique=False, validate_constraints=False)
    except ValidationError as e:
        for field, messages in e.message_dict.items():
            errors.add(f'{path}.{field}', ' '.join(messages))
        return
    for f in instance._meta.concrete_fields:
        value = getattr(instance, f.attname)
        if isinstance(f, models.DecimalField) and f.name not in exclude and value is not None:
            setattr(instance, f.attname, value.quantize(Decimal(1).scaleb(-f.decimal_places)))


def _unique_orders(items, path, errors):
    seen = set()
    for i, order in items:
        if order is not None and order in seen:
            errors.add(f'{path}[{i}].order', f'duplicate order {order}')
        seen.add(order)


def _no_custom_config(raw, path, errors):
    if raw.get('config') is not None:
        errors.unsupported(f'{path}.config', 'custom config is not supported in a preview')


class _Plan:
    """The validated proposal: canonical JSON plus the unsaved native objects"""

    def __init__(self):
        self.routine = {}
        self.labels = []
        self.days = []
        """[(Day, canonical dict)], sorted by order"""
        self.entries = []
        """[(SlotEntry, canonical configs dict, path)]"""

    @property
    def canonical(self):
        return {
            'version': PROPOSAL_VERSION,
            'routine': self.routine,
            'labels': self.labels,
            'days': [canonical for _, canonical in self.days],
        }


def _parse_routine(raw, errors, plan):
    # Local, the serializers module imports the models
    # wger
    from wger.manager.api.serializers import RoutineSerializer

    meta = _object(raw, _ROUTINE_KEYS, 'routine', errors, ('name', 'start', 'end'))
    if meta is None:
        return
    fit_in_week = meta.get('fit_in_week', False)
    if not isinstance(fit_in_week, bool):
        errors.add('routine.fit_in_week', 'must be true or false')
    serializer = RoutineSerializer(data={k: v for k, v in meta.items() if k in _ROUTINE_KEYS})
    if not serializer.is_valid():
        for key, messages in serializer.errors.items():
            errors.add(f'routine.{key}', ' '.join(map(str, messages)))
        return
    data = serializer.validated_data
    plan.routine = {
        'name': data['name'],
        'description': data.get('description', ''),
        'start': data['start'],
        'end': data['end'],
        'fit_in_week': fit_in_week is True,
    }


def _parse_labels(raw_labels, errors, plan):
    labels = []
    for i, raw in enumerate(_list(raw_labels, 'labels', errors)):
        path = f'labels[{i}]'
        raw = _object(raw, _LABEL_KEYS, path, errors, ('start_offset', 'end_offset', 'label'))
        if raw is None:
            continue
        start = _int(raw.get('start_offset'), f'{path}.start_offset', errors)
        end = _int(raw.get('end_offset'), f'{path}.end_offset', errors)
        label = Label(label=raw.get('label'), comment=raw.get('comment', ''))
        _clean(label, path, errors, ('label', 'comment'))
        if start is None or end is None:
            continue
        if end < start:
            errors.add(f'{path}.end_offset', 'must not be before start_offset')
            continue
        labels.append((start, end, label, path))

    labels.sort(key=lambda row: row[:2])
    for (_, previous_end, _, _), (start, _, _, path) in zip(labels, labels[1:]):
        if start <= previous_end:
            errors.add(f'{path}.start_offset', 'labels must not overlap')

    if plan.routine:
        duration = (plan.routine['end'] - plan.routine['start']).days
        for _, end, _, path in labels:
            if end > duration:
                errors.add(f'{path}.end_offset', f'the routine ends at offset {duration}')

    plan.labels = [
        {'start_offset': start, 'end_offset': end, 'label': label.label, 'comment': label.comment}
        for start, end, label, _ in labels
    ]


def _parse_configs(raw_configs, path, errors):
    configs = {}
    if _object(raw_configs, set(CONFIG_MODELS), path, errors) is None:
        raw_configs = {}
    for key, (model, _) in CONFIG_MODELS.items():
        rows = {}
        value_field = model._meta.get_field('value')
        whole = isinstance(value_field, models.IntegerField)
        for i, raw in enumerate(_list(raw_configs.get(key), f'{path}.{key}', errors)):
            c_path = f'{path}.{key}[{i}]'
            raw = _object(raw, _CONFIG_KEYS, c_path, errors, ('iteration', 'value'))
            if raw is None:
                continue
            if raw.get('operation', 'r') != 'r':
                errors.unsupported(f'{c_path}.operation', 'only "r" (explicit target) is supported')
            if raw.get('step') not in (None, 'na'):
                errors.unsupported(f'{c_path}.step', 'steps only apply to +/- operations')
            if raw.get('repeat', False) is not False:
                errors.unsupported(f'{c_path}.repeat', 'repeating progressions are not supported')
            if raw.get('requirements') is not None:
                errors.unsupported(f'{c_path}.requirements', 'log requirements are not supported')

            iteration = _int(raw.get('iteration'), f'{c_path}.iteration', errors, minimum=1)
            value = _decimal(raw.get('value'), f'{c_path}.value', errors)
            if iteration is None or value is None:
                continue
            if iteration in rows:
                errors.add(f'{c_path}.iteration', f'duplicate iteration {iteration}')
                continue
            if whole:
                if value != value.to_integral_value():
                    errors.add(f'{c_path}.value', 'must be a whole number')
                    continue
                value = int(value)
            config = model(
                iteration=iteration,
                value=value,
                operation='r',
                step='na',
                repeat=False,
                requirements=None,
            )
            _clean(config, c_path, errors, ('iteration', 'value'))
            rows[iteration] = (config, c_path)
        configs[key] = [rows[i] for i in sorted(rows)]
    return configs


def _parse_entry(raw, path, errors, profile):
    raw = _object(raw, _ENTRY_KEYS, path, errors, ('exercise', 'order'))
    if raw is None:
        return None
    _no_custom_config(raw, path, errors)
    if raw.get('class_name') is not None:
        errors.unsupported(f'{path}.class_name', 'custom calculation classes are not supported')

    exercise = _int(raw.get('exercise'), f'{path}.exercise', errors, minimum=1)
    order = _int(raw.get('order'), f'{path}.order', errors)
    repetition_unit = _int(
        raw.get('repetition_unit', REP_UNIT_REPETITIONS),
        f'{path}.repetition_unit',
        errors,
        minimum=1,
    )
    weight_unit = _int(
        raw.get('weight_unit', WEIGHT_UNIT_KG), f'{path}.weight_unit', errors, minimum=1
    )

    # Omitted or empty rounding takes the profile default, like SlotEntry.save
    rounding = {}
    for key, profile_key in (
        ('repetition_rounding', 'repetitions_rounding'),
        ('weight_rounding', 'weight_rounding'),
    ):
        value = raw.get(key)
        value = _decimal(value, f'{path}.{key}', errors) if value is not None else None
        rounding[key] = value or getattr(profile, profile_key)

    entry = SlotEntry(
        exercise_id=exercise,
        order=order,
        type=raw.get('type', SlotEntry._meta.get_field('type').default),
        comment=raw.get('comment', ''),
        repetition_unit_id=repetition_unit,
        weight_unit_id=weight_unit,
        **rounding,
    )
    _clean(entry, path, errors, ('order', 'type', 'comment', *rounding))
    configs = _parse_configs(raw.get('configs') or {}, f'{path}.configs', errors)

    canonical = {
        'exercise': exercise,
        'order': order,
        'type': entry.type,
        'comment': entry.comment,
        'repetition_unit': repetition_unit,
        'repetition_rounding': _decimal_str(entry.repetition_rounding),
        'weight_unit': weight_unit,
        'weight_rounding': _decimal_str(entry.weight_rounding),
        'class_name': None,
        'config': None,
        'configs': {
            key: [
                {
                    'iteration': config.iteration,
                    'value': config.value
                    if isinstance(config.value, int)
                    else _decimal_str(config.value),
                    'operation': 'r',
                    'step': 'na',
                    'repeat': False,
                    'requirements': None,
                }
                for config, _ in rows
            ]
            for key, rows in configs.items()
        },
    }
    return entry, configs, canonical


def _parse_days(raw_days, errors, plan, profile):
    raw_days = _list(raw_days, 'days', errors, required=True)
    if not raw_days:
        errors.add('days', 'at least one day is required')

    days = []
    for d, raw in enumerate(raw_days):
        path = f'days[{d}]'
        raw = _object(raw, _DAY_KEYS, path, errors, ('order',))
        if raw is None:
            continue
        _no_custom_config(raw, path, errors)
        if raw.get('need_logs_to_advance', False) is not False:
            errors.unsupported(
                f'{path}.need_logs_to_advance',
                'days that wait for logs have no fixed calendar',
            )
        order = _int(raw.get('order'), f'{path}.order', errors, minimum=1)
        is_rest = raw.get('is_rest', False)
        if not isinstance(is_rest, bool):
            errors.add(f'{path}.is_rest', 'must be true or false')
        day = Day(
            order=order,
            type=raw.get('type', Day._meta.get_field('type').default),
            name=raw.get('name', ''),
            description=raw.get('description', ''),
            is_rest=is_rest is True,
            need_logs_to_advance=False,
        )
        _clean(day, path, errors, ('order', 'type', 'name', 'description'))

        raw_slots = _list(raw.get('slots'), f'{path}.slots', errors)
        if day.is_rest and raw_slots:
            errors.add(f'{path}.slots', 'a rest day has no exercises')

        slots = []
        canonical_slots = []
        slot_orders = []
        for s, raw_slot in enumerate(raw_slots):
            s_path = f'{path}.slots[{s}]'
            raw_slot = _object(raw_slot, _SLOT_KEYS, s_path, errors, ('order',))
            if raw_slot is None:
                continue
            _no_custom_config(raw_slot, s_path, errors)
            slot = Slot(
                order=_int(raw_slot.get('order'), f'{s_path}.order', errors),
                comment=raw_slot.get('comment', ''),
            )
            _clean(slot, s_path, errors, ('comment',))
            slot_orders.append((s, slot.order))

            entries = []
            entry_orders = []
            for e, raw_entry in enumerate(
                _list(raw_slot.get('entries'), f'{s_path}.entries', errors)
            ):
                e_path = f'{s_path}.entries[{e}]'
                parsed = _parse_entry(raw_entry, e_path, errors, profile)
                if parsed is None:
                    continue
                entry, configs, canonical = parsed
                entry_orders.append((e, entry.order))
                entries.append((entry, canonical))
                plan.entries.append((entry, configs, e_path, day))
            _unique_orders(entry_orders, f'{s_path}.entries', errors)

            entries.sort(key=lambda row: row[0].order or 0)
            slot.prefetched_entries = [entry for entry, _ in entries]
            slots.append((slot, [canonical for _, canonical in entries]))
        _unique_orders(slot_orders, f'{path}.slots', errors)

        slots.sort(key=lambda row: row[0].order or 0)
        day.prefetched_slots = [slot for slot, _ in slots]
        for slot, entries in slots:
            canonical_slots.append(
                {'order': slot.order, 'comment': slot.comment, 'config': None, 'entries': entries}
            )
        days.append(
            (
                d,
                day,
                {
                    'order': order,
                    'type': day.type,
                    'name': day.name,
                    'description': day.description,
                    'is_rest': day.is_rest,
                    'need_logs_to_advance': False,
                    'config': None,
                    'slots': canonical_slots,
                },
            )
        )
    _unique_orders([(d, day.order) for d, day, _ in days], 'days', errors)

    days.sort(key=lambda row: row[1].order or 0)
    plan.days = [(day, canonical) for _, day, canonical in days]


def _weekly_cycle(plan, errors):
    """Iteration is a calendar week only for these two native shapes"""
    count = len(plan.days)
    routine = plan.routine
    if count == 7:
        return
    if count < 7 and routine['fit_in_week'] and routine['start'].weekday() == 0:
        return
    errors.unsupported(
        'days',
        'weeks are only fixed with exactly 7 days (training and rest days), or fewer '
        'days with routine.fit_in_week true and a Monday start',
    )


def _check_references(plan, errors):
    """Exercises and units must exist; attach the units so display needs no query"""
    wanted = {
        'exercise': (Exercise, {row[0].exercise_id for row in plan.entries}),
        'repetition_unit': (RepetitionUnit, {row[0].repetition_unit_id for row in plan.entries}),
        'weight_unit': (WeightUnit, {row[0].weight_unit_id for row in plan.entries}),
    }
    found = {key: model.objects.in_bulk(ids - {None}) for key, (model, ids) in wanted.items()}
    for entry, _, path, _ in plan.entries:
        for key in wanted:
            pk = getattr(entry, f'{key}_id')
            if pk is not None and pk not in found[key]:
                errors.add(f'{path}.{key}', f'{key.replace("_", " ")} {pk} does not exist')
            elif pk is not None and key != 'exercise':
                setattr(entry, key, found[key][pk])
    return found['exercise']


def _schedule(plan, errors):
    """
    The native display schedule of the unsaved plan

    Every entry resolves its targets with the native config walk over its
    proposed rows and no logs (there can be none: nothing is saved).
    """
    # Local, the serializers module imports the models
    # wger
    from wger.manager.api.serializers import WorkoutDayDataDisplayModeSerializer

    for entry, configs, _, _ in plan.entries:
        walk_configs = {
            field: [config for config, _ in configs[key]]
            for key, (_, field) in CONFIG_MODELS.items()
        }
        # Instance attribute shadows the method: Slot.set_data calls it and would
        # otherwise read (unsaved, so empty) configs and logs from the database
        entry.get_config_data = lambda iteration, e=entry, c=walk_configs: e.resolve_config_data(
            iteration, c, []
        )

    routine = plan.routine
    labels = {}
    for label in plan.labels:
        for offset in range(label['start_offset'], label['end_offset'] + 1):
            labels[routine['start'] + datetime.timedelta(days=offset)] = label['label']

    days = [day for day, _ in plan.days]
    sequence = resolve_date_sequence(
        routine['start'],
        routine['end'],
        days,
        routine['fit_in_week'],
        labels,
        lambda day, date: True,
    )

    # Rows after the last week of their day would never be shown
    last_iteration = {}
    for item in sequence:
        if item.day is not None:
            last_iteration[id(item.day)] = item.iteration
    for _, configs, _, day in plan.entries:
        weeks = last_iteration.get(id(day), 0)
        for rows in configs.values():
            for config, c_path in rows:
                if config.iteration > weeks:
                    errors.add(f'{c_path}.iteration', f'the day only runs {weeks} weeks')

    data = WorkoutDayDataDisplayModeSerializer(sequence, many=True).data
    return json.loads(_canonical_json(data))


def build(user, proposal):
    """
    Validates a proposal; (canonical proposal, schedule, exercise names)

    Raises PreviewError with every problem found. Writes nothing.
    """
    errors = _Errors()
    plan = _Plan()
    raw = _object(proposal, _PROPOSAL_KEYS, 'proposal', errors, ('version', 'routine', 'days'))
    if raw is None or raw.get('version') != PROPOSAL_VERSION:
        if raw is not None:
            errors.add('proposal.version', f'must be {PROPOSAL_VERSION}')
        raise PreviewError(400, 'invalid_proposal', 'The proposal is not valid.', errors=errors)

    _parse_routine(raw.get('routine'), errors, plan)
    _parse_labels(raw.get('labels'), errors, plan)
    _parse_days(raw.get('days'), errors, plan, user.userprofile)
    if len(plan.entries) > MAX_ENTRIES:
        errors.add('days', f'at most {MAX_ENTRIES} exercise entries per program')
    exercises = _check_references(plan, errors)
    if not errors and plan.routine:
        _weekly_cycle(plan, errors)
    schedule = _schedule(plan, errors) if not errors else None
    if errors:
        raise PreviewError(400, 'invalid_proposal', 'The proposal is not valid.', errors=errors)

    canonical = json.loads(_canonical_json(plan.canonical))
    names = {}
    for pk, exercise in exercises.items():
        translation_ = exercise.get_translation()
        names[str(pk)] = translation_.name if translation_ else ''
    return canonical, schedule, names


def _validate_body(body):
    errors = _Errors()
    body = _object(body, _BODY_KEYS, 'body', errors, tuple(sorted(_BODY_KEYS)))
    if body is not None:
        if body.get('schema_version') != SCHEMA_VERSION:
            errors.add('schema_version', f'must be {SCHEMA_VERSION}')
        key = body.get('idempotency_key')
        if not isinstance(key, str) or not 1 <= len(key) <= 64 or not key.isprintable():
            errors.add('idempotency_key', 'must be a text of 1 to 64 characters')
        version = body.get('external_version')
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 100
            or not version.isprintable()
        ):
            errors.add('external_version', 'must be a text of 1 to 100 characters')
    if errors:
        raise PreviewError(400, 'invalid_request', 'The request is not valid.', errors=errors)
    return body


def _replay(preview, request_hash):
    if preview.request_hash != request_hash:
        raise PreviewError(
            409,
            'idempotency_conflict',
            'This idempotency_key was already used with a different request.',
            preview_id=str(preview.pk),
        )
    return preview


def create(user, body):
    """(preview, replayed) for a POST body; raises PreviewError"""
    body = _validate_body(body)
    request_hash = _sha({k: body[k] for k in ('schema_version', 'external_version', 'proposal')})
    now = timezone.now()

    # Expired previews are gone for good, there is no background job
    RoutinePreview.objects.filter(user=user, expires_at__lte=now).delete()

    existing = RoutinePreview.objects.filter(user=user, idempotency_key=body['idempotency_key'])
    if (preview := existing.first()) is not None:
        return _replay(preview, request_hash), True

    canonical, schedule, names = build(user, body['proposal'])
    plan_hash = _sha(canonical)

    same = RoutinePreview.objects.filter(
        user=user,
        plan_hash=plan_hash,
        external_version=body['external_version'],
    ).first()
    if same is not None:
        return same, True

    if RoutinePreview.objects.filter(user=user).count() >= MAX_ACTIVE_PREVIEWS:
        raise PreviewError(
            429,
            'too_many_previews',
            f'At most {MAX_ACTIVE_PREVIEWS} unexpired previews per user.',
        )

    try:
        with transaction.atomic():
            preview = RoutinePreview(
                user=user,
                idempotency_key=body['idempotency_key'],
                external_version=body['external_version'],
                request_hash=request_hash,
                plan_hash=plan_hash,
                canonical_proposal=canonical,
                schedule=schedule,
                exercise_names=names,
                created_at=now,
                expires_at=now + PREVIEW_LIFETIME,
            )
            preview.save()
    except IntegrityError:
        # A concurrent retry with the same key won the race
        return _replay(existing.get(), request_hash), True
    return preview, False


def owned(request, preview_id) -> RoutinePreview:
    """The requesting owner's unexpired preview; 404 for anyone else, 410 when expired"""
    # A trainer logged in as a member must not see the member's private proposals
    if request.session.get('trainer.identity'):
        raise Http404
    preview = RoutinePreview.objects.filter(pk=preview_id, user=request.user).first()
    if preview is None:
        raise Http404
    if preview.is_expired:
        raise PreviewError(
            410,
            'preview_expired',
            'This preview has expired.',
            expired_at=preview.expires_at.isoformat(),
        )
    return preview


def preview_url(request, preview) -> str:
    with translation.override(settings.LANGUAGE_CODE):
        path = reverse('manager:routine:preview', kwargs={'preview_id': preview.pk})
    return request.build_absolute_uri(path)


def payload(request, preview) -> dict:
    return {
        'preview_id': str(preview.pk),
        'schema_version': SCHEMA_VERSION,
        'status': STATUS,
        'external_version': preview.external_version,
        'plan_hash': preview.plan_hash,
        'created_at': preview.created_at.isoformat(),
        'expires_at': preview.expires_at.isoformat(),
        'preview_url': preview_url(request, preview),
        'canonical_proposal': preview.canonical_proposal,
        'exercise_names': preview.exercise_names,
        'schedule': preview.schedule,
    }
