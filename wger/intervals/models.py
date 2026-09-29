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

# Django
from django.contrib.auth.models import User
from django.db import models

# wger
from wger.utils.uuid import uuid7


class EnduranceEntry(models.Model):
    """Read-only mirror of one Intervals.icu activity or planned event.

    Field names match planning.STORED_FIELDS; values are verbatim in source
    units. Edits happen only in Intervals. Never linked to gym sessions/sets.
    """

    id = models.UUIDField(default=uuid7, primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    kind = models.CharField(
        max_length=9,
        choices=[('planned', 'planned'), ('completed', 'completed')],
    )
    intervals_id = models.CharField(max_length=32)
    sport = models.CharField(max_length=40, null=True)
    name = models.CharField(max_length=255, null=True)
    start_local = models.CharField(max_length=32)
    local_date = models.DateField(db_index=True)
    timezone = models.CharField(max_length=64, null=True)
    moving_time_s = models.IntegerField(null=True)
    elapsed_time_s = models.IntegerField(null=True)
    distance_m = models.FloatField(null=True)
    training_load = models.IntegerField(null=True)
    power_load = models.IntegerField(null=True)
    hr_load = models.IntegerField(null=True)
    pace_load = models.IntegerField(null=True)
    hr_load_type = models.CharField(max_length=16, null=True)
    pace_load_type = models.CharField(max_length=16, null=True)
    intensity = models.FloatField(null=True)
    avg_hr = models.IntegerField(null=True)
    max_hr = models.IntegerField(null=True)
    rpe = models.IntegerField(null=True)
    feel = models.IntegerField(null=True)
    load_target = models.IntegerField(null=True)
    time_target = models.IntegerField(null=True)
    paired_event_id = models.IntegerField(null=True)
    activity_source = models.CharField(max_length=32, null=True)
    upstream_state = models.CharField(
        max_length=7,
        choices=[('present', 'present'), ('missing', 'missing')],
        default='present',
    )
    fetched_at = models.DateTimeField()

    class Meta:
        ordering = ['local_date', 'start_local']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'kind', 'intervals_id'],
                name='intervals_endurance_identity',
            ),
        ]

    def get_owner_object(self):
        return self


class IntervalsEventLink(models.Model):
    """Push ledger: one wger planned gym occurrence written to Intervals.

    external_id is `wger-gym:{routine_id}:{date}` (outbound.external_id). Only
    events with an active row here are ever updated or deleted remotely, and
    only while the remote id matches intervals_event_id. A row with no
    intervals_event_id is pending: saved just before its POST. Rows outlive
    their routine/day (SET_NULL) so a removed routine's event can still be
    deleted. `deleted` rows are history; one is reactivated only by a previewed
    `adopt` of an identical remote event.
    """

    id = models.UUIDField(default=uuid7, primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    external_id = models.CharField(max_length=64)
    routine = models.ForeignKey('manager.Routine', null=True, on_delete=models.SET_NULL)
    day = models.ForeignKey('manager.Day', null=True, on_delete=models.SET_NULL)
    date = models.DateField()
    intervals_event_id = models.BigIntegerField(null=True)
    pushed_hash = models.CharField(max_length=64)
    state = models.CharField(
        max_length=7,
        choices=[('active', 'active'), ('deleted', 'deleted')],
        default='active',
    )
    pushed_at = models.DateTimeField()

    class Meta:
        ordering = ['date', 'external_id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'external_id'],
                name='intervals_event_link_identity',
            ),
        ]

    def get_owner_object(self):
        return self
