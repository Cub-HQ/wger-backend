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

# wger
from wger.manager import routine_recovery
from wger.manager.api.serializers import (
    RoutineSerializer,
    WorkoutLogSerializer,
    WorkoutSessionSerializer,
)
from wger.manager.api.views import (
    RoutineViewSet,
    WorkoutLogViewSet,
    WorkoutSessionViewSet,
)
from wger.manager.models import (
    Routine,
    WorkoutLog,
    WorkoutSession,
)
from wger.utils.powersync import (
    PowerSyncHandler,
    register_handler,
)


@register_handler
class WorkoutLogHandler(PowerSyncHandler):
    """
    Logs reference both a ``Routine`` and a ``WorkoutSession``; the serializer
    consults ``user_id`` from the context when pinning a log to a session.
    """

    model = WorkoutLog
    serializer_class = WorkoutLogSerializer
    viewset_class = WorkoutLogViewSet
    pass_user_id_in_context = True


@register_handler
class WorkoutSessionHandler(PowerSyncHandler):
    model = WorkoutSession
    serializer_class = WorkoutSessionSerializer
    viewset_class = WorkoutSessionViewSet
    # The legacy-triple shim resolves its wall times in the owner's zone
    pass_user_id_in_context = True


@register_handler
class RoutineHandler(PowerSyncHandler):
    """
    Creation goes through REST so the backend can assign the integer PK and
    ``created`` timestamp; only PATCH/DELETE arrive via PowerSync.
    """

    model = Routine
    serializer_class = RoutineSerializer
    viewset_class = RoutineViewSet
    supports_create = False

    def handle_update(self, payload, user_id):
        """A recorded, undoable edit; trashed routines refuse writes"""
        routine = self._get_or_none(payload, user_id)
        if routine is None:
            return super().handle_update(payload, user_id)
        try:
            with routine_recovery.recorded_edit(routine.user, routine.pk):
                error = super().handle_update(payload, user_id)
                if error is not None:
                    return error
        except routine_recovery.RecoveryError as e:
            return {'error': e.code, 'details': e.detail}
        return None

    def handle_delete(self, payload, user_id):
        """Move the routine to the trash (undoable for 14 days)"""
        routine = self._get_or_none(payload, user_id)
        if routine is None:
            return self._ack_missing('delete', payload['id'])
        routine_recovery.legacy_delete(routine.user, routine.pk)
        return None
