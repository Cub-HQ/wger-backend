"""
Fourteen-day routine trash/restore and recoverable plan edits/rebuilds (wger-gym#24)

One service for the UI and API. Nothing a completed session or log points at is
ever deleted or rewritten here:

* trash sets ``Routine.deleted_at``; the graph stays in place.
* edits happen in place; a full snapshot of the prior graph is kept and undo
  reapplies it by primary key.
* rebuild creates a new routine and tombstones the old one (``replaced_by``),
  leaving the old graph and every historical FK untouched.

Each operation leaves a ``RoutineRecovery`` row restorable for exactly 14x24h.
Undo is last-in-first-out: it needs the routine to still be in the state the
operation left it in, so later edits are never silently overwritten. Rows that
history references are never deleted by a restore; that fails with a conflict.
"""

# Standard Library
import datetime
import hashlib
import importlib.util
import json
from contextlib import contextmanager

# Django
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import (
    IntegrityError,
    transaction,
)
from django.db.models import (
    ProtectedError,
    RestrictedError,
)
from django.utils import timezone

# Third Party
from rest_framework import serializers

# wger
from wger.manager.api.validators import validate_requirements
from wger.manager.helpers import reset_routine_cache
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
    Routine,
    RoutineRecovery,
    SetsConfig,
    Slot,
    SlotEntry,
    WeightConfig,
    WorkoutSessionRecovery,
)


RECOVERY_WINDOW = datetime.timedelta(days=14)
REPLACEMENT_VERSION = 1

CONFIG_MODELS = {
    'weight': WeightConfig,
    'max_weight': MaxWeightConfig,
    'repetitions': RepetitionsConfig,
    'max_repetitions': MaxRepetitionsConfig,
    'sets': SetsConfig,
    'max_sets': MaxSetsConfig,
    'rest': RestConfig,
    'max_rest': MaxRestConfig,
    'rir': RiRConfig,
    'max_rir': MaxRiRConfig,
}

ROUTINE_FIELDS = (
    'name',
    'description',
    'start',
    'end',
    'fit_in_week',
    'is_template',
    'is_public',
    'deleted_at',
    'replaced_by_id',
)
LABEL_FIELDS = ('start_offset', 'end_offset', 'label', 'comment')
DAY_FIELDS = ('order', 'type', 'name', 'description', 'is_rest', 'need_logs_to_advance', 'config')
SLOT_FIELDS = ('order', 'comment', 'config')
ENTRY_FIELDS = (
    'exercise_id',
    'order',
    'type',
    'comment',
    'repetition_unit_id',
    'repetition_rounding',
    'weight_unit_id',
    'weight_rounding',
    'class_name',
    'config',
)
CONFIG_FIELDS = ('iteration', 'value', 'operation', 'step', 'repeat', 'requirements')


class RecoveryError(Exception):
    """A refused operation; `status` and `code` map onto the API error body"""

    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def body(self):
        return {'detail': self.detail, 'code': self.code, **self.extra}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), cls=DjangoJSONEncoder)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _row(obj, fields):
    return {'id': obj.pk, **{f: getattr(obj, f) for f in fields}}


def snapshot(routine: Routine) -> dict:
    """The complete plan graph as JSON-compatible data (primary keys included)"""
    routine = Routine.objects.get(pk=routine.pk)
    days = []
    for day in Day.objects.filter(routine=routine).order_by('pk'):
        slots = []
        for slot in Slot.objects.filter(day=day).order_by('pk'):
            entries = []
            for entry in SlotEntry.objects.filter(slot=slot).order_by('pk'):
                row = _row(entry, ENTRY_FIELDS)
                row['configs'] = {
                    key: [
                        _row(c, CONFIG_FIELDS)
                        for c in model.objects.filter(slot_entry=entry).order_by('pk')
                    ]
                    for key, model in CONFIG_MODELS.items()
                }
                entries.append(row)
            slots.append({**_row(slot, SLOT_FIELDS), 'entries': entries})
        days.append({**_row(day, DAY_FIELDS), 'slots': slots})
    data = {
        'routine': _row(routine, ROUTINE_FIELDS),
        'labels': [_row(label, LABEL_FIELDS) for label in routine.labels.order_by('pk')],
        'days': days,
    }
    # Round-trip through JSON so snapshots taken now compare equal to stored ones
    return json.loads(_canonical(data))


