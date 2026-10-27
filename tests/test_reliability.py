"""Real PostgreSQL fault, concurrency, immutable delivery and digest tests."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from jinja2 import TemplateError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Alert, Notification, NotificationStatus, OutboundTarget
from app.notifiers.render import render_source
from app.schemas.alertmanager import AlertmanagerPayload
from app.services import dispatch, ingest, retention

DSN = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test"
)
START = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no test database: {type(exc).__name__}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dispatch, "SessionLocal", maker)
    monkeypatch.setattr(retention, "SessionLocal", maker)
    settings = dispatch.get_settings().model_copy(
        update={
            "msteams_enabled": False,
            "outbound_webhook_enabled": True,
            "outbound_webhook_url": "https://receiver.test/events",
            "dispatch_target_interval_seconds": 0,
            "backlog_digest_after_seconds": 300,
        }
    )
    monkeypatch.setattr(dispatch, "get_settings", lambda: settings)
    yield maker, settings
    await engine.dispose()


def payload(status="firing", fingerprint="fp", starts=START):
    return AlertmanagerPayload.model_validate(
        {
            "alerts": [
                {
                    "fingerprint": fingerprint,
                    "status": status,
                    "startsAt": starts.isoformat(),
                    "endsAt": (starts + timedelta(minutes=5)).isoformat()
                    if status == "resolved"
                    else None,
                    "labels": {"alertname": "TestAlert", "severity": "critical"},
                }
            ]
        }
    )


async def accept(maker, status="firing", fingerprint="fp"):
    async with maker() as session:
        return await ingest.ingest_payload(session, payload(status, fingerprint))


async def counts(maker):
    async with maker() as session:
        return [
            (await session.execute(select(func.count(model.id)))).scalar_one()
            for model in (Alert, Notification)
        ]


async def test_concurrent_duplicate_intake_creates_one_delivery(db):
    maker, _ = db
    await asyncio.gather(*(accept(maker) for _ in range(12)))
    assert await counts(maker) == [1, 1]


async def test_failure_between_alert_and_queue_rolls_back(db, monkeypatch):
    maker, _ = db
    original = dispatch.queue_for_alerts

    async def fail(*args):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(dispatch, "queue_for_alerts", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await accept(maker)
    assert await counts(maker) == [0, 0]
    monkeypatch.setattr(dispatch, "queue_for_alerts", original)
    await accept(maker)
    assert await counts(maker) == [1, 1]


async def test_uncommitted_ingest_leaves_nothing(db):
    maker, _ = db
    async with maker() as session:
        await ingest.ingest_payload(session, payload(), commit=False)
    assert await counts(maker) == [0, 0]


async def test_snapshot_survives_state_change_and_multiple_events_in_batch(db):
    maker, _ = db
    batch = payload()
    batch.alerts.append(payload("resolved").alerts[0])
    async with maker() as session:
        await ingest.ingest_payload(session, batch)
    async with maker() as session:
        notes = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        assert [n.payload["status"] for n in notes] == ["firing", "resolved"]
        assert [n.event_status for n in notes] == ["firing", "resolved"]


async def test_two_workers_claim_once_and_fence_stale_owner(db):
    maker, _ = db
    await accept(maker)
    claims = await asyncio.gather(*(dispatch.claim_next() for _ in range(8)))
    active = [c for c in claims if c]
    assert len(active) == 1
    first = active[0]
    async with maker() as session, session.begin():
        await session.execute(
            update(OutboundTarget).values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = await dispatch.claim_next()
    assert second and second.token != first.token
    calls = []

    async def endpoint(request):
        calls.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        assert not await dispatch.deliver_claim(first, client)
        assert await dispatch.deliver_claim(second, client)
    assert len(calls) == 1
    assert await dispatch.claim_next() is None


async def test_digest_retry_is_frozen_and_preserves_members(db):
    maker, _ = db
    await accept(maker)
    await accept(maker, "resolved")
    async with maker() as session, session.begin():
        await session.execute(
            update(Notification).values(created_at=datetime.now(UTC) - timedelta(minutes=10))
        )
    claim = await dispatch.claim_next()
    async with maker() as session:
        note = await session.get(Notification, claim.note_id)
        frozen = note.payload
        assert frozen["schema"] == "opstrail.digest.v1"
        assert len(frozen["delivery_ids"]) == 2
        assert frozen["alerts"][0]["status"] == "resolved"
        notes = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        assert notes[0].event_snapshot["status"] == "firing"
        assert notes[1].digest_parent_id == notes[0].id
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        assert not await dispatch.deliver_claim(claim, client)
    # More events arrive while digest is waiting. It must never absorb them.
    await accept(maker, fingerprint="new")
    async with maker() as session, session.begin():
        await session.execute(
            update(Notification)
            .where(Notification.id == claim.note_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.execute(update(OutboundTarget).values(next_send_at=None))
    retry = await dispatch.claim_next()
    assert retry.note_id == claim.note_id
    async with maker() as session:
        assert (await session.get(Notification, retry.note_id)).payload == frozen
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        assert await dispatch.deliver_claim(retry, client)
    async with maker() as session:
        notes = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        assert [n.status for n in notes] == [
            NotificationStatus.sent,
            NotificationStatus.sent,
            NotificationStatus.pending,
        ]


async def test_retry_after_blocks_destination_not_other_targets(db):
    maker, settings = db
    await accept(maker)
    first = await dispatch.claim_next()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "120"})
        )
    ) as client:
        assert not await dispatch.deliver_claim(first, client)
    assert await dispatch.claim_next() is None
    settings.outbound_webhook_url = "https://other.test/events"
    await accept(maker, fingerprint="other")
    assert (await dispatch.claim_next()).note_id != first.note_id
    async with maker() as session:
        note = await session.get(Notification, first.note_id)
        assert note.next_attempt_at > datetime.now(UTC) + timedelta(seconds=110)


async def test_expired_backlog_becomes_visible_failure(db):
    maker, settings = db
    settings.backlog_retention_hours = 1
    await accept(maker)
    async with maker() as session, session.begin():
        await session.execute(
            update(Notification).values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
    claim = await dispatch.claim_next()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        await dispatch.deliver_claim(claim, client)
    async with maker() as session:
        assert (await session.get(Notification, claim.note_id)).status == NotificationStatus.failed


async def test_retention_keeps_old_firing_alert(db):
    maker, _ = db
    await accept(maker)
    async with maker() as session, session.begin():
        await session.execute(
            update(Alert).values(starts_at=datetime.now(UTC) - timedelta(days=400))
        )
    assert await retention.purge_old_alerts(months=1) == 0
    assert await counts(maker) == [1, 1]


@pytest.mark.parametrize(
    "source",
    [
        "{{ cycler.__init__.__globals__ }}",
        "{{ alert.__class__.__mro__ }}",
        '{{ "x" * 1000000000 }}',
        "{% for a in [1] %}{% for b in [1] %}{}{% endfor %}{% endfor %}",
        "{% macro run() %}{}{% endmacro %}{{ run() }}",
    ],
)
def test_unsafe_templates_rejected(source):
    with pytest.raises((TemplateError, ValueError)):
        render_source(source, {"alert": {}})


async def test_viewer_cannot_preview_template():
    from types import SimpleNamespace

    from app.db import get_session
    from app.main import app
    from app.models import UserRole
    from app.security import current_user

    async def dummy_session():
        yield None

    app.dependency_overrides[current_user] = lambda: SimpleNamespace(role=UserRole.viewer)
    app.dependency_overrides[get_session] = dummy_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/settings/template/preview", data={"body": "{}"})
            assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
