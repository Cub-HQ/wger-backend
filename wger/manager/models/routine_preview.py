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

# Standard Library
import uuid

# Django
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class RoutinePreview(models.Model):
    """
    An owner-private, immutable routine proposal awaiting approval

    This is the only table the preview writes: nothing here is a routine, and
    no routine, day, slot, entry, config, session or log is created for it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='routine_previews')
    idempotency_key = models.CharField(max_length=64)
    external_version = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    """sha256 of the whole request body, detects a reused key with another body"""
    plan_hash = models.CharField(max_length=64, db_index=True)
    """sha256 of the canonical proposal"""
    canonical_proposal = models.JSONField()
    schedule = models.JSONField()
    """The resolved schedule, frozen at creation like the proposal"""
    exercise_names = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                name='routine_preview_unique_idempotency_key',
            ),
        ]

    def save(self, *args, **kwargs):
        """Previews are immutable, a changed proposal is a new preview"""
        if not self._state.adding:
            raise ValueError('A routine preview cannot be changed once stored')
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()
