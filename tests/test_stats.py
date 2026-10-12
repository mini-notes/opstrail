"""Dashboard aggregation, against a real Postgres.

SQLite cannot stand in here: the queries use date_trunc and native enums.
The test is skipped when no database is reachable so the rest of the suite
still runs on a laptop without Docker.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Alert, AlertStatus, Service, Team
from app.services import stats
from app.services.stats import Scope

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)


@pytest.fixture
async def session():
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test database at {DSN}: {exc}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _alert(fp: str, *, days_ago: float, severity: str, service_id=None, status="resolved"):
    start = datetime.now(UTC) - timedelta(days=days_ago)
    return Alert(
        fingerprint=fp,
        starts_at=start,
        ends_at=start + timedelta(minutes=5),
        status=AlertStatus(status),
        alertname="X",
        severity=severity,
        service_id=service_id,
        labels={},
        annotations={},
    )


async def test_quiet_days_are_zero_filled(session):
    """A quiet day must appear as 0, not vanish -- a chart that skips days
    draws a flat line through an outage-free week and misreads as steady load."""
    rows = await stats.alerts_per_day(session, Scope(days=7))
    assert len(rows) == 7
    assert all(r["count"] == 0 for r in rows)
    # strictly increasing, one calendar day apart, no gaps
    days = [r["day"] for r in rows]
    assert days == sorted(days) and len(set(days)) == 7


async def test_alerts_per_day_counts_within_window(session):
    """Deliberately does NOT assert which bucket: `days_ago` is relative to
    now, so whether 3h ago lands on today or yesterday depends on the clock
    at run time. The invariant that matters is that nothing is lost."""
    session.add(_alert("a", days_ago=0.1, severity="critical"))
    session.add(_alert("b", days_ago=0.2, severity="info"))
    await session.commit()

    rows = await stats.alerts_per_day(session, Scope(days=7))
    assert len(rows) == 7
    assert sum(r["count"] for r in rows) == 2


async def test_severity_breakdown_groups_case_insensitively(session):
    for i, sev in enumerate(["critical", "CRITICAL", "warning"]):
        session.add(_alert(f"s{i}", days_ago=1, severity=sev))
    await session.commit()

    breakdown = await stats.severity_breakdown(session, Scope(days=30))
    rows = {r["severity"]: r["count"] for r in breakdown}
    assert rows["critical"] == 2
    assert rows["warning"] == 1


async def test_missing_severity_becomes_unknown(session):
    session.add(_alert("n", days_ago=1, severity=None))
    await session.commit()
    breakdown = await stats.severity_breakdown(session, Scope(days=30))
    rows = {r["severity"]: r["count"] for r in breakdown}
    assert rows["unknown"] == 1


async def test_top_services_surfaces_unlabelled(session):
    """Unattributed alerts are the backlog; dropping them from the chart
    would hide exactly the thing that needs fixing."""
    team = Team(key="t", name="T")
    session.add(team)
    await session.flush()
    svc = Service(key="svc", name="Svc", team_id=team.id)
    session.add(svc)
    await session.flush()

    session.add(_alert("x1", days_ago=1, severity="info", service_id=svc.id))
    session.add(_alert("x2", days_ago=1, severity="info", service_id=svc.id))
    session.add(_alert("x3", days_ago=1, severity="info"))
    await session.commit()

    rows = {r["service"]: r["count"] for r in await stats.top_services(session, Scope(days=30))}
    assert rows["svc"] == 2
    assert rows["(unlabelled)"] == 1


async def test_headline_counts(session):
    session.add(_alert("h1", days_ago=1, severity="critical", status="firing"))
    session.add(_alert("h2", days_ago=1, severity="info"))
    await session.commit()

    head = await stats.headline(session, Scope(days=30))
    assert head["alerts"] == 2
    assert head["firing"] == 1
    assert head["unlabelled"] == 2


async def test_window_excludes_older_alerts(session):
    session.add(_alert("recent", days_ago=1, severity="info"))
    session.add(_alert("ancient", days_ago=90, severity="info"))
    await session.commit()
    assert (await stats.headline(session, Scope(days=30)))["alerts"] == 1


# ---------------------------------------------------------------------------
# Scope filtering and the analytics added for the dashboard
# ---------------------------------------------------------------------------

async def _two_services(session):
    t1, t2 = Team(key="t1", name="T1"), Team(key="t2", name="T2")
    session.add_all([t1, t2])
    await session.flush()
    s1 = Service(key="svc1", name="Svc1", team_id=t1.id)
    s2 = Service(key="svc2", name="Svc2", team_id=t2.id)
    session.add_all([s1, s2])
    await session.flush()
    return (s1, t1), (s2, t2)


async def test_scope_filters_by_service(session):
    (s1, _), (s2, _) = await _two_services(session)
    session.add(_alert("f1", days_ago=1, severity="critical", service_id=s1.id))
    session.add(_alert("f2", days_ago=1, severity="info", service_id=s2.id))
    await session.commit()

    assert (await stats.headline(session, Scope(days=30)))["alerts"] == 2
    scoped = await stats.headline(session, Scope(days=30, service_id=s1.id))
    assert scoped["alerts"] == 1


async def test_scope_filters_by_team(session):
    (s1, t1), (s2, t2) = await _two_services(session)
    a = _alert("t1a", days_ago=1, severity="info", service_id=s1.id)
    a.team_id = t1.id
    b = _alert("t2a", days_ago=1, severity="info", service_id=s2.id)
    b.team_id = t2.id
    session.add_all([a, b])
    await session.commit()

    assert (await stats.headline(session, Scope(days=30, team_id=t1.id)))["alerts"] == 1


async def test_noisiest_counts_occurrences_and_notifications(session):
    """A rule that notified far more often than it occurred is flapping; the
    chart exists to surface exactly that ratio."""
    a = _alert("n1", days_ago=1, severity="critical")
    a.alertname = "Flappy"
    a.notify_count = 9
    b = _alert("n2", days_ago=1, severity="critical")
    b.alertname = "Flappy"
    b.notify_count = 7
    c = _alert("n3", days_ago=1, severity="info")
    c.alertname = "Quiet"
    c.notify_count = 1
    session.add_all([a, b, c])
    await session.commit()

    noisy = await stats.noisiest_alerts(session, Scope(days=30))
    rows = {r["alertname"]: r for r in noisy}
    assert rows["Flappy"]["occurrences"] == 2
    assert rows["Flappy"]["notifications"] == 16
    assert rows["Quiet"]["occurrences"] == 1


async def test_duration_percentiles_ignore_unresolved(session):
    """An alert still firing has no duration; including it as zero would drag
    every percentile down and make outages look shorter than they were."""
    (s1, _), _ = await _two_services(session)
    for i, minutes in enumerate([10, 20, 30]):
        a = _alert(f"d{i}", days_ago=1, severity="info", service_id=s1.id)
        a.ends_at = a.starts_at + timedelta(minutes=minutes)
        session.add(a)
    still_firing = _alert(
        "open", days_ago=1, severity="info", service_id=s1.id, status="firing"
    )
    still_firing.ends_at = None
    session.add(still_firing)
    await session.commit()

    rows = await stats.duration_percentiles(session, Scope(days=30))
    assert len(rows) == 1
    assert rows[0]["count"] == 3          # the unresolved one is excluded
    assert rows[0]["p50_min"] == 20.0


async def test_heatmap_is_a_dense_7x24_grid(session):
    """Dense, not sparse: an empty cell is information, and the client should
    not have to invent the gaps."""
    session.add(_alert("h", days_ago=1, severity="info"))
    await session.commit()

    cells = await stats.hour_weekday_heatmap(session, Scope(days=30))
    assert len(cells) == 7 * 24
    assert {c["dow"] for c in cells} == set(range(7))
    assert {c["hour"] for c in cells} == set(range(24))
    assert sum(c["count"] for c in cells) == 1


async def test_label_coverage_names_the_backlog(session):
    """Unattributed alerts must appear as a real 'none' bucket, not vanish."""
    from app.models import LabelSource

    a = _alert("c1", days_ago=1, severity="info")
    a.label_source = LabelSource.payload
    b = _alert("c2", days_ago=1, severity="info")
    b.label_source = LabelSource.manual
    c = _alert("c3", days_ago=1, severity="info")  # label_source stays NULL
    session.add_all([a, b, c])
    await session.commit()

    coverage = await stats.label_coverage(session, Scope(days=30))
    rows = {r["source"]: r["count"] for r in coverage}
    assert rows == {"payload": 1, "manual": 1, "none": 1}


# ---------------------------------------------------------------------------
# absolute windows and the viewer's timezone
# ---------------------------------------------------------------------------

def _at(moment: datetime, fp: str) -> Alert:
    """An alert at an exact instant, for boundary assertions."""
    return Alert(
        fingerprint=fp, starts_at=moment, ends_at=moment + timedelta(minutes=1),
        status=AlertStatus.resolved, alertname="X", severity="critical",
        labels={}, annotations={},
    )


@pytest.mark.asyncio
async def test_an_absolute_window_is_bounded_at_both_ends(session):
    """A lower bound alone would let an absolute range run to now, and two
    charts on the same page would then be counting different things."""
    base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    session.add_all([
        _at(base - timedelta(hours=2), "before"),
        _at(base + timedelta(hours=2), "inside"),
        _at(base + timedelta(days=5), "after"),
    ])
    await session.commit()

    scope = Scope(start=base, end=base + timedelta(days=1))
    assert (await stats.headline(session, scope))["alerts"] == 1


@pytest.mark.asyncio
async def test_absolute_wins_when_both_are_given(session):
    base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    session.add_all([_at(base + timedelta(hours=1), "inside")])
    await session.commit()

    # days=1 would mean "the last 24 hours", which is nowhere near June.
    scope = Scope(days=1, start=base, end=base + timedelta(days=1))
    assert (await stats.headline(session, scope))["alerts"] == 1
    assert scope.is_absolute


@pytest.mark.asyncio
async def test_a_short_absolute_window_is_bucketed_by_hour(session):
    """A per-day line over a two-hour range is a single point."""
    base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    session.add_all([_at(base + timedelta(minutes=30), "a")])
    await session.commit()

    rows = await stats.alerts_per_day(
        session, Scope(start=base, end=base + timedelta(hours=4))
    )
    assert len(rows) == 5  # 12:00 through 16:00 inclusive
    assert sum(r["count"] for r in rows) == 1


@pytest.mark.asyncio
async def test_the_day_boundary_follows_the_viewer_not_utc(session):
    """22:00 UTC is 05:00 the NEXT day in Vietnam. Bucketing in UTC while
    labelling in local time would file the morning under the day before."""
    utc_evening = datetime(2026, 6, 15, 22, 0, tzinfo=UTC)
    session.add_all([_at(utc_evening, "evening")])
    await session.commit()

    window = Scope(
        start=datetime(2026, 6, 14, tzinfo=UTC),
        end=datetime(2026, 6, 17, tzinfo=UTC),
    )
    in_utc = {r["day"][:10]: r["count"] for r in await stats.alerts_per_day(session, window)}

    window_vn = Scope(start=window.start, end=window.end, tz_offset_minutes=420)
    in_vn = {r["day"][:10]: r["count"] for r in await stats.alerts_per_day(session, window_vn)}

    assert in_utc["2026-06-15"] == 1
    assert in_vn["2026-06-16"] == 1
    assert in_vn.get("2026-06-15", 0) == 0


@pytest.mark.asyncio
async def test_the_heatmap_hour_follows_the_viewer_too(session):
    """'Nothing fires at 04:00' is a claim about when people are asleep where
    they live, not about UTC."""
    session.add_all([_at(datetime(2026, 6, 15, 22, 0, tzinfo=UTC), "evening")])
    await session.commit()

    window = {"start": datetime(2026, 6, 14, tzinfo=UTC),
              "end": datetime(2026, 6, 17, tzinfo=UTC)}
    utc = {(r["dow"], r["hour"]): r["count"]
           for r in await stats.hour_weekday_heatmap(session, Scope(**window))}
    vn = {(r["dow"], r["hour"]): r["count"]
          for r in await stats.hour_weekday_heatmap(
              session, Scope(**window, tz_offset_minutes=420))}

    assert utc[(1, 22)] == 1          # Monday 22:00 UTC
    assert vn[(2, 5)] == 1            # Tuesday 05:00 in Vietnam
    assert vn.get((1, 22), 0) == 0


@pytest.mark.asyncio
async def test_a_relative_window_still_means_what_it_always_did(session):
    """The chart and the tiles beside it are counted over the same rolling
    window, and 'last 7 days' has always drawn seven labels ending today.
    Adding absolute ranges must not quietly renegotiate that."""
    rows = await stats.alerts_per_day(session, Scope(days=7))
    assert len(rows) == 7
