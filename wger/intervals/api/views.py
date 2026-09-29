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

# Third Party
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

# wger
from wger.intervals.models import EnduranceEntry
from wger.intervals.planning import STORED_FIELDS, link


class EnduranceEntrySerializer(serializers.ModelSerializer):
    link = serializers.SerializerMethodField()
    link_exact = serializers.SerializerMethodField()

    class Meta:
        model = EnduranceEntry
        fields = ('id', *STORED_FIELDS, 'fetched_at', 'link', 'link_exact')

    def get_link(self, obj) -> str:
        return link(obj.kind, obj.intervals_id, obj.local_date)[0]

    def get_link_exact(self, obj) -> bool:
        return link(obj.kind, obj.intervals_id, obj.local_date)[1]


class EnduranceEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """Intervals.icu rides/runs/swims mirrored for the calendar. Read-only;
    edit them in Intervals."""

    permission_classes = [IsAuthenticated]
    serializer_class = EnduranceEntrySerializer
    filterset_fields = {
        'local_date': ['exact', 'gte', 'lte'],
        'kind': ['exact'],
        'upstream_state': ['exact'],
    }

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return EnduranceEntry.objects.none()
        return EnduranceEntry.objects.filter(user=self.request.user)
