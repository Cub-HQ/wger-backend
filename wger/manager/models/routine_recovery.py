"""Fourteen-day routine trash, edit and rebuild recovery records; see wger.manager.routine_recovery"""
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models


def protect_history(collector, field, sub_objs, using):
    """
    on_delete of history -> plan foreign keys: deleting a plan row that completed
    history references fails, except while a whole account is deleted, where
    another user's history merely loses the link (their own goes with them)
    """
    if collector.data.get(get_user_model()):
        models.SET_NULL(collector, field, sub_objs, using)
    else:
        models.RESTRICT(collector, field, sub_objs, using)


class RoutineRecovery(models.Model):
    TRASH = 'trash'
    EDIT = 'edit'
    REBUILD = 'rebuild'
    RESTORE = 'restore'
    OPERATIONS = [(o, o) for o in (TRASH, EDIT, REBUILD, RESTORE)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    routine = models.ForeignKey('Routine', on_delete=models.CASCADE, related_name='+')
    replacement = models.ForeignKey(
        'Routine', null=True, on_delete=models.CASCADE, related_name='+'
    )
    operation = models.CharField(max_length=10, choices=OPERATIONS)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    restored_at = models.DateTimeField(null=True)

    snapshot = models.JSONField()
    """The complete plan graph of `routine` before the operation"""

    revision_after = models.CharField(max_length=64)
    """Revision the client holds right after the operation (the replacement's for rebuild)"""

    idempotency_key = models.CharField(max_length=100, null=True)
    request_hash = models.CharField(max_length=64, null=True)
    receipt = models.JSONField(null=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'expires_at'], name='routine_recovery_owner_expiry')]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='routine_recovery_idempotency',
            )
        ]
