"""Acknowledging an alert: who has picked it up, and when it stops counting.

The badge beside "Firing" in the sidebar is a worklist -- it falls when a
person acts, where a plain firing count only falls when the system heals
itself. That is only true if an acknowledgement survives the things that
happen to an alert afterwards, which is what this file pins.

The sharpest of those: an Alertmanager repeat must not undo somebody's
decision. That protection is by ABSENCE -- the ingest UPSERT lists the
columns it refreshes, and the ack columns are not among them -- so it is
pinned by a test that reads the clause, not only by one that exercises it.
"""

import datetime as dt
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.ui import unacked_count
from app.db import Base
from app.models import Alert, AlertStatus, AuditLog, User, UserRole
from app.schemas.alertmanager import AlertmanagerPayload
from app.services.ingest import ingest_payload

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)
STARTS = dt.datetime(2026, 10, 14, 8, 30, tzinfo=dt.UTC)


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


def payload(status="firing", fingerprint="fp1", starts=STARTS):
    return AlertmanagerPayload.model_validate({
        "receiver": "opstrail",
        "status": status,
        "alerts": [{
            "status": status,
            "labels": {"alertname": "PaymentsLatencyHigh", "severity": "critical"},
            "annotations": {"summary": "p99 cao"},
            "startsAt": starts.isoformat(),
            "endsAt": (starts + dt.timedelta(minutes=20)).isoformat()
            if status == "resolved" else None,
            "fingerprint": fingerprint,
        }],
    })


async def row(session, fingerprint="fp1") -> Alert:
    # The UPSERT is Core SQL, so the identity map does not learn about it;
    # without expunging, a read back here returns whatever the test last set.
    session.expunge_all()
    return (
        await session.execute(select(Alert).where(Alert.fingerprint == fingerprint))
    ).scalar_one()


async def a_user(session, username="oncall") -> User:
    user = User(username=username, password_hash="x", role=UserRole.viewer)
    session.add(user)
    await session.flush()
    return user


async def ack(session, alert, user):
    alert.acked_at = dt.datetime.now(dt.UTC)
    alert.acked_by_id = user.id
    await session.commit()


# ---------------------------------------------------------------------------
# the protection that is easiest to break by accident
# ---------------------------------------------------------------------------

def test_the_upsert_does_not_refresh_the_ack_columns():
    """Reads the clause itself.

    An acknowledgement survives a repeat because `acked_at` and `acked_by_id`
    are absent from the UPSERT's set_ map -- protection by omission, which
    the next person to add a column there would undo without noticing. The
    behavioural test below would catch it, but this one says why.
    """
    import inspect

    from app.services import ingest

    source = inspect.getsource(ingest.ingest_payload)
    clause = source[source.index("on_conflict_do_update"):source.index("alert_id = ")]
    assert "acked_at" not in clause
    assert "acked_by_id" not in clause


@pytest.mark.asyncio
async def test_an_ack_survives_a_repeat(session):
    """Alertmanager re-sends every repeat_interval -- often every minute in
    the dev stack. An ack undone that often would be useless."""
    user = await a_user(session)
    await ingest_payload(session, payload())
    await ack(session, await row(session), user)

    for _ in range(3):
        await ingest_payload(session, payload())

    stored = await row(session)
    assert stored.acked_at is not None
    assert stored.acked_by_id == user.id


@pytest.mark.asyncio
async def test_resolving_keeps_the_ack_as_the_record_of_who_handled_it(session):
    user = await a_user(session)
    await ingest_payload(session, payload())
    await ack(session, await row(session), user)

    await ingest_payload(session, payload(status="resolved"))

    stored = await row(session)
    assert stored.status is AlertStatus.resolved
    assert stored.acked_by_id == user.id
    # Off the worklist because it is no longer firing, not because the ack
    # was thrown away.
    assert await unacked_count(session) == 0


