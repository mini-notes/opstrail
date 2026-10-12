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
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import Select, String, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Notification, NotificationStatus, Service

DEFAULT_DAYS = 30
# Below this, a per-day line has too few points to read; at or above it, a
# per-hour line has too many -- three days is already 72 points on a chart
# 150px tall. Grafana solves this with a full unit ladder; two steps cover
# every window this dashboard offers.
HOURLY_BELOW = timedelta(days=2)


@dataclass(slots=True)
class Scope:
    """What the dashboard is currently looking at.

    Two ways to say when: `days` is a window ending now, `start`/`end` is a
    fixed one. An absolute window wins when both are given -- it is the more
    specific statement, and the picker only ever sends one of them.

    `tz_offset_minutes` is the VIEWER's offset from UTC, and it is not
    cosmetic. "Alerts per day" and the hour/weekday heatmap both cut time
    into buckets, and a day boundary in UTC is 07:00 in Vietnam: bucketing in
    UTC while labelling in local time would put the morning's alerts on the
    previous day. Storage and comparison stay in UTC throughout; the offset
    is applied only where a bucket edge has to fall somewhere.
    """

    days: int | None = 30
    start: datetime | None = None
    end: datetime | None = None
    service_id: int | None = None
    team_id: int | None = None
    tz_offset_minutes: int = 0

    @property
    def window(self) -> tuple[datetime, datetime]:
        """(from, to), both aware UTC. Always bounded at both ends."""
        if self.start is not None and self.end is not None:
            return self.start, self.end
        now = datetime.now(UTC)
        return now - timedelta(days=self.days or DEFAULT_DAYS), now

    @property
    def since(self) -> datetime:
        """Kept because it reads better at call sites that only need the
        lower bound."""
        return self.window[0]

    @property
    def is_absolute(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def span(self) -> timedelta:
        start, end = self.window
        return end - start

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(minutes=self.tz_offset_minutes))


def _local(column, scope: Scope):
    """A timestamp shifted into the viewer's offset, for bucketing only.

    Postgres stores timestamptz in UTC; adding the offset before truncating
    is what makes a "day" mean the viewer's day. Never used for filtering --
    the window bounds are already absolute instants, and shifting those too
    would move the window instead of the bucket edges.
    """
    if not scope.tz_offset_minutes:
        return column
    return column + func.make_interval(0, 0, 0, 0, 0, scope.tz_offset_minutes)


def _scoped(stmt: Select, scope: Scope) -> Select:
    start, end = scope.window
    # Bounded at BOTH ends. With only a lower bound an absolute window would
    # silently run to now, and two charts on the same page would be counting
    # different things.
    stmt = stmt.where(Alert.starts_at >= start, Alert.starts_at <= end)
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
        .where(Alert.starts_at >= scope.window[0], Alert.starts_at <= scope.window[1])
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
        # What the window actually is, not how it was asked for: an absolute
        # range has no "days", and the client needs the bounds to label axes.
        "days": scope.days,
        "from": scope.window[0].isoformat(),
        "to": scope.window[1].isoformat(),
        "absolute": scope.is_absolute,
    }


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------

async def alerts_per_day(session: AsyncSession, scope: Scope) -> list[dict]:
    """Alert count per bucket across the window, zero-filled.

    Zero-filling matters: a line that simply skips quiet buckets draws
    straight through an outage-free week and misreads as constant load.

    Buckets are the VIEWER's days (or hours), not UTC ones. Somebody in
    Vietnam looking at "yesterday" means their yesterday, and a UTC day
    boundary would file their morning under the day before.
    """
    hourly = scope.span <= HOURLY_BELOW
    unit = "hour" if hourly else "day"
    step = timedelta(hours=1) if hourly else timedelta(days=1)

    bucket = func.date_trunc(unit, _local(Alert.starts_at, scope))
    rows = (
        await session.execute(
            _scoped(select(bucket.label("bucket"), func.count(Alert.id)), scope)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    # date_trunc on a shifted timestamptz comes back as timestamptz in the
    # session zone (UTC); the shift is already baked into the value, so the
    # key is the naive local instant.
    counts = {r[0].replace(tzinfo=None): r[1] for r in rows}

    start, end = scope.window
    tz = scope.tz
    last = end.astimezone(tz).replace(tzinfo=None)

    if scope.is_absolute:
        # Span the whole range; the first and last buckets may be partial,
        # which is what an absolute range means.
        cursor = _truncate(start.astimezone(tz).replace(tzinfo=None), unit)
    else:
        # "Last 7 days" has always meant seven labels ending today, and the
        # tiles beside the chart are counted over the same rolling window.
        # Anchoring on the end keeps both, rather than quietly turning it
        # into eight buckets the first of which is a part-day.
        buckets = int(scope.span / step) or 1
        cursor = _truncate(last, unit) - step * (buckets - 1)

    out = []
    # Bounded: a malformed window must not spin. 400 points is already more
    # than the chart can draw usefully.
    while cursor <= last and len(out) < 1000:
        out.append({"day": cursor.isoformat(), "count": counts.get(cursor, 0)})
        cursor += step
    return out


def _truncate(moment: datetime, unit: str) -> datetime:
    if unit == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


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
    # The viewer's clock, not UTC. "Nothing fires at 04:00" is a claim about
    # the hour people are asleep where they live; in UTC it would be a claim
    # about 11:00 in Vietnam, which is the opposite of what the chart says.
    local = _local(Alert.starts_at, scope)
    dow = func.extract("dow", local)
    hour = func.extract("hour", local)
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
        .where(Alert.starts_at >= scope.window[0],
               Alert.starts_at <= scope.window[1])
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