def revision_of(data: dict) -> str:
    return _sha(data)


def revision(routine: Routine) -> str:
    return revision_of(snapshot(routine))


def routine_id_of(obj) -> int:
    """The routine a plan object belongs to"""
    if isinstance(obj, Routine):
        return obj.pk
    if isinstance(obj, (Day, Label)):
        return obj.routine_id
    if isinstance(obj, Slot):
        return obj.day.routine_id
    if isinstance(obj, SlotEntry):
        return obj.slot.day.routine_id
    return obj.slot_entry.slot.day.routine_id


def _now():
    return timezone.now()


def iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def _lock(user, routine_id) -> Routine:
    routine = Routine.objects.select_for_update().filter(pk=routine_id, user=user).first()
    if routine is None:
        raise RecoveryError(404, 'not_found', 'Routine not found.')
    return routine


def _invalidate_on_commit(*routines):
    for routine in routines:
        transaction.on_commit(lambda r=routine: reset_routine_cache(Routine.objects.get(pk=r.pk)))


def _check_revision(current, expected):
    if expected != current:
        raise RecoveryError(
            409, 'stale_revision', 'The plan changed since that revision.', current_revision=current
        )


def _key_and_hash(idempotency_key, request):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 100:
        raise RecoveryError(400, 'invalid', 'idempotency_key must be a string of 1-100 characters.')
    return idempotency_key, _sha(request)


def _replay(user, key, request_hash):
    """The stored receipt for a repeated request, None for a new key"""
    row = RoutineRecovery.objects.filter(user=user, idempotency_key=key).first()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise RecoveryError(
            409, 'idempotency_conflict', 'idempotency_key was already used for another request.'
        )
    return row.receipt


def _record(user, routine, operation, before, revision_after, key=None, request_hash=None, now=None, **kw):
    now = now or _now()
    row = RoutineRecovery(
        user=user,
        routine=routine,
        operation=operation,
        created_at=now,
        expires_at=now + RECOVERY_WINDOW,
        snapshot=before,
        revision_after=revision_after,
        idempotency_key=key,
        request_hash=request_hash,
        **kw,
    )
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        # The same key committed concurrently for a different routine
        raise RecoveryError(
            409, 'idempotency_conflict', 'idempotency_key was already used for another request.'
        )
    return row


def _idempotent(user, idempotency_key, request, lock_ids, work):
    """
    Run `work(key, request_hash)` under the routine locks unless the key was
    already used; the receipt is stored with the recovery row it created.
    """
    key, request_hash = _key_and_hash(idempotency_key, request)
    with transaction.atomic():
        for routine_id in sorted(set(lock_ids)):
            _lock(user, routine_id)
        receipt = _replay(user, key, request_hash)
        if receipt is not None:
            return receipt
        row, receipt = work(key, request_hash)
        row.receipt = receipt
        row.save(update_fields=['receipt'])
        return receipt


# Trash


def _trash_receipt(row: RoutineRecovery, rev: str) -> dict:
    return {
        'routine_id': row.routine_id,
        'recovery_id': str(row.pk),
        'operation': RoutineRecovery.TRASH,
        'deleted_at': iso(row.created_at),
        'expires_at': iso(row.expires_at),
        'revision': rev,
    }


