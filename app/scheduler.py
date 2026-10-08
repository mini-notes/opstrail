"""In-process scheduler.

APScheduler rather than an external cron or pg_cron: on-prem installs should
need one container, not a second scheduling system to operate.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.services.retention import purge_old_alerts

log = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        purge_old_alerts,
        CronTrigger(hour=settings.retention_hour_utc, minute=0),
        id="purge_old_alerts",
        # A missed run (container down at 03:00) should still happen on the
        # next start rather than wait a full day.
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )
    log.info(
        "scheduler: retention job at %02d:00 UTC, keeping %d month(s)",
        settings.retention_hour_utc, settings.alert_retention_months,
    )
    return scheduler
