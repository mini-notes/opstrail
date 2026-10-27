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


async def test_digest_never_nests_an_existing_digest(db):
    maker, _ = db
    await accept(maker, fingerprint="older")
    await accept(maker, fingerprint="digest-one")
    await accept(maker, "resolved", fingerprint="digest-one")
    async with maker() as session, session.begin():
        notes = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        notes[0].status = NotificationStatus.failed
        for note in notes:
            note.created_at = datetime.now(UTC) - timedelta(minutes=10)
    claim = await dispatch.claim_next()
    async with maker() as session:
        original = (await session.get(Notification, claim.note_id)).payload
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        await dispatch.deliver_claim(claim, client)
    async with maker() as session:
        older = (
            await session.execute(select(Notification).order_by(Notification.id).limit(1))
        ).scalar_one()
    await dispatch.requeue_failed(older.id)
    async with maker() as session, session.begin():
        await session.execute(update(OutboundTarget).values(next_send_at=None))
    next_claim = await dispatch.claim_next()
    assert next_claim.note_id == older.id
    async with maker() as session:
        grouped = await session.get(Notification, older.id)
        assert grouped.payload["delivery_ids"] == [older.id]
        assert (await session.get(Notification, claim.note_id)).payload == original
        assert (await session.get(Notification, claim.note_id)).digest_parent_id is None


async def test_digest_team_scope_and_limit(db):
    maker, settings = db
    settings.backlog_digest_max_events = 2
    for index in range(5):
        await accept(maker, fingerprint=f"scope-{index}")
    async with maker() as session, session.begin():
        notes = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        for index, note in enumerate(notes):
            note.created_at = datetime.now(UTC) - timedelta(minutes=10)
            note.scope_team_id = index % 2 + 1
    claim = await dispatch.claim_next()
    async with maker() as session:
        parent = await session.get(Notification, claim.note_id)
        assert len(parent.payload["delivery_ids"]) == 2
        members = (
            (
                await session.execute(
                    select(Notification).where(Notification.id.in_(parent.payload["delivery_ids"]))
                )
            )
            .scalars()
            .all()
        )
        assert {n.scope_team_id for n in members} == {1}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        assert await dispatch.deliver_claim(claim, client)
    async with maker() as session:
        alerts = (await session.execute(select(Alert).order_by(Alert.id))).scalars().all()
        assert [a.notify_count for a in alerts] == [1, 0, 1, 0, 0]


async def test_operator_requeue_preserves_payload_and_rejects_pending(db):
    maker, _ = db
    await accept(maker)
    claim = await dispatch.claim_next()
    with pytest.raises(ValueError, match="failed"):
        await dispatch.requeue_failed(claim.note_id)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400))
    ) as client:
        await dispatch.deliver_claim(claim, client)
    async with maker() as session:
        original = (await session.get(Notification, claim.note_id)).payload
    await dispatch.requeue_failed(claim.note_id)
    async with maker() as session:
        row = await session.get(Notification, claim.note_id)
        assert row.payload == original
        assert row.status == NotificationStatus.pending
        assert row.next_attempt_at is not None


async def test_unexpected_error_does_not_rollback_previous_success(db, monkeypatch):
    maker, _ = db
    await accept(maker, fingerprint="first")
    await accept(maker, fingerprint="second")
    seen = []

    async def endpoint(request):
        seen.append(request.headers["Idempotency-Key"])
        if len(seen) == 2:
            raise RuntimeError("injected")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        assert await dispatch.deliver_claim(await dispatch.claim_next(), client)
        assert not await dispatch.deliver_claim(await dispatch.claim_next(), client)
    assert len(set(seen)) == 2
    async with maker() as session:
        rows = (
            (await session.execute(select(Notification).order_by(Notification.id))).scalars().all()
        )
        assert [n.status for n in rows] == [NotificationStatus.sent, NotificationStatus.pending]


async def test_retention_keeps_recent_resolution_and_pending_delivery(db):
    maker, _ = db
    await accept(maker)
    await accept(maker, "resolved")
    async with maker() as session, session.begin():
        await session.execute(
            update(Alert).values(
                starts_at=datetime.now(UTC) - timedelta(days=400), ends_at=datetime.now(UTC)
            )
        )
        await session.execute(update(Notification).values(status=NotificationStatus.sent))
    assert await retention.purge_old_alerts(months=1) == 0
    async with maker() as session, session.begin():
        await session.execute(update(Alert).values(ends_at=datetime.now(UTC) - timedelta(days=100)))
        await session.execute(update(Notification).values(status=NotificationStatus.pending))
    assert await retention.purge_old_alerts(months=1) == 0
    async with maker() as session, session.begin():
        await session.execute(update(Notification).values(status=NotificationStatus.sent))
    assert await retention.purge_old_alerts(months=1) == 1


