"""Scheduled purge of aged-out alert history.

Deletes in bounded batches rather than one statement: a single
`DELETE FROM alerts WHERE starts_at < ...` over months of history takes a
long-held lock and bloats WAL. Batching keeps each transaction short so
intake is never blocked behind the purge.

`notifications` rows disappear with their alert via ON DELETE CASCADE.

If volume ever exceeds roughly 100k alerts/month, convert `alerts` to monthly
partitions and swap this for DROP PARTITION, which is O(1).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Alert

log = logging.getLogger(__name__)

BATCH_SIZE = 5_000


def cutoff_for(months: int, *, now: datetime | None = None) -> datetime:
    """Approximates a month as 30 days.

    Retention here is a hygiene bound, not a legal one; exact calendar
    arithmetic would add a dependency for no operational benefit.
    """
    now = now or datetime.now(UTC)
    return now - timedelta(days=30 * months)


async def purge_old_alerts(months: int | None = None) -> int:
    """Delete alerts older than the retention window. Returns rows removed."""
    settings = get_settings()
    months = settings.alert_retention_months if months is None else months
    if months <= 0:
        log.info("retention: disabled (alert_retention_months=%s)", months)
        return 0

    cutoff = cutoff_for(months)
    removed = 0

    async with SessionLocal() as session:
        while True:
            ids = (
                (
                    await session.execute(
                        select(Alert.id)
                        .where(Alert.starts_at < cutoff)
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            await session.execute(delete(Alert).where(Alert.id.in_(ids)))
            await session.commit()
            removed += len(ids)

        remaining = (await session.execute(select(func.count(Alert.id)))).scalar_one()

    if removed:
        log.info(
            "retention: removed %d alert(s) older than %s (%d month(s)); %d remain",
            removed, cutoff.date(), months, remaining,
        )
    return removed
