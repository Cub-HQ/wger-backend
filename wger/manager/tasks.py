"""Bounded recovery retention, discovered by Celery on all worker roles."""
from celery import shared_task
from wger.manager.models.session_recovery import purge_expired_recoveries


@shared_task(name='wger.manager.tasks.purge_session_recoveries')
def purge_session_recoveries():
    return purge_expired_recoveries()
