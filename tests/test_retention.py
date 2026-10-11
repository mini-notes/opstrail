"""Retention: window arithmetic, and the notification purge.

The notification purge has one requirement that outranks the rest: it must
never delete a delivery that is still waiting to be retried. Doing so drops
an alert with no record that anything was ever attempted -- the precise
failure the notifications table exists to make impossible.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Alert,
    AlertStatus,
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import retention
from app.services.retention import cutoff_for

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_default_six_months_is_about_180_days():
    assert (NOW - cutoff_for(6, now=NOW)) == timedelta(days=180)


def test_cutoff_scales_with_months():
    assert cutoff_for(1, now=NOW) > cutoff_for(12, now=NOW)


def test_cutoff_is_timezone_aware():
    """A naive cutoff compared against timestamptz raises at query time."""
    assert cutoff_for(6, now=NOW).tzinfo is not None


# ---------------------------------------------------------------------------
# notification purge, against a real Postgres (native enums, ON DELETE CASCADE)
# ---------------------------------------------------------------------------

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)


@pytest.fixture
async def session(monkeypatch):
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test database at {DSN}: {exc}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # The purge opens its own session; point it at the test database.
    monkeypatch.setattr(retention, "SessionLocal", maker)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _alert(session, fp: str, *, days_ago: float) -> Alert:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    alert = Alert(
        fingerprint=fp, starts_at=started, received_at=started,
        status=AlertStatus.firing, alertname="X", severity="critical",
        labels={}, annotations={},
    )
    session.add(alert)
    await session.flush()
    return alert


async def _note(session, alert, *, days_ago: float, status, next_attempt_at=None):
    note = Notification(
        alert_id=alert.id, channel=NotificationChannel.msteams,
        target="http://teams-mock:8080/teams", status=status,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
        next_attempt_at=next_attempt_at,
    )
    session.add(note)
    await session.flush()
    return note


async def _surviving_ids(session) -> set[int]:
    return set((await session.execute(select(Notification.id))).scalars().all())


@pytest.mark.asyncio
async def test_pending_retry_in_the_future_survives_however_old_it_is(session):
    """The requirement. An in-flight retry is a delivery that has not happened
    yet; purging it loses the alert silently."""
    alert = await _alert(session, "fp-pending", days_ago=200)
    inflight = await _note(
        session, alert, days_ago=180, status=NotificationStatus.pending,
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    await session.commit()

    removed = await retention.purge_old_notifications(months=3)

    assert removed == 0
    assert await _surviving_ids(session) == {inflight.id}


@pytest.mark.asyncio
async def test_pending_whose_retry_is_overdue_is_purged(session):
    """dispatch gives up on age after an hour, so a months-old pending row
    whose next attempt is in the past is dead, not in flight."""
    alert = await _alert(session, "fp-stuck", days_ago=200)
    await _note(
        session, alert, days_ago=180, status=NotificationStatus.pending,
        next_attempt_at=datetime.now(UTC) - timedelta(days=170),
    )
    await session.commit()

    assert await retention.purge_old_notifications(months=3) == 1
    assert await _surviving_ids(session) == set()


@pytest.mark.asyncio
async def test_pending_with_no_next_attempt_is_purged(session):
    alert = await _alert(session, "fp-null", days_ago=200)
    await _note(session, alert, days_ago=180, status=NotificationStatus.pending)
    await session.commit()

    assert await retention.purge_old_notifications(months=3) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [NotificationStatus.sent, NotificationStatus.failed])
async def test_finished_rows_past_the_cutoff_are_purged(session, status):
    alert = await _alert(session, f"fp-{status.value}", days_ago=200)
    await _note(session, alert, days_ago=180, status=status)
    await session.commit()

    assert await retention.purge_old_notifications(months=3) == 1


@pytest.mark.asyncio
async def test_recent_rows_are_kept_whatever_their_status(session):
    alert = await _alert(session, "fp-recent", days_ago=1)
    kept = [
        (await _note(session, alert, days_ago=1, status=s)).id
        for s in (NotificationStatus.sent, NotificationStatus.failed,
                  NotificationStatus.pending)
    ]
    await session.commit()

    assert await retention.purge_old_notifications(months=3) == 0
    assert await _surviving_ids(session) == set(kept)


@pytest.mark.asyncio
async def test_the_alert_itself_is_untouched(session):
    """Deliveries age out faster than alerts; the alert must stay."""
    alert = await _alert(session, "fp-keep-alert", days_ago=200)
    await _note(session, alert, days_ago=180, status=NotificationStatus.sent)
    await session.commit()

    await retention.purge_old_notifications(months=3)

    assert (await session.execute(select(func.count(Alert.id)))).scalar_one() == 1


@pytest.mark.asyncio
async def test_batching_terminates_and_removes_everything(session, monkeypatch):
    """Guards the `while True`: a batch size smaller than the backlog must
    still drain it rather than loop or stop early."""
    monkeypatch.setattr(retention, "BATCH_SIZE", 10)
    alert = await _alert(session, "fp-many", days_ago=200)
    for _ in range(25):
        await _note(session, alert, days_ago=180, status=NotificationStatus.sent)
    await session.commit()

    assert await retention.purge_old_notifications(months=3) == 25
    assert await _surviving_ids(session) == set()


@pytest.mark.asyncio
async def test_zero_months_disables_the_purge(session):
    alert = await _alert(session, "fp-disabled", days_ago=400)
    await _note(session, alert, days_ago=400, status=NotificationStatus.sent)
    await session.commit()

    assert await retention.purge_old_notifications(months=0) == 0
    assert len(await _surviving_ids(session)) == 1


@pytest.mark.asyncio
async def test_preview_counts_exactly_what_a_run_removes(session):
    """The admin page shows this number next to a button that deletes. If the
    preview were computed a different way from the purge, it would be a
    preview that lies about what the button does."""
    alert = await _alert(session, "fp-preview", days_ago=200)
    for _ in range(4):
        await _note(session, alert, days_ago=180, status=NotificationStatus.sent)
    # Not purgeable: an in-flight retry, however old.
    await _note(
        session, alert, days_ago=180, status=NotificationStatus.pending,
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )
    # Not purgeable: recent.
    await _note(session, alert, days_ago=1, status=NotificationStatus.failed)
    await session.commit()

    before = await retention.preview()
    assert before["notifications_due"] == 4
    assert before["notifications_inflight"] == 1

    assert await retention.purge_old_notifications() == before["notifications_due"]

    after = await retention.preview()
    assert after["notifications_due"] == 0
    assert after["notifications_total"] == 2