async def test_ops_readiness_metrics_and_origin(db, monkeypatch):
    maker, _ = db
    from app.api import ops
    from app.main import _settings, app

    monkeypatch.setattr(ops, "SessionLocal", maker)
    settings = ops.get_settings().model_copy(update={"ops_metrics_token": "metrics-test"})
    monkeypatch.setattr(ops, "get_settings", lambda: settings)
    monkeypatch.setattr(_settings, "environment", "production")
    monkeypatch.setattr(_settings, "public_host", "https://opstrail.test")
    await dispatch.heartbeat()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://opstrail.test"
    ) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/metrics")).status_code == 401
        response = await client.get("/metrics", headers={"Authorization": "Bearer metrics-test"})
        assert response.status_code == 200
        assert "opstrail_pending_oldest_age_seconds 0" in response.text
        assert 'component="dispatch"' in response.text
        assert 'job="dispatch"' not in response.text
        assert (
            await client.post("/logout", headers={"Origin": "https://evil.test"})
        ).status_code == 403
        assert (
            await client.post("/logout", headers={"Origin": "https://opstrail.test"})
        ).status_code == 303


@pytest.mark.parametrize(
    "updates",
    [
        {"session_secret": "change-me-in-production"},
        {"intake_token": ""},
        {"ops_metrics_token": ""},
        {"public_host": "http://opstrail.test"},
        {"session_secret": "REPLACE_WITH_A_NEW_SECRET_LONGER_THAN_32_CHARS"},
        {"dispatch_lease_seconds": 1},
        {"msteams_enabled": True, "msteams_power_automate_url": ""},
    ],
)
def test_production_rejects_unsafe_configuration(updates):
    from app.config import Settings

    values = dict(
        environment="production",
        session_secret="s" * 48,
        intake_token="i" * 48,
        ops_metrics_token="m" * 48,
        public_host="https://opstrail.test",
        msteams_enabled=False,
        outbound_webhook_enabled=False,
    )
    values.update(updates)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **values).validate_runtime()


async def test_expired_inflight_owner_cannot_overwrite_new_owner(db):
    maker, _ = db
    await accept(maker)
    first = await dispatch.claim_next()
    replacement = None
    keys = []

    async def endpoint(request):
        nonlocal replacement
        keys.append(request.headers["Idempotency-Key"])
        if replacement is None:
            async with maker() as session, session.begin():
                await session.execute(
                    update(OutboundTarget).values(
                        lease_until=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
            replacement = await dispatch.claim_next()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        assert not await dispatch.deliver_claim(first, client)
        async with maker() as session:
            note = await session.get(Notification, first.note_id)
            assert note.status == NotificationStatus.pending
            assert note.attempts == 0
        assert await dispatch.deliver_claim(replacement, client)
    # HTTP can still be repeated across a lost lease; receiver can deduplicate this key.
    assert len(keys) == 2 and keys[0] == keys[1]


async def test_send_has_wall_clock_timeout_inside_lease(db):
    maker, settings = db
    settings.dispatch_timeout_seconds = 0.01
    await accept(maker)
    claim = await dispatch.claim_next()

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        assert not await dispatch.deliver_claim(claim, client)
    async with maker() as session:
        note = await session.get(Notification, claim.note_id)
        assert note.status == NotificationStatus.pending
        assert note.attempts == 1
        assert (await session.get(OutboundTarget, claim.key)).claim_token is None


async def test_readiness_handles_database_failure(monkeypatch):
    from app.api import ops

    def broken_session():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ops, "SessionLocal", broken_session)
    response = await ops.readyz()
    assert response.status_code == 503
    assert b"db unavailable" not in response.body


async def test_legacy_pending_without_retry_time_is_recoverable(db):
    maker, _ = db
    await accept(maker)
    async with maker() as session, session.begin():
        await session.execute(
            update(Notification).values(
                payload=None,
                event_status=None,
                event_snapshot=None,
                next_attempt_at=None,
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    claim = await dispatch.claim_next()
    assert claim is not None
    async with maker() as session:
        note = await session.get(Notification, claim.note_id)
        assert "1 unknown" in note.payload["summary"]
        assert "0 resolved" in note.payload["summary"]


@pytest.mark.parametrize("source", ["[]", "null", '"text"', "42"])
def test_card_requires_json_object(source):
    with pytest.raises(ValueError, match="JSON object"):
        render_source(source, {})


async def test_retention_preserves_digest_anchor_while_members_remain(db):
    maker, _ = db
    await accept(maker, fingerprint="parent")
    await accept(maker, "resolved", fingerprint="parent")
    await accept(maker, fingerprint="child")
    async with maker() as session, session.begin():
        await session.execute(
            update(Notification).values(created_at=datetime.now(UTC) - timedelta(days=200))
        )
    claim = await dispatch.claim_next()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        assert await dispatch.deliver_claim(claim, client)
    async with maker() as session, session.begin():
        parent = await session.get(Notification, claim.note_id)
        await session.execute(
            update(Alert)
            .where(Alert.id == parent.alert_id)
            .values(ends_at=datetime.now(UTC) - timedelta(days=200))
        )
        child = (
            await session.execute(
                select(Notification)
                .where(Notification.digest_parent_id == parent.id)
                .order_by(Notification.id.desc())
                .limit(1)
            )
        ).scalar_one()
        child.created_at = datetime.now(UTC)
    assert await retention.purge_old_alerts(months=1) == 0
    await retention.purge_old_notifications(months=1)
    async with maker() as session:
        assert await session.get(Notification, claim.note_id) is not None
        children = (
            (
                await session.execute(
                    select(Notification).where(Notification.digest_parent_id == claim.note_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1