@pytest.mark.asyncio
async def test_firing_again_puts_it_back_on_the_worklist(session):
    """"I am on it" referred to an episode that ended. It broke again, so it
    needs somebody again."""
    user = await a_user(session)
    await ingest_payload(session, payload())
    await ack(session, await row(session), user)
    await ingest_payload(session, payload(status="resolved"))

    await ingest_payload(session, payload(status="firing"))

    stored = await row(session)
    assert stored.acked_at is None
    assert stored.acked_by_id is None
    assert await unacked_count(session) == 1

    actions = (
        await session.execute(select(AuditLog.action).order_by(AuditLog.id))
    ).scalars().all()
    assert "alert.ack.cleared" in actions


@pytest.mark.asyncio
async def test_deleting_the_user_does_not_requeue_the_alert(session):
    """`acked_by_id` is ON DELETE SET NULL. `acked_at` is the marker, so a
    departed colleague does not silently push work back into the queue."""
    user = await a_user(session)
    await ingest_payload(session, payload())
    alert = await row(session)
    await ack(session, alert, user)

    await session.delete(user)
    await session.commit()

    stored = await row(session)
    assert stored.acked_by_id is None
    assert stored.acked_at is not None
    assert stored.is_acked is True
    assert await unacked_count(session) == 0


# ---------------------------------------------------------------------------
# the badge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_count_is_firing_and_unacked_only(session):
    user = await a_user(session)
    await ingest_payload(session, payload(fingerprint="a"))
    await ingest_payload(session, payload(fingerprint="b"))
    await ingest_payload(session, payload(fingerprint="c", status="resolved"))
    assert await unacked_count(session) == 2

    await ack(session, await row(session, "a"), user)
    assert await unacked_count(session) == 1


@pytest.mark.asyncio
async def test_a_resolved_alert_is_never_on_the_worklist(session):
    await ingest_payload(session, payload(status="resolved"))
    assert await unacked_count(session) == 0


# ---------------------------------------------------------------------------
# age
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_age_is_only_meaningful_while_firing(session):
    """Every other duration in the codebase is ends_at - starts_at. A
    resolved alert has a duration, not an age."""
    await ingest_payload(session, payload())
    assert (await row(session)).age_hours > 0

    await ingest_payload(session, payload(status="resolved"))
    assert (await row(session)).age_hours is None


# ---------------------------------------------------------------------------
# the badge and the page must not drift apart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_badge_counts_exactly_what_the_page_lists(session):
    """The predicate is written twice -- once in `unacked_count`, once in
    `_filtered_query` -- which is exactly how a badge stops matching its page
    and stops being believed."""
    from sqlalchemy import func

    from app.api.ui import _filtered_query

    user = await a_user(session)
    await ingest_payload(session, payload(fingerprint="a"))
    await ingest_payload(session, payload(fingerprint="b"))
    await ingest_payload(session, payload(fingerprint="c", status="resolved"))
    await ack(session, await row(session, "b"), user)

    listed = (
        await session.execute(
            select(func.count()).select_from(
                _filtered_query(
                    service_id=None, team_id=None, severity=None,
                    alert_status="firing", label_source=None, q=None, days=None,
                    acked=False,
                ).subquery()
            )
        )
    ).scalar_one()

    assert await unacked_count(session) == listed == 1


# ---------------------------------------------------------------------------
# who may hand an alert back
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "role,acked_by,expected",
    [
        (UserRole.viewer, 7, True),     # their own
        (UserRole.viewer, 8, False),    # somebody else's
        (UserRole.viewer, None, False),  # acker was offboarded
        (UserRole.editor, 8, True),
        (UserRole.admin, None, True),
    ],
)
def test_can_unack(role, acked_by, expected):
    """A bystander must not be able to quietly put an attended alert back on
    the worklist; the person who mis-clicked must be able to undo it."""
    from app.api.ui import can_unack

    user = User(id=7, username="u", password_hash="x", role=role)
    alert = Alert(
        id=1, fingerprint="f", starts_at=STARTS, status=AlertStatus.firing,
        labels={}, annotations={},
        acked_at=dt.datetime.now(dt.UTC), acked_by_id=acked_by,
    )
    assert can_unack(user, alert) is expected
