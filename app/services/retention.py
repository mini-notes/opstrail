"""Scheduled purge of aged-out history.

Two windows, because the two kinds of record age out at different speeds.
An alert is the record of what happened; a delivery is debugging data for
"did it go out", which is asked within days and is the bulkier of the two
(one row per channel per attempt, and a failed custom template puts its whole
rendered output in `last_error`).

Deletes in bounded batches rather than one statement: a single
`DELETE FROM alerts WHERE starts_at < ...` over months of history takes a
long-held lock and bloats WAL. Batching keeps each transaction short so
intake is never blocked behind the purge.

`notifications` rows disappear with their alert via ON DELETE CASCADE, so the
notification window only does anything while it is SHORTER than the alert
window; app/main.py warns at startup when it is not.

If volume ever exceeds roughly 100k alerts/month, convert `alerts` to monthly
partitions and swap this for DROP PARTITION, which is O(1).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Alert, Notification, NotificationStatus

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


def notification_cutoff(months: int | None = None) -> datetime | None:
    """The age at which delivery records stop being kept, or None when the
    purge is disabled.

    Exported because the alert detail page needs it: with no rows to show it
    has to say whether the history was purged or never existed, and an empty
    table reads as "nothing was ever sent".
    """
    settings = get_settings()
    months = settings.notification_retention_months if months is None else months
    return cutoff_for(months) if months > 0 else None


async def preview() -> dict:
    """What the next run would remove, and what is being kept.

    Exists so the admin page can answer "is retention working" without anyone
    having to wait for 03:00 and then read the logs. Counting uses the same
    predicates as the purge itself, so the number shown is the number that
    would go -- a preview computed a different way is a preview that lies.
    """
    settings = get_settings()
    alert_months = settings.alert_retention_months
    note_months = settings.notification_retention_months
    alert_cut = cutoff_for(alert_months) if alert_months > 0 else None
    note_cut = cutoff_for(note_months) if note_months > 0 else None

    async with SessionLocal() as session:
        async def count(stmt):
            return (await session.execute(stmt)).scalar_one()

        alerts_total = await count(select(func.count(Alert.id)))
        notes_total = await count(select(func.count(Notification.id)))
        alerts_due = (
            await count(
                select(func.count(Alert.id)).where(Alert.starts_at < alert_cut)
            )
            if alert_cut
            else 0
        )
        notes_due = (
            await count(
                select(func.count(Notification.id)).where(_purgeable(note_cut))
            )
            if note_cut
            else 0
        )
        inflight = await count(
            select(func.count(Notification.id)).where(
                Notification.status == NotificationStatus.pending,
                or_(
                    Notification.next_attempt_at.is_(None),
                    Notification.next_attempt_at > func.now(),
                ),
            )
        )

    return {
        "alert_months": alert_months,
        "notification_months": note_months,
        "alert_cutoff": alert_cut,
        "notification_cutoff": note_cut,
        "alerts_total": alerts_total,
        "alerts_due": alerts_due,
        "notifications_total": notes_total,
        "notifications_due": notes_due,
        # Pending deliveries are never purged however old they are; showing
        # the number stops "why didn't that row go" being a mystery.
        "notifications_inflight": inflight,
    }


def _purgeable(cutoff: datetime):
    """Delivery rows old enough to drop.

    The `pending` carve-out is the point of this predicate. A pending row
    whose next attempt is still in the future is an in-flight retry that the
    dispatch sweeper has not reached yet; deleting it drops the delivery with
    no record that anything was attempted.

    A pending row whose next attempt has passed is a different thing: the
    sweeper gives up on AGE after dispatch_give_up_after_minutes, so a row
    months past its retry time is dead and nothing will ever pick it up. Those
    are purged like any other finished row -- otherwise they accumulate for
    ever, and the one state we refuse to delete becomes a leak.
    """
    return and_(
        Notification.created_at < cutoff,
        or_(
            Notification.status != NotificationStatus.pending,
            Notification.next_attempt_at.is_(None),
            Notification.next_attempt_at <= func.now(),
        ),
    )


async def purge_old_notifications(months: int | None = None) -> int:
    """Delete delivery records older than their own window. Returns rows removed."""
    settings = get_settings()
    months = settings.notification_retention_months if months is None else months
    if months <= 0:
        log.info("retention: notification purge disabled (months=%s)", months)
        return 0

    cutoff = cutoff_for(months)
    removed = 0

    async with SessionLocal() as session:
        while True:
            ids = (
                (
                    await session.execute(
                        select(Notification.id)
                        .where(_purgeable(cutoff))
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            # The predicate is repeated on the DELETE, not just the SELECT: a
            # row can become due for a retry between the two statements, and
            # it must be spared when that happens. rowcount rather than
            # len(ids) so the tally stays honest when it does.
            result = await session.execute(
                delete(Notification).where(
                    Notification.id.in_(ids), _purgeable(cutoff)
                )
            )
            await session.commit()
            if not result.rowcount:
                # Everything in this batch became live. Nothing left to do
                # that another pass would fix, and looping would spin.
                break
            removed += result.rowcount

        stuck = (
            await session.execute(
                select(func.count(Notification.id)).where(
                    Notification.created_at < cutoff,
                    Notification.status == NotificationStatus.pending,
                    Notification.next_attempt_at > func.now(),
                )
            )
        ).scalar_one()

    if stuck:
        # Not an error here -- refusing to delete these is correct -- but a
        # delivery still queued months after it was created means the sweeper
        # has not run, and that is worth saying out loud.
        log.warning(
            "retention: kept %d pending notification(s) older than %s that are "
            "still awaiting a retry -- is the dispatch loop running?",
            stuck, cutoff.date(),
        )
    if removed:
        log.info(
            "retention: removed %d delivery record(s) older than %s (%d month(s))",
            removed, cutoff.date(), months,
        )
    return removed
