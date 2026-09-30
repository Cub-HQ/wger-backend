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

# Django
from django.http import HttpResponseGone

# wger
from wger.core.views.react import ReactView
from wger.manager import routine_preview


class RoutinePreviewView(ReactView):
    """
    The React shell of a private preview, for its owner only

    The page reads the snapshot from the API; the same owner and expiry checks
    run here so that another user, or an expired link, gets no shell at all.
    """

    login_required = True

    def get(self, request, *args, **kwargs):
        try:
            routine_preview.owned(request, kwargs['preview_id'])
        except routine_preview.PreviewError as e:
            return HttpResponseGone(e.detail)
        return super().get(request, *args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Robots-Tag'] = 'noindex'
        return response