def _trash(user, routine, key=None, request_hash=None):
    if routine.deleted_at:
        raise RecoveryError(409, 'routine_trashed', 'The routine is already in the trash.')
    before = snapshot(routine)
    now = _now()
    Routine.objects.filter(pk=routine.pk).update(deleted_at=now)
    rev = revision(routine)
    row = _record(user, routine, RoutineRecovery.TRASH, before, rev, key, request_hash, now)
    _invalidate_on_commit(routine)
    return row, _trash_receipt(row, rev)


def trash(user, routine_id, expected_revision, idempotency_key) -> dict:
    def work(key, request_hash):
        routine = Routine.objects.get(pk=routine_id)
        _check_revision(revision(routine), expected_revision)
        return _trash(user, routine, key, request_hash)

    request = {'op': 'trash', 'routine': routine_id, 'expected_revision': expected_revision}
    return _idempotent(user, idempotency_key, request, [routine_id], work)


def legacy_delete(user, routine_id) -> dict:
    """DELETE routine/{id}/: trash without a revision check; repeats return the receipt"""
    with transaction.atomic():
        routine = _lock(user, routine_id)
        if routine.deleted_at:
            row = (
                RoutineRecovery.objects.filter(routine=routine, operation=RoutineRecovery.TRASH)
                .order_by('-created_at')
                .first()
            )
            if row is None or row.receipt is None:
                raise RecoveryError(404, 'not_found', 'Routine not found.')
            return row.receipt
        row, receipt = _trash(user, routine)
        row.receipt = receipt
        row.save(update_fields=['receipt'])
        return receipt


def _refuse_archived_references(user, before, after):
    """
    Refuse removing a day or slot entry that an archived (restorable) workout
    session still points at; its restore would otherwise be impossible
    """
    gone_days = _graph_ids(before)['days'] - _graph_ids(after)['days']
    gone_entries = _graph_ids(before)['entries'] - _graph_ids(after)['entries']
    if not gone_days and not gone_entries:
        return
    for data in WorkoutSessionRecovery.objects.filter(user=user, expires_at__gt=_now()).values_list(
        'snapshot', flat=True
    ):
        days = {data['session'].get('day_id')}
        entries = {log.get('slot_entry_id') for log in data['logs']}
        if {str(i) for i in gone_days} & days or {str(i) for i in gone_entries} & entries:
            raise RecoveryError(
                409,
                'history_referenced',
                'A recoverable deleted workout references this part of the plan.',
            )


# Recorded edits


class EditRecord:
    """Filled in by `recorded_edit` once the edit is recorded"""

    recovery = None
    revision = None


@contextmanager
def recorded_edit(user, routine_id):
    """
    Wrap an in-place planning write: lock the owner's routine, refuse trashed
    ones, snapshot before and record an `edit` recovery if anything changed.
    Deleting rows that history references fails with a 409, writing nothing.
    """
    record = EditRecord()
    with transaction.atomic():
        routine = _lock(user, routine_id)
        if routine.deleted_at:
            raise RecoveryError(409, 'routine_trashed', 'The routine is in the trash.')
        before = snapshot(routine)
        try:
            with transaction.atomic():
                yield record
        except (RestrictedError, ProtectedError):
            raise RecoveryError(
                409,
                'history_referenced',
                'Completed workouts reference this part of the plan; it cannot be deleted.',
            )
        after = snapshot(routine)
        _refuse_archived_references(user, before, after)
        record.revision = revision_of(after)
        if after != before:
            record.recovery = _record(user, routine, RoutineRecovery.EDIT, before, record.revision)
            _invalidate_on_commit(routine)


# Restore


def _graph_ids(data):
    ids = {'labels': set(), 'days': set(), 'slots': set(), 'entries': set()}
    ids.update({key: set() for key in CONFIG_MODELS})
    ids['labels'] = {r['id'] for r in data['labels']}
    for day in data['days']:
        ids['days'].add(day['id'])
        for slot in day['slots']:
            ids['slots'].add(slot['id'])
            for entry in slot['entries']:
                ids['entries'].add(entry['id'])
                for key in CONFIG_MODELS:
                    ids[key] |= {c['id'] for c in entry['configs'][key]}
    return ids


