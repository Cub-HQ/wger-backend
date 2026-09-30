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

# Standard Library
from contextlib import ExitStack
from functools import partial

# Django
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

# Third Party
from drf_spectacular.utils import (
    OpenApiTypes,
    extend_schema,
)
from rest_framework import (
    status,
    viewsets,
)
from rest_framework.decorators import action
from rest_framework.exceptions import (
    NotFound,
    ValidationError,
)
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

# wger
from wger.manager import (
    history_integrity,
    routine_recovery,
    spreadsheet,
)
from wger.manager.api.consts import BASE_CONFIG_FILTER_FIELDS
from wger.manager.api.filtersets import (
    WorkoutLogFilterSet,
    WorkoutSessionFilterSet,
)
from wger.manager.api.permissions import RoutinePermission
from wger.manager.api.serializers import (
    DaySerializer,
    LogDisplaySerializer,
    LogStatsDataSerializer,
    MaxRepetitionsConfigSerializer,
    MaxRestConfigSerializer,
    MaxRiRConfigSerializer,
    MaxSetNrConfigSerializer,
    MaxWeightConfigSerializer,
    RepetitionsConfigSerializer,
    RestConfigSerializer,
    RiRConfigSerializer,
    RoutineSerializer,
    RoutineStructureSerializer,
    SetNrConfigSerializer,
    SlotEntrySerializer,
    SlotSerializer,
    WeightConfigSerializer,
    WorkoutDayDataDisplayModeSerializer,
    WorkoutDayDataGymModeSerializer,
    WorkoutLogSerializer,
    WorkoutSessionSerializer,
)
from wger.manager.models import (
    Day,
    MaxRepetitionsConfig,
    MaxRestConfig,
    MaxRiRConfig,
    MaxSetsConfig,
    MaxWeightConfig,
    RepetitionsConfig,
    RestConfig,
    RiRConfig,
    Routine,
    SetsConfig,
    Slot,
    SlotEntry,
    WeightConfig,
    WorkoutLog,
    WorkoutSession,
)
from wger.utils.cache import CacheKeyMapper
from wger.utils.viewsets import WgerOwnerObjectModelViewSet


def request_user_or_trainer_q(request):
    """
    Helper function to build a Q object for filtering objects by user or trainer.
    """
    trainer_identity_pk = request.session.get('trainer.identity', None)
    if trainer_identity_pk:
        return Q(user=request.user) | Q(user_id=trainer_identity_pk)
    return Q(user=request.user)


def _recorded(request, routine_ids, write):
    """
    Run `write()` as a recorded planning edit of every routine it touches and
    add the recovery headers to its response
    """
    with ExitStack() as stack:
        records = [
            stack.enter_context(routine_recovery.recorded_edit(request.user, routine_id))
            for routine_id in sorted(routine_ids)
        ]
        response = write()
    record = next((r for r in records if r.recovery), records[0])
    response['X-Routine-Revision'] = record.revision
    if record.recovery:
        response['X-Routine-Recovery-Id'] = str(record.recovery.pk)
        response['X-Routine-Recovery-Expires-At'] = routine_recovery.iso(record.recovery.expires_at)
    return response


class RecoveryErrorMixin:
    """Answer refused recovery operations with their `{detail, code}` body"""

    def handle_exception(self, exc):
        if isinstance(exc, routine_recovery.RecoveryError):
            return Response(exc.body(), status=exc.status)
        return super().handle_exception(exc)


