"""Bounded recovery retention, discovered by Celery on all worker roles."""
# Standard Library
from datetime import timedelta

# Third Party
from celery import shared_task

# wger
from wger.celery_configuration import app
from wger.manager.models.session_recovery import purge_expired_recoveries


@shared_task(name='wger.manager.tasks.purge_session_recoveries')
def purge_session_recoveries():
    return purge_expired_recoveries()


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    # Same entry name as the deployment settings used, so an old
    # CELERY_BEAT_SCHEDULE entry overwrites rather than duplicates it.
    sender.add_periodic_task(
        timedelta(hours=1),
        purge_session_recoveries.s(),
        name='purge-session-recoveries',
    )