def _upsert(model, row, fields, current_ids, **parent):
    values = {f: row[f] for f in fields}
    values.update(parent)
    if row['id'] in current_ids:
        model.objects.filter(pk=row['id']).update(**values)
    elif model.objects.filter(pk=row['id']).exists():
        raise RecoveryError(409, 'restore_conflict', 'A plan row was reassigned; cannot restore.')
    else:
        model.objects.bulk_create([model(pk=row['id'], **values)])


def apply_snapshot(routine: Routine, data: dict):
    """Make `routine`'s graph equal to `data` by primary key"""
    live = snapshot(routine)
    _refuse_archived_references(routine.user, live, data)
    current = _graph_ids(live)
    target = _graph_ids(data)
    try:
        with transaction.atomic():
            for key, model in CONFIG_MODELS.items():
                model.objects.filter(pk__in=current[key] - target[key]).delete()
            SlotEntry.objects.filter(pk__in=current['entries'] - target['entries']).delete()
            Slot.objects.filter(pk__in=current['slots'] - target['slots']).delete()
            Day.objects.filter(pk__in=current['days'] - target['days']).delete()
            Label.objects.filter(pk__in=current['labels'] - target['labels']).delete()
    except (RestrictedError, ProtectedError):
        raise RecoveryError(
            409,
            'restore_conflict',
            'Workouts were logged against plan parts added since; restoring would remove them.',
        )

    try:
        with transaction.atomic():
            _write_snapshot(routine, data, current)
    except IntegrityError:
        # E.g. two config rows swapped iterations since the snapshot
        raise RecoveryError(409, 'restore_conflict', 'The snapshot conflicts with the current plan.')
    if snapshot(routine) != data:
        raise RecoveryError(409, 'restore_conflict', 'The snapshot could not be restored exactly.')


def _write_snapshot(routine, data, current):
    Routine.objects.filter(pk=routine.pk).update(
        **{f: data['routine'][f] for f in ROUTINE_FIELDS}
    )
    for row in data['labels']:
        _upsert(Label, row, LABEL_FIELDS, current['labels'], routine_id=routine.pk)
    for day in data['days']:
        _upsert(Day, day, DAY_FIELDS, current['days'], routine_id=routine.pk)
        for slot in day['slots']:
            _upsert(Slot, slot, SLOT_FIELDS, current['slots'], day_id=day['id'])
            for entry in slot['entries']:
                _upsert(SlotEntry, entry, ENTRY_FIELDS, current['entries'], slot_id=slot['id'])
                for key, model in CONFIG_MODELS.items():
                    for config in entry['configs'][key]:
                        _upsert(model, config, CONFIG_FIELDS, current[key], slot_entry_id=entry['id'])


def _get_recovery(user, recovery_id, for_update=False) -> RoutineRecovery:
    qs = RoutineRecovery.objects.filter(user=user)
    if for_update:
        qs = qs.select_for_update()
    try:
        row = qs.filter(pk=recovery_id).first()
    except ValidationError:
        row = None
    if row is None:
        raise RecoveryError(404, 'not_found', 'Recovery not found.')
    return row


