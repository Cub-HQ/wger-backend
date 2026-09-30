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

"""Owner-private routine preview API, see ``wger.manager.routine_preview``"""

# Standard Library
import json

# Django
from django.core.exceptions import RequestDataTooBig

# Third Party
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# wger
from wger.manager import routine_preview


def _error(error: routine_preview.PreviewError) -> Response:
    return Response(error.body(), status=error.status)


class _PrivateView(APIView):
    permission_classes = (IsAuthenticated,)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        routine_preview.refuse_trainer(request)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response


class RoutinePreviewCreateView(_PrivateView):
    """
    Create (or replay) a private preview of a proposed routine

    Writes only the preview record: no routine, day, slot, entry, config,
    session or log is created.
    """

    def post(self, request):
        # Size is checked on the raw body, before anything is parsed
        try:
            raw = request.body
        except RequestDataTooBig:
            raw = None
        if raw is None or len(raw) > routine_preview.MAX_BODY_BYTES:
            return Response(
                {
                    'detail': f'The body is larger than {routine_preview.MAX_BODY_BYTES} bytes.',
                    'code': 'payload_too_large',
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return Response(
                {'detail': 'The body must be JSON.', 'code': 'invalid_request'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            preview, replayed = routine_preview.create(request.user, body)
        except routine_preview.PreviewError as e:
            return _error(e)
        return Response(
            {**routine_preview.payload(request, preview), 'replayed': replayed},
            status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
        )


class RoutinePreviewDetailView(_PrivateView):
    """The owner's immutable preview snapshot"""

    def get(self, request, preview_id):
        try:
            preview = routine_preview.owned(request, preview_id)
        except routine_preview.PreviewError as e:
            return _error(e)
        return Response({**routine_preview.payload(request, preview), 'replayed': False})
