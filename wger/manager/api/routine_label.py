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
Owner-only API for routine labels (wger-gym#26)

A label names a span of calendar days of its routine, e.g. "Deload". Offsets
are zero-based days after ``routine.start`` and both ends are inclusive:
``start_offset=28, end_offset=34`` covers days 29 to 35 of the routine. The
last valid offset is ``(routine.end - routine.start).days``. Labels of one
routine never overlap, so every date has at most one label.
"""

# Third Party
from rest_framework import serializers

# wger
from wger.manager import routine_preview
from wger.manager.api.views import RecordedPlanEditMixin
from wger.manager.models import (
    Label,
    Routine,
)
from wger.utils.viewsets import WgerOwnerObjectModelViewSet


class LabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ('id', 'routine', 'start_offset', 'end_offset', 'label', 'comment')

    def validate(self, data):
        routine = data.get('routine') or self.instance.routine
        start = data.get('start_offset', getattr(self.instance, 'start_offset', None))
        end = data.get('end_offset', getattr(self.instance, 'end_offset', None))
        # The model defaults (1, 2) are no sensible span, so both are required
        if start is None or end is None:
            raise serializers.ValidationError(
                {'start_offset': 'start_offset and end_offset are required.'}
            )
        if end < start:
            raise serializers.ValidationError(
                {'end_offset': 'end_offset must not be before start_offset.'}
            )
        last = (routine.end - routine.start).days
        if end > last:
            raise serializers.ValidationError({'end_offset': f'The routine ends at offset {last}.'})
        # Runs inside the routine lock of recorded_edit, so no concurrent write slips in
        overlapping = routine.labels.filter(start_offset__lte=end, end_offset__gte=start)
        if self.instance is not None:
            overlapping = overlapping.exclude(pk=self.instance.pk)
        if other := overlapping.first():
            raise serializers.ValidationError(
                {
                    'start_offset': f'Overlaps label {other.pk} "{other.label}" '
                    f'({other.start_offset}-{other.end_offset}).'
                }
            )
        return data


class RoutineLabelViewSet(RecordedPlanEditMixin, WgerOwnerObjectModelViewSet):
    """
    API endpoint for routine labels, for the routine's owner only

    Every write is a recorded, undoable plan edit of the routine.
    """

    serializer_class = LabelSerializer
    is_private = True
    ordering_fields = '__all__'
    filterset_fields = ('routine',)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Labels are owner-only like previews: no trainer logged in as the member
        routine_preview.refuse_trainer(request)

    def get_queryset(self):
        # REST API generation
        if getattr(self, 'swagger_fake_view', False):
            return Label.objects.none()

        return Label.objects.filter(routine__user=self.request.user).order_by(
            'routine_id', 'start_offset', 'pk'
        )

    @staticmethod
    def get_owner_objects():
        return [(Routine, 'routine')]