def restore(user, recovery_id, expected_revision, idempotency_key) -> dict:
    row = _get_recovery(user, recovery_id)
    affected = row.replacement_id or row.routine_id

    def work(key, request_hash):
        target = _get_recovery(user, recovery_id, for_update=True)
        if _now() >= target.expires_at:
            raise RecoveryError(
                410, 'recovery_expired', 'The undo window has ended.', expires_at=iso(target.expires_at)
            )
        if target.operation == RoutineRecovery.RESTORE or target.restored_at:
            raise RecoveryError(409, 'restore_conflict', 'This recovery is not restorable.')
        routine = Routine.objects.get(pk=target.routine_id)
        current = revision(Routine.objects.get(pk=affected))
        _check_revision(current, expected_revision)
        if current != target.revision_after:
            raise RecoveryError(
                409,
                'restore_conflict',
                'The plan changed after this operation; undo the newer change first.',
                current_revision=current,
            )
        before = snapshot(routine)
        apply_snapshot(routine, target.snapshot)
        now = _now()
        if target.replacement_id:
            Routine.objects.filter(pk=target.replacement_id).update(deleted_at=now)
        target.restored_at = now
        target.save(update_fields=['restored_at'])
        rev = revision(routine)
        new = _record(
            user,
            routine,
            RoutineRecovery.RESTORE,
            before,
            rev,
            key,
            request_hash,
            replacement_id=target.replacement_id,
        )
        _invalidate_on_commit(*Routine.objects.filter(pk__in={routine.pk, affected}))
        return new, {
            'routine_id': routine.pk,
            'revision': rev,
            'restored_from': str(target.pk),
            'recovery_id': str(new.pk),
            'expires_at': iso(new.expires_at),
        }

    request = {'op': 'restore', 'recovery': str(row.pk), 'expected_revision': expected_revision}
    return _idempotent(user, idempotency_key, request, [row.routine_id, affected], work)


def recoveries(user, routine_id=None):
    """Unexpired recoveries, newest first; purges the owner's expired rows"""
    now = _now()
    RoutineRecovery.objects.filter(user=user, expires_at__lte=now).delete()
    qs = RoutineRecovery.objects.filter(user=user).select_related('routine').order_by('-created_at')
    if routine_id is not None:
        qs = qs.filter(routine_id=routine_id) | qs.filter(replacement_id=routine_id)
    return qs


def recovery_payload(row: RoutineRecovery) -> dict:
    return {
        'recovery_id': str(row.pk),
        'routine_id': row.routine_id,
        'routine_name': row.routine.name,
        'operation': row.operation,
        'created_at': iso(row.created_at),
        'expires_at': iso(row.expires_at),
        'restorable': row.operation != RoutineRecovery.RESTORE
        and row.restored_at is None
        and _now() < row.expires_at,
        'restored_at': iso(row.restored_at),
        'replacement_routine_id': row.replacement_id,
    }


# Rebuild


_ROUTINE_KEYS = {'name', 'description', 'start', 'end', 'fit_in_week'}
_LABEL_KEYS = set(LABEL_FIELDS)
_DAY_KEYS = {*DAY_FIELDS, 'slots'}
_SLOT_KEYS = {*SLOT_FIELDS, 'entries'}
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
_CONFIG_KEYS = set(CONFIG_FIELDS)


def _object(value, allowed, path, errors, required=()):
    if not isinstance(value, dict):
        errors.append({'path': path, 'error': 'must be an object'})
        return None
    for key in sorted(set(value) - allowed):
        errors.append({'path': f'{path}.{key}', 'error': 'unsupported field'})
    for key in required:
        if key not in value:
            errors.append({'path': f'{path}.{key}', 'error': 'required'})
    return {k: v for k, v in value.items() if k in allowed}


def _list(value, path, errors):
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append({'path': path, 'error': 'must be a list'})
        return []
    return value


def _clean(instance, path, errors, given):
    """Validate the fields the plan gave; omitted ones keep their model defaults"""
    given = {f.removesuffix('_id') for f in given}
    exclude = [f.name for f in instance._meta.concrete_fields if f.name not in given]
    try:
        instance.full_clean(exclude=exclude, validate_unique=False, validate_constraints=False)
    except ValidationError as e:
        for field, messages in e.message_dict.items():
            errors.append({'path': f'{path}.{field}', 'error': ' '.join(messages)})