class RecordedPlanEditMixin(RecoveryErrorMixin):
    """Writes to a routine's plan children record an undoable `edit` recovery"""

    def _routine_ids(self, request, instance=None):
        ids = set()
        if instance is not None:
            ids.add(routine_recovery.routine_id_of(instance))
        model, field = self.get_owner_objects()[0]
        pk = request.data.get(field) if isinstance(request.data, dict) else None
        try:
            parent = model.objects.filter(pk=pk).first() if pk is not None else None
        except (ValueError, TypeError, RecoveryValidationError):
            parent = None
        if parent is not None:
            ids.add(routine_recovery.routine_id_of(parent))
        return ids

    def create(self, request, *args, **kwargs):
        self._check_owner_permission(request)
        ids = self._routine_ids(request)
        write = partial(super().create, request, *args, **kwargs)
        # No valid parent: the serializer rejects it and nothing is written
        return _recorded(request, ids, write) if ids else write()

    def update(self, request, *args, **kwargs):
        self._check_owner_permission(request)
        ids = self._routine_ids(request, self.get_object())
        return _recorded(request, ids, partial(super().update, request, *args, **kwargs))

    def destroy(self, request, *args, **kwargs):
        ids = {routine_recovery.routine_id_of(self.get_object())}
        return _recorded(request, ids, partial(super().destroy, request, *args, **kwargs))


