"""The delivery history page.

Three things are worth pinning here, and they are the three that would be
silent if they broke:

* the time window filters when the delivery was ATTEMPTED, not when the alert
  started -- the opposite of stats.notification_health, deliberately;
* an empty delivery table says WHY it is empty, because "nobody was told" and
  "the records aged out" are different problems and look identical;
* the Power Automate target never leaves the server in full on a list page.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.ui import _delivery_query, empty_delivery_reason
from app.db import Base
from app.models import (
    Alert,
    AlertStatus,
    Notification,
    NotificationChannel,
    NotificationStatus,
    Service,
)
from app.templating import redact_target

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)

NO_FILTERS = dict(
    channel=None, delivery_status=None, service_id=None,
    team_id=None, q=None, days=None,
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


def make_alert(**kw) -> Alert:
    base = dict(
        fingerprint="fp", starts_at=datetime.now(UTC), received_at=datetime.now(UTC),
        status=AlertStatus.firing, alertname="PostgresqlDown", severity="critical",
        labels={}, annotations={}, notify_count=0,
    )
    base.update(kw)
    return Alert(**base)


async def _ids(session, **filters):
    stmt = _delivery_query(**{**NO_FILTERS, **filters})
    return {n.id for n in (await session.execute(stmt)).scalars().all()}


# ---------------------------------------------------------------------------
# the time window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_is_the_attempt_time_not_the_alert_start(session):
    """A retry of a two-day-old alert happened TODAY. On a page about
    deliveries, "last 1 day" has to mean deliveries in the last day.

    stats.notification_health scopes by Alert.starts_at instead, on purpose.
    If someone ever "fixes" the two to agree, this test says which is which.
    """
    old_alert = make_alert(fingerprint="old", starts_at=datetime.now(UTC) - timedelta(days=5))
    session.add(old_alert)
    await session.flush()
    retried_today = Notification(
        alert_id=old_alert.id, channel=NotificationChannel.msteams,
        target="https://example/x", status=NotificationStatus.sent,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(retried_today)
    await session.commit()

    assert await _ids(session, days=1) == {retried_today.id}


@pytest.mark.asyncio
async def test_old_attempt_on_a_new_alert_is_outside_the_window(session):
    alert = make_alert(fingerprint="new")
    session.add(alert)
    await session.flush()
    session.add(Notification(
        alert_id=alert.id, channel=NotificationChannel.msteams,
        target="https://example/x", status=NotificationStatus.sent,
        created_at=datetime.now(UTC) - timedelta(days=10),
    ))
    await session.commit()

    assert await _ids(session, days=1) == set()


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_each_filter_narrows_independently(session):
    svc = Service(key="payments-api", name="Payments")
    session.add(svc)
    await session.flush()
    a1 = make_alert(fingerprint="a1", service_id=svc.id)
    a2 = make_alert(fingerprint="a2", alertname="DiskWillFill")
    session.add_all([a1, a2])
    await session.flush()

    teams_sent = Notification(
        alert_id=a1.id, channel=NotificationChannel.msteams,
        target="https://flow/abc", status=NotificationStatus.sent,
    )
    hook_failed = Notification(
        alert_id=a2.id, channel=NotificationChannel.webhook,
        target="https://hook/def", status=NotificationStatus.failed,
        last_error="connection refused",
    )
    session.add_all([teams_sent, hook_failed])
    await session.commit()

    assert await _ids(session, channel="msteams") == {teams_sent.id}
    assert await _ids(session, delivery_status="failed") == {hook_failed.id}
    assert await _ids(session, service_id=svc.id) == {teams_sent.id}
    assert await _ids(session, q="DiskWill") == {hook_failed.id}
    assert await _ids(session, q="connection refused") == {hook_failed.id}
    # Searching the target is how you find everything sent to one webhook,
    # even though the value is never displayed in full.
    assert await _ids(session, q="hook/def") == {hook_failed.id}


# ---------------------------------------------------------------------------
# why the table is empty
# ---------------------------------------------------------------------------

CUTOFF = datetime(2026, 7, 1, tzinfo=UTC)


def test_sent_count_without_rows_proves_the_records_were_purged():
    """notify_count lives on the alert, so it survives the purge."""
    alert = make_alert(notify_count=3, starts_at=datetime(2026, 1, 1, tzinfo=UTC))
    reason = empty_delivery_reason(alert, CUTOFF, 3)
    assert "3×" in reason and "retention" in reason


def test_old_alert_with_no_sends_admits_the_ambiguity():
    """Past the cutoff, "a failed attempt was purged" and "nothing was ever
    queued" are indistinguishable. Saying so beats guessing."""
    alert = make_alert(notify_count=0, starts_at=datetime(2026, 1, 1, tzinfo=UTC))
    reason = empty_delivery_reason(alert, CUTOFF, 3)
    assert "không phân biệt được" in reason


def test_recent_alert_with_no_sends_was_never_sent():
    alert = make_alert(notify_count=0, starts_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert "Chưa từng gửi" in empty_delivery_reason(alert, CUTOFF, 3)


def test_with_the_purge_disabled_an_old_alert_was_simply_never_sent():
    """No cutoff means nothing has ever been deleted, so there is no
    ambiguity to admit."""
    alert = make_alert(notify_count=0, starts_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert "Chưa từng gửi" in empty_delivery_reason(alert, None, 0)


# ---------------------------------------------------------------------------
# the target is a credential
# ---------------------------------------------------------------------------

def test_signature_and_flow_id_never_survive_redaction():
    full = (
        "https://prod-07.westus.logic.azure.com/workflows/9f8e7d/triggers/"
        "manual/paths/invoke?api-version=2016-06-01&sig=SUPERSECRETVALUE"
    )
    out = redact_target(full)
    assert "sig=" not in out
    assert "SUPERSECRETVALUE" not in out
    assert "9f8e7d" not in out
    # Still enough to tell two tenants' flows apart.
    assert "prod-07.westus.logic.azure.com" in out


@pytest.mark.parametrize("value", ["", None, "not a url", "https://host"])
def test_redaction_never_raises_on_odd_values(value):
    assert isinstance(redact_target(value), str)