def _build(user, replacement):
    """Validate a v1 replacement plan; (unsaved graph, errors). Nothing is written."""
    # Local
    from wger.manager.api.serializers import RoutineSerializer

    errors = []
    plan = _object(replacement, {'version', 'routine', 'labels', 'days'}, 'replacement', errors, ('version', 'routine'))
    if plan is None:
        return None, errors
    if plan.get('version') != REPLACEMENT_VERSION:
        errors.append({'path': 'replacement.version', 'error': f'must be {REPLACEMENT_VERSION}'})
        return None, errors

    graph = {'labels': [], 'days': []}
    meta = _object(plan.get('routine'), _ROUTINE_KEYS, 'routine', errors, ('name', 'start', 'end'))
    if meta is not None:
        s = RoutineSerializer(data={k: meta[k] for k in _ROUTINE_KEYS if k in meta})
        if s.is_valid():
            graph['routine'] = Routine(user=user, **s.validated_data)
        else:
            errors += [{'path': f'routine.{k}', 'error': ' '.join(map(str, v))} for k, v in s.errors.items()]

    for i, raw in enumerate(_list(plan.get('labels'), 'labels', errors)):
        path = f'labels[{i}]'
        if (raw := _object(raw, _LABEL_KEYS, path, errors, ('label',))) is not None:
            label = Label(**raw)
            _clean(label, path, errors, raw)
            graph['labels'].append(label)

    for d, raw_day in enumerate(_list(plan.get('days'), 'days', errors)):
        path = f'days[{d}]'
        if (raw_day := _object(raw_day, _DAY_KEYS, path, errors, ('name',))) is None:
            continue
        day = Day(**{k: v for k, v in raw_day.items() if k != 'slots'})
        _clean(day, path, errors, raw_day)
        slots = []
        for s_i, raw_slot in enumerate(_list(raw_day.get('slots'), f'{path}.slots', errors)):
            s_path = f'{path}.slots[{s_i}]'
            if (raw_slot := _object(raw_slot, _SLOT_KEYS, s_path, errors)) is None:
                continue
            slot = Slot(**{k: v for k, v in raw_slot.items() if k != 'entries'})
            _clean(slot, s_path, errors, raw_slot)
            entries = []
            for e_i, raw_entry in enumerate(_list(raw_slot.get('entries'), f'{s_path}.entries', errors)):
                e_path = f'{s_path}.entries[{e_i}]'
                if (raw_entry := _object(raw_entry, _ENTRY_KEYS, e_path, errors, ('exercise',))) is None:
                    continue
                fields = {k: v for k, v in raw_entry.items() if k != 'configs'}
                for fk in ('exercise', 'repetition_unit', 'weight_unit'):
                    if fk in fields:
                        fields[f'{fk}_id'] = fields.pop(fk)
                entry = SlotEntry(**fields)
                _clean(entry, e_path, errors, fields)
                if entry.class_name and not importlib.util.find_spec(
                    f'wger.manager.config_calculations.{entry.class_name}'
                ):
                    errors.append({'path': f'{e_path}.class_name', 'error': 'unknown calculation class'})
                configs = {}
                raw_configs = raw_entry.get('configs') or {}
                if _object(raw_configs, set(CONFIG_MODELS), f'{e_path}.configs', errors) is None:
                    raw_configs = {}
                for key, model in CONFIG_MODELS.items():
                    configs[key] = []
                    for c_i, raw_c in enumerate(_list(raw_configs.get(key), f'{e_path}.configs.{key}', errors)):
                        c_path = f'{e_path}.configs.{key}[{c_i}]'
                        if (raw_c := _object(raw_c, _CONFIG_KEYS, c_path, errors, ('iteration', 'value'))) is None:
                            continue
                        config = model(**raw_c)
                        _clean(config, c_path, errors, raw_c)
                        try:
                            validate_requirements(config.requirements)
                        except serializers.ValidationError as e:
                            errors.append({'path': f'{c_path}.requirements', 'error': ' '.join(map(str, e.detail))})
                        configs[key].append(config)
                entries.append((entry, configs))
            slots.append((slot, entries))
        graph['days'].append((day, slots))
    return graph, errors


