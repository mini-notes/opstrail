"""Aggregate queries behind the dashboard.

Every query takes the same `Scope` (time window + optional service/team) so a
filter applied at the top of the page reaches all charts identically -- one
chart silently ignoring the filter would be worse than no filter at all.

A note on GROUP BY: always group by the EXPRESSION, never by the output label.
Postgres resolves a bare name in GROUP BY to an input column first (the
opposite of ORDER BY), so `group_by("severity")` groups by the raw
`alerts.severity` and splits "critical" from "CRITICAL" into two slices.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, String, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Notification, NotificationStatus, Service


@dataclass(slots=True)
class Scope:
    """What the dashboard is currently looking at."""

    days: int = 30
    service_id: int | None = None
    team_id: int | None = None

    @property
    def since(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=self.days)


def _scoped(stmt: Select, scope: Scope) -> Select:
    stmt = stmt.where(Alert.starts_at >= scope.since)
    if scope.service_id:
        stmt = stmt.where(Alert.service_id == scope.service_id)
    if scope.team_id:
        stmt = stmt.where(Alert.team_id == scope.team_id)
    return stmt


# --------------------------------------------------------------------------
# headline tiles
# --------------------------------------------------------------------------

async def headline(session: AsyncSession, scope: Scope) -> dict:
    total = (
        await session.execute(_scoped(select(func.count(Alert.id)), scope))
    ).scalar_one()
    firing = (
        await session.execute(
            _scoped(select(func.count(Alert.id)), scope).where(Alert.status == "firing")
        )
    ).scalar_one()
    unlabelled = (
        await session.execute(
            _scoped(select(func.count(Alert.id)), scope).where(
                Alert.label_source.is_(None)
            )
        )
    ).scalar_one()

    # Notifications are scoped through their alert so the service/team filter
    # applies here too.
    failed_stmt = (
        select(func.count(Notification.id))
        .select_from(Notification)
        .join(Alert, Alert.id == Notification.alert_id)
        .where(Notification.status == NotificationStatus.failed)
        .where(Alert.starts_at >= scope.since)
    )
    if scope.service_id:
        failed_stmt = failed_stmt.where(Alert.service_id == scope.service_id)
    if scope.team_id:
        failed_stmt = failed_stmt.where(Alert.team_id == scope.team_id)
    failed = (await session.execute(failed_stmt)).scalar_one()

    return {
        "alerts": total,
        "firing": firing,
        "unlabelled": unlabelled,
        "failed_notifications": failed,
        "days": scope.days,
    }


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------

async def alerts_per_day(session: AsyncSession, scope: Scope) -> list[dict]:
    """Alert count per calendar day, zero-filled.

    Zero-filling matters: a line that simply skips quiet days draws straight
    through an outage-free week and misreads as constant load.
    """
    day = func.date_trunc("day", Alert.starts_at)
    rows = (
        await session.execute(
            _scoped(select(day.label("day"), func.count(Alert.id)), scope)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    counts = {r[0].date(): r[1] for r in rows}

    today = datetime.now(UTC).date()
    return [
        {
            "day": (today - timedelta(days=offset)).isoformat(),
            "count": counts.get(today - timedelta(days=offset), 0),
        }
        for offset in range(scope.days - 1, -1, -1)
    ]


async def top_services(session: AsyncSession, scope: Scope, limit: int = 10) -> list[dict]:
    """Noisiest services. Unattributed alerts are shown as '(unlabelled)' --
    dropping them would hide exactly the backlog that needs fixing."""
    service_key = func.coalesce(Service.key, "(unlabelled)")
    rows = (
        await session.execute(
            _scoped(
                select(service_key.label("service"), func.count(Alert.id).label("n"))
                .select_from(Alert)
                .outerjoin(Service, Service.id == Alert.service_id),
                scope,
            )
            .group_by(service_key)
            .order_by(func.count(Alert.id).desc())
            .limit(limit)
        )
    ).all()
    return [{"service": r[0], "count": r[1]} for r in rows]


async def severity_breakdown(session: AsyncSession, scope: Scope) -> list[dict]:
    severity = func.coalesce(func.lower(Alert.severity), "unknown")
    rows = (
        await session.execute(
            _scoped(select(severity.label("severity"), func.count(Alert.id)), scope)
            .group_by(severity)
            .order_by(func.count(Alert.id).desc())
        )
    ).all()
    return [{"severity": r[0], "count": r[1]} for r in rows]


async def noisiest_alerts(session: AsyncSession, scope: Scope, limit: int = 12) -> list[dict]:
    """Which alert rules fire most, and how many notifications they cost.

    `occurrences` counts distinct alert rows; `notifications` counts how often
    we actually posted to a channel. A rule with a high ratio of notifications
    to occurrences is flapping and is the first candidate for a threshold or a
    `for:` duration change.
    """
    name = func.coalesce(Alert.alertname, "(unnamed)")
    rows = (
        await session.execute(
            _scoped(
                select(
                    name.label("alertname"),
                    func.count(Alert.id).label("occurrences"),
                    func.coalesce(func.sum(Alert.notify_count), 0).label("notifications"),
                    func.count(func.distinct(Alert.service_id)).label("services"),
                ),
                scope,
            )
            .group_by(name)
            .order_by(func.count(Alert.id).desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "alertname": r[0],
            "occurrences": r[1],
            "notifications": int(r[2]),
            "services": r[3],
        }
        for r in rows
    ]


async def duration_percentiles(
    session: AsyncSession, scope: Scope, limit: int = 8
) -> list[dict]:
    """How long alerts stay firing, per service (p50 / p90, in minutes).

    This is NOT MTTR. It measures how long the alert CONDITION held, which a
    flapping rule splits across many short firing/resolved pairs. Real MTTR
    needs the `incidents` table and a human-owned lifecycle (Phase 2).
    """
    service_key = func.coalesce(Service.key, "(unlabelled)")
    duration = Alert.ends_at - Alert.starts_at
    p50 = func.percentile_cont(0.5).within_group(duration)
    p90 = func.percentile_cont(0.9).within_group(duration)

    rows = (
        await session.execute(
            _scoped(
                select(
                    service_key.label("service"),
                    func.count(Alert.id).label("n"),
                    p50.label("p50"),
                    p90.label("p90"),
                )
                .select_from(Alert)
                .outerjoin(Service, Service.id == Alert.service_id),
                scope,
            )
            .where(Alert.ends_at.is_not(None))
            .group_by(service_key)
            .order_by(func.count(Alert.id).desc())
            .limit(limit)
        )
    ).all()

    def minutes(td) -> float:
        return round(td.total_seconds() / 60, 1) if td else 0.0

    return [
        {"service": r[0], "count": r[1], "p50_min": minutes(r[2]), "p90_min": minutes(r[3])}
        for r in rows
    ]


async def hour_weekday_heatmap(session: AsyncSession, scope: Scope) -> list[dict]:
    """Alerts bucketed by weekday (0=Sunday) and hour, as a dense 7x24 grid.

    Dense rather than sparse: an empty cell is information (nothing fires at
    04:00 on Sunday) and the client should not have to invent the gaps.
    """
    dow = func.extract("dow", Alert.starts_at)
    hour = func.extract("hour", Alert.starts_at)
    rows = (
        await session.execute(
            _scoped(
                select(dow.label("dow"), hour.label("hour"), func.count(Alert.id)), scope
            ).group_by(dow, hour)
        )
    ).all()
    counts = {(int(r[0]), int(r[1])): r[2] for r in rows}
    return [
        {"dow": d, "hour": h, "count": counts.get((d, h), 0)}
        for d in range(7)
        for h in range(24)
    ]


async def label_coverage(session: AsyncSession, scope: Scope) -> list[dict]:
    """Where attribution came from. `none` is the actionable backlog."""
    # Cast the enum to text so NULL can collapse into a literal "none" bucket.
    source = func.coalesce(func.cast(Alert.label_source, String), "none")
    rows = (
        await session.execute(
            _scoped(select(source.label("source"), func.count(Alert.id)), scope)
            .group_by(source)
            .order_by(func.count(Alert.id).desc())
        )
    ).all()
    return [{"source": r[0], "count": r[1]} for r in rows]


async def notification_health(session: AsyncSession, scope: Scope) -> list[dict]:
    """Delivery outcome per channel -- the answer to 'did the alert go out?'."""
    stmt = (
        select(
            Notification.channel,
            func.count(Notification.id).label("total"),
            func.count(case((Notification.status == NotificationStatus.sent, 1)))
            .label("sent"),
            func.count(case((Notification.status == NotificationStatus.failed, 1)))
            .label("failed"),
            func.count(case((Notification.status == NotificationStatus.pending, 1)))
            .label("pending"),
        )
        .select_from(Notification)
        .join(Alert, Alert.id == Notification.alert_id)
        .where(Alert.starts_at >= scope.since)
        .group_by(Notification.channel)
    )
    if scope.service_id:
        stmt = stmt.where(Alert.service_id == scope.service_id)
    if scope.team_id:
        stmt = stmt.where(Alert.team_id == scope.team_id)

    rows = (await session.execute(stmt)).all()
    return [
        {"channel": r[0].value, "total": r[1], "sent": r[2], "failed": r[3], "pending": r[4]}
        for r in rows
    ]