class RoutineViewSet(RecoveryErrorMixin, viewsets.ModelViewSet):
    """
    API endpoint for routine objects
    """

    serializer_class = RoutineSerializer
    permission_classes = [RoutinePermission]
    ordering_fields = '__all__'
    filterset_fields = (
        'name',
        'description',
        'created',
        'start',
        'end',
        'is_public',
        'is_template',
    )

    def get_queryset(self):
        """
        Only allow access to appropriate objects

        Trashed routines are hidden from lists (the owner's are listed with
        ?trashed=true) and from everybody but the owner.
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return Routine.objects.none()

        qs = Routine.objects.filter(
            request_user_or_trainer_q(request=self.request)
            | Q(is_public=True, deleted_at__isnull=True)
        )
        if self.action != 'list':
            return qs
        if self.request.query_params.get('trashed') == 'true':
            return qs.filter(user=self.request.user, deleted_at__isnull=False)
        return qs.filter(deleted_at__isnull=True)

    def perform_create(self, serializer):
        """
        Set the owner
        """
        serializer.save(user=self.request.user)

    def update(self, request, *args, **kwargs):
        routine = self.get_object()
        return _recorded(request, [routine.pk], partial(super().update, request, *args, **kwargs))

    def destroy(self, request, *args, **kwargs):
        """Move the routine to the trash (undoable for 14 days)"""
        routine = self.get_object()
        if routine.user_id != request.user.pk:
            raise NotFound()
        return Response(routine_recovery.legacy_delete(request.user, routine.pk))

    def _owned(self, pk) -> Routine:
        routine = Routine.objects.filter(pk=pk, user=self.request.user).first()
        if routine is None:
            raise NotFound()
        return routine

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, url_path='revision', pagination_class=None)
    def revision(self, request, pk):
        """Current plan revision, for the stale checks of trash/restore/rebuild"""
        routine = self._owned(pk)
        return Response(
            {
                'routine_id': routine.pk,
                'revision': routine_recovery.revision(routine),
                'deleted_at': routine_recovery.iso(routine.deleted_at),
                'replaced_by': routine.replaced_by_id,
            }
        )

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=['post'], url_path='trash', pagination_class=None)
    def trash(self, request, pk):
        """Move the routine to the trash; restorable for 14 days"""
        routine = self._owned(pk)
        return Response(
            routine_recovery.trash(
                request.user,
                routine.pk,
                request.data.get('expected_revision'),
                request.data.get('idempotency_key'),
            )
        )

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=['post'], url_path='rebuild-preview', pagination_class=None)
    def rebuild_preview(self, request, pk):
        """Validate a replacement plan and show the change; nothing is written"""
        return Response(
            routine_recovery.preview(
                request.user,
                self._owned(pk).pk,
                request.data.get('expected_revision'),
                request.data.get('replacement'),
            )
        )

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=['post'], url_path='rebuild', pagination_class=None)
    def rebuild(self, request, pk):
        """Replace the plan with a new linked routine; the old one stays intact and restorable"""
        return Response(
            routine_recovery.rebuild(
                request.user,
                self._owned(pk).pk,
                request.data.get('expected_revision'),
                request.data.get('replacement'),
                request.data.get('plan_hash'),
                request.data.get('idempotency_key'),
            )
        )

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    @action(detail=False, url_path='recoveries')
    def recoveries(self, request):
        """The owner's unexpired trash, edit, rebuild and restore records"""
        routine_id = request.query_params.get('routine')
        if routine_id is not None and not routine_id.isdigit():
            raise ValidationError({'routine': 'Must be a routine id.'})
        qs = routine_recovery.recoveries(request.user, routine_id and int(routine_id))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response([routine_recovery.recovery_payload(r) for r in page])

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(
        detail=False,
        methods=['post'],
        url_path=r'recoveries/(?P<recovery_id>[^/.]+)/restore',
        pagination_class=None,
    )
    def restore(self, request, recovery_id):
        """Undo a recorded operation (last-in-first-out)"""
        return Response(
            routine_recovery.restore(
                request.user,
                recovery_id,
                request.data.get('expected_revision'),
                request.data.get('idempotency_key'),
            )
        )

    @extend_schema(responses={200: WorkoutDayDataDisplayModeSerializer(many=True)})
    @action(detail=True, url_path='date-sequence-display', pagination_class=None)
    def date_sequence_display_mode(self, request, pk):
        """
        Return the day sequence of the routine
        """
        cache_key = CacheKeyMapper.routine_api_date_sequence_display_key(pk, request.user.id)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        out = WorkoutDayDataDisplayModeSerializer(
            self.get_object().date_sequence,
            many=True,
        ).data
        cache.set(cache_key, out, settings.WGER_SETTINGS['ROUTINE_CACHE_TTL'])

        return Response(out)

    @extend_schema(responses={200: WorkoutDayDataGymModeSerializer(many=True)})
    @action(detail=True, url_path='date-sequence-gym', pagination_class=None)
    def date_sequence_gym_mode(self, request, pk):
        """
        Return the day sequence of the routine
        """
        cache_key = CacheKeyMapper.routine_api_date_sequence_gym_key(pk, request.user.id)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        out = WorkoutDayDataGymModeSerializer(self.get_object().date_sequence, many=True).data
        cache.set(cache_key, out, settings.WGER_SETTINGS['ROUTINE_CACHE_TTL'])

        return Response(out)

    @extend_schema(responses={200: RoutineStructureSerializer})
    @action(detail=True)
    def structure(self, request, pk):
        """
        Return the full object structure of the routine.
        """
        cache_key = CacheKeyMapper.routine_api_structure_key(pk, request.user.id)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        out = RoutineStructureSerializer(self.get_object()).data
        cache.set(cache_key, out, settings.WGER_SETTINGS['ROUTINE_CACHE_TTL'])
        return Response(out)

    @extend_schema(responses={200: LogDisplaySerializer(many=True)})
    @action(detail=True, url_path='logs', pagination_class=None)
    def logs(self, request, pk):
        """
        Returns the logs for the routine
        """
        cache_key = CacheKeyMapper.routine_api_logs(pk, request.user.id)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        out = LogDisplaySerializer(self.get_object().logs_display(), many=True).data
        cache.set(cache_key, out, settings.WGER_SETTINGS['ROUTINE_CACHE_TTL'])
        return Response(out)

    @extend_schema(responses={200: LogStatsDataSerializer})
    @action(detail=True, url_path='stats')
    def stats(self, request, pk):
        """
        Returns the logs for the routine
        """
        cache_key = CacheKeyMapper.routine_api_stats(pk, request.user.id)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        out = LogStatsDataSerializer(self.get_object().calculate_log_statistics()).data
        cache.set(cache_key, out, settings.WGER_SETTINGS['ROUTINE_CACHE_TTL'])

        return Response(out)

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    @action(detail=True, pagination_class=None)
    def export(self, request, pk):
        """
        Download the planned structure as ?file=csv or ?file=xlsx

        Same access as `structure`. Logged workouts are never part of the file.
        """
        routine = self.get_object()
        kind = _file_kind(request)
        return spreadsheet.download(
            spreadsheet.export_rows(routine), kind, f'routine-{routine.pk}', with_help=True
        )

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    @action(detail=False, url_path='import-template', pagination_class=None)
    def import_template(self, request):
        """Download an empty import template as ?file=csv or ?file=xlsx"""
        kind = _file_kind(request)
        return spreadsheet.download([], kind, 'routine-template', with_help=True)

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(
        detail=False,
        methods=['post'],
        url_path='import-preview',
        parser_classes=[MultiPartParser],
        pagination_class=None,
    )
    def import_preview(self, request):
        """
        Check an uploaded plan and show what confirming it would change

        Nothing is written. Send the returned plan_hash to import-confirm.
        """
        mode, target, drop = _import_arguments(request)
        plan = spreadsheet.build_plan(request.FILES.get('file'), request.user, mode, target, drop)
        return Response(plan.preview())

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    @action(
        detail=False,
        methods=['post'],
        url_path='import-confirm',
        parser_classes=[MultiPartParser],
        pagination_class=None,
    )
    def import_confirm(self, request):
        """
        Apply a previewed plan in one transaction

        The plan is rebuilt under the owner lock. If it doesn't match the
        previewed plan_hash (file, exercise matches, options or the routine
        changed, or it was already applied) nothing is written and 409 is
        returned.
        """
        mode, target, drop = _import_arguments(request)
        plan_hash = request.data.get('plan_hash', '')
        with transaction.atomic():
            get_user_model()._default_manager.select_for_update().get(pk=request.user.pk)
            if target is not None:
                target = Routine.objects.select_for_update().get(pk=target.pk)

            upload = request.FILES.get('file')
            plan = spreadsheet.build_plan(upload, request.user, mode, target, drop)
            if not plan.ok:
                return Response(plan.preview(), status=status.HTTP_400_BAD_REQUEST)
            if plan.hash != plan_hash:
                return Response(
                    {
                        **plan.preview(),
                        'detail': 'The file or routine changed since the preview, preview again.',
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if target is None:
                routine = spreadsheet.apply_plan(plan, request.user)
                return Response(
                    {'id': routine.pk, 'plan_hash': plan.hash}, status=status.HTTP_201_CREATED
                )
            with routine_recovery.recorded_edit(request.user, target.pk) as record:
                routine = spreadsheet.apply_plan(plan, request.user)

        body = {'id': routine.pk, 'plan_hash': plan.hash, 'revision': record.revision}
        response = Response(body)
        response['X-Routine-Revision'] = record.revision
        if record.recovery:
            body['recovery_id'] = str(record.recovery.pk)
            body['expires_at'] = routine_recovery.iso(record.recovery.expires_at)
            response['X-Routine-Recovery-Id'] = body['recovery_id']
            response['X-Routine-Recovery-Expires-At'] = body['expires_at']
        return response

    @staticmethod
    def get_owner_objects():
        return []


def _file_kind(request) -> str:
    kind = request.query_params.get('file', 'csv')
    if kind not in ('csv', 'xlsx'):
        raise ValidationError({'file': 'Must be csv or xlsx.'})
    return kind


def _import_arguments(request):
    """
    mode, owned target routine and drop_unsupported of an import request

    Only the owner's own, non-template routines can be updated; no trainer
    identity and no public templates.
    """
    mode = request.data.get('mode')
    if mode not in ('create', 'update'):
        raise ValidationError({'mode': 'Must be create or update.'})
    drop = request.data.get('drop_unsupported', '').lower() in ('true', '1', 'yes', 'on')

    target = None
    if mode == 'update':
        try:
            pk = int(request.data.get('routine', ''))
        except ValueError:
            raise ValidationError({'routine': 'Required for update.'})
        target = Routine.objects.filter(
            pk=pk, user=request.user, is_template=False, deleted_at__isnull=True
        ).first()
        if target is None:
            raise NotFound()
    return mode, target, drop


class UserRoutineTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for routine template objects
    """

    serializer_class = RoutineSerializer
    permission_classes = [RoutinePermission]
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = ('name', 'description', 'created')

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return Routine.objects.none()

        # If the current user is a trainer, also return their templates.
        return Routine.templates.filter(
            request_user_or_trainer_q(request=self.request), deleted_at__isnull=True
        )


class PublicRoutineTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for public routine templates objects
    """

    serializer_class = RoutineSerializer
    permission_classes = [RoutinePermission]
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = ('name', 'description', 'created')

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        return Routine.public.filter(deleted_at__isnull=True)


# Django
from django.core.exceptions import ValidationError as RecoveryValidationError
from django.utils import timezone as recovery_timezone

# Third Party
from rest_framework.exceptions import NotFound as RecoveryNotFound

# wger
from wger.manager.models.session_recovery import (
    WorkoutSessionRecovery,
    archive_session,
    purge_expired_recoveries,
    recovery_summary,
    restore_session,
)


class WorkoutSessionViewSet(WgerOwnerObjectModelViewSet):
    """
    API endpoint for workout sessions objects
    """

    serializer_class = WorkoutSessionSerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_class = WorkoutSessionFilterSet

    def destroy(self, request, *args, **kwargs):
        try:
            row = archive_session(request.user, kwargs['pk'])
        except (RecoveryValidationError, ValueError):
            raise RecoveryNotFound()
        return Response(recovery_summary(row))

    @action(detail=False, methods=['get'], url_path='recoveries')
    def recoveries(self, request):
        purge_expired_recoveries(user=request.user)
        rows = WorkoutSessionRecovery.objects.filter(
            user=request.user, expires_at__gt=recovery_timezone.now()
        ).order_by('-deleted_at', 'pk')
        if 'routine' in request.query_params:
            try:
                routine_id = int(request.query_params['routine'])
            except (TypeError, ValueError):
                raise RecoveryNotFound()
            rows = rows.filter(routine_id=routine_id)
        summaries = rows.values(
            'id', 'original_session_id', 'routine_id', 'deleted_at', 'expires_at',
            'snapshot__session__datetime_start',
        )
        return Response([{
            'id': str(row['id']),
            'original_session_id': str(row['original_session_id']),
            'routine_id': row['routine_id'],
            'deleted_at': row['deleted_at'].isoformat(),
            'expires_at': row['expires_at'].isoformat(),
            'datetime_start': row['snapshot__session__datetime_start'],
        } for row in summaries])

    @action(detail=False, methods=['post'], url_path=r'recoveries/(?P<recovery_id>[^/.]+)/restore')
    def restore_recovery(self, request, recovery_id=None):
        try:
            session = restore_session(request.user, recovery_id)
        except (RecoveryValidationError, ValueError):
            raise RecoveryNotFound()
        return Response(self.get_serializer(session).data)

    @action(detail=False, methods=['get'], url_path='integrity')
    def integrity(self, request):
        """
        Counts and digests of the caller's own sessions and logs; see history_integrity
        """
        response = Response(history_integrity.fingerprint(request.user.pk))
        response['Cache-Control'] = 'no-store'
        return response


    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """

        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return WorkoutSession.objects.none()

        return WorkoutSession.objects.filter(user=self.request.user)

    # def create(self, request, *args, **kwargs):
    #     super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """
        Set the owner
        """
        serializer.save(user=self.request.user)

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [(Routine, 'routine'), (Day, 'day')]


class WorkoutLogViewSet(WgerOwnerObjectModelViewSet):
    """
    API endpoint for workout log objects
    """

    serializer_class = WorkoutLogSerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_class = WorkoutLogFilterSet

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return WorkoutLog.objects.none()

        return WorkoutLog.objects.filter(user=self.request.user)

    def perform_create(self, serializer: WorkoutLogSerializer):
        """
        Set the owner
        """
        serializer.save(user=self.request.user)

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [
            (Routine, 'routine'),
            (WorkoutSession, 'session'),
            (SlotEntry, 'slot_entry'),
            (WorkoutLog, 'next_log'),
        ]


class RoutineDayViewSet(RecordedPlanEditMixin, WgerOwnerObjectModelViewSet):
    """
    API endpoint for routine day objects
    """

    serializer_class = DaySerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = (
        'id',
        'routine',
        'order',
        'name',
        'description',
        'is_rest',
        'need_logs_to_advance',
    )

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return Day.objects.none()

        return Day.objects.filter(routine__user=self.request.user)

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [(Routine, 'routine')]


class SlotViewSet(RecordedPlanEditMixin, WgerOwnerObjectModelViewSet):
    """
    API endpoint for routine slot objects
    """

    serializer_class = SlotSerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = (
        'day',
        'order',
        'comment',
    )

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return Slot.objects.none()

        return Slot.objects.filter(day__routine__user=self.request.user)

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [(Day, 'day')]


class SlotEntryViewSet(RecordedPlanEditMixin, WgerOwnerObjectModelViewSet):
    """
    API endpoint for routine slot entry objects
    """

    serializer_class = SlotEntrySerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = (
        'slot',
        'exercise',
        'type',
        'repetition_unit',
        'repetition_rounding',
        'weight_unit',
        'weight_rounding',
        'order',
        'comment',
    )

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return SlotEntry.objects.none()

        return SlotEntry.objects.filter(slot__day__routine__user=self.request.user)

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [(Slot, 'slot')]


class AbstractConfigViewSet(RecordedPlanEditMixin, WgerOwnerObjectModelViewSet):
    """
    API endpoint for weight config objects
    """

    is_private = True
    ordering_fields = '__all__'
    filterset_fields = BASE_CONFIG_FILTER_FIELDS

    @staticmethod
    def get_owner_objects():
        """
        Return objects to check for ownership permission
        """
        return [(SlotEntry, 'slot_entry')]


class WeightConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for weight config objects
    """

    serializer_class = WeightConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return WeightConfig.objects.none()

        return WeightConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class MaxWeightConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for max weight config objects
    """

    serializer_class = MaxWeightConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return MaxWeightConfig.objects.none()

        return MaxWeightConfig.objects.filter(
            slot_entry__slot__day__routine__user=self.request.user
        )


class RepetitionsConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for reps config objects
    """

    serializer_class = RepetitionsConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return RepetitionsConfig.objects.none()

        return RepetitionsConfig.objects.filter(
            slot_entry__slot__day__routine__user=self.request.user
        )


class MaxRepetitionsConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for max reps config objects
    """

    serializer_class = MaxRepetitionsConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return MaxRepetitionsConfig.objects.none()

        return MaxRepetitionsConfig.objects.filter(
            slot_entry__slot__day__routine__user=self.request.user
        )


class SetsConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for set config objects
    """

    serializer_class = SetNrConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return SetsConfig.objects.none()

        return SetsConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class MaxSetsConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for max set config objects
    """

    serializer_class = MaxSetNrConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return MaxSetsConfig.objects.none()

        return MaxSetsConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class RestConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for set config objects
    """

    serializer_class = RestConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return RestConfig.objects.none()

        return RestConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class MaxRestConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for max rest config objects
    """

    serializer_class = MaxRestConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return MaxRestConfig.objects.none()

        return MaxRestConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class RiRConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for set config objects
    """

    serializer_class = RiRConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return RiRConfig.objects.none()

        return RiRConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)


class MaxRiRConfigViewSet(AbstractConfigViewSet):
    """
    API endpoint for set config objects
    """

    serializer_class = MaxRiRConfigSerializer

    def get_queryset(self):
        """
        Only allow access to appropriate objects
        """
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return MaxRiRConfig.objects.none()

        return MaxRiRConfig.objects.filter(slot_entry__slot__day__routine__user=self.request.user)