def _counts(data):
    days = data['days']
    slots = [s for d in days for s in d['slots']]
    entries = [e for s in slots for e in s['entries']]
    configs = sum(len(c) for e in entries for c in e['configs'].values())
    return {
        'labels': len(data['labels']),
        'days': len(days),
        'slots': len(slots),
        'entries': len(entries),
        'configs': configs,
    }


def _replacement_counts(graph):
    slots = [s for _, ss in graph['days'] for s in ss]
    entries = [e for _, es in slots for e in es]
    return {
        'labels': len(graph['labels']),
        'days': len(graph['days']),
        'slots': len(slots),
        'entries': len(entries),
        'configs': sum(len(c) for _, cs in entries for c in cs.values()),
    }


def preview(user, routine_id, expected_revision, replacement) -> dict:
    """Validate a rebuild; read-only"""
    routine = Routine.objects.filter(pk=routine_id, user=user).first()
    if routine is None:
        raise RecoveryError(404, 'not_found', 'Routine not found.')
    if routine.deleted_at:
        raise RecoveryError(409, 'routine_trashed', 'The routine is in the trash.')
    current = snapshot(routine)
    _check_revision(revision_of(current), expected_revision)
    graph, errors = _build(user, replacement)
    if errors:
        return {'ok': False, 'plan_hash': None, 'diff': None, 'errors': errors, 'history_effect': 'none'}
    old = current['routine']
    new = graph['routine']
    diff = {
        'routine': {
            f: {'from': old[f], 'to': json.loads(_canonical(getattr(new, f)))}
            for f in _ROUTINE_KEYS
            if json.loads(_canonical(getattr(new, f))) != old[f]
        },
        'before': _counts(current),
        'after': _replacement_counts(graph),
    }
    plan_hash = _sha({'revision': expected_revision, 'replacement': replacement})
    return {'ok': True, 'plan_hash': plan_hash, 'diff': diff, 'errors': [], 'history_effect': 'none'}


def _save_graph(routine, graph):
    for label in graph['labels']:
        label.routine = routine
        label.save()
    for day, slots in graph['days']:
        day.routine = routine
        Day.objects.bulk_create([day])
        for slot, entries in slots:
            slot.day = day
            Slot.objects.bulk_create([slot])
            for entry, configs in entries:
                entry.slot = slot
                SlotEntry.objects.bulk_create([entry])
                for key, model in CONFIG_MODELS.items():
                    for config in configs[key]:
                        config.slot_entry = entry
                    model.objects.bulk_create(configs[key])


def rebuild(user, routine_id, expected_revision, replacement, plan_hash, idempotency_key) -> dict:
    def work(key, request_hash):
        result = preview(user, routine_id, expected_revision, replacement)
        if not result['ok']:
            raise RecoveryError(400, 'invalid', 'The replacement plan is invalid.', errors=result['errors'])
        if result['plan_hash'] != plan_hash:
            raise RecoveryError(409, 'plan_changed', 'The plan hash does not match the preview.')
        old = Routine.objects.get(pk=routine_id)
        before = snapshot(old)
        graph, _ = _build(user, replacement)
        new = graph['routine']
        new.is_template, new.is_public = old.is_template, old.is_public
        new.save()
        _save_graph(new, graph)
        Routine.objects.filter(pk=old.pk).update(deleted_at=_now(), replaced_by=new)
        rev = revision(new)
        row = _record(
            user, old, RoutineRecovery.REBUILD, before, rev, key, request_hash, replacement=new
        )
        _invalidate_on_commit(old, new)
        return row, {
            'routine_id': old.pk,
            'replacement_routine_id': new.pk,
            'revision': rev,
            'previous_recovery_id': str(row.pk),
            'expires_at': iso(row.expires_at),
        }

    request = {
        'op': 'rebuild',
        'routine': routine_id,
        'expected_revision': expected_revision,
        'plan_hash': plan_hash,
        'replacement': replacement,
    }
    return _idempotent(user, idempotency_key, request, [routine_id], work)
