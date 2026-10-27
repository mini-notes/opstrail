"""Dedicated worker: python -m app.worker. No UI/API process is required."""

import asyncio
import logging
import signal
from datetime import timedelta

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import WorkerState
from app.services import dispatch, retention, severity_rules

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)


async def maintenance_loop():
    while True:
        try:
            # One maintenance owner, even with two worker containers. Locks
            # release on rollback, cancellation or lost DB connection.
            async with SessionLocal() as session, session.begin():
                locked = (
                    await session.execute(text("SELECT pg_try_advisory_xact_lock(782031450)"))
                ).scalar_one()
                if locked:
                    await severity_rules.reevaluate_once()
                    last = await session.get(WorkerState, "retention")
                    if last is None or dispatch._now() - last.updated_at > timedelta(hours=24):
                        await retention.purge_old_alerts()
                        await retention.purge_old_notifications()
                        await dispatch.heartbeat("retention")
                    await dispatch.heartbeat("maintenance")
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(60)


async def main():
    get_settings().validate_runtime()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(dispatch.dispatch_loop()),
        asyncio.create_task(maintenance_loop()),
        asyncio.create_task(stop.wait()),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
