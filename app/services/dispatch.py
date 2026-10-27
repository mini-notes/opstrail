"""Fan an ingested alert out to every configured channel, with retry.

Why a `notifications` table instead of fire-and-forget: when Teams is down an
operator must be able to answer "did the alert go out?" without grepping
container logs. Each attempt is recorded with its HTTP status, duration and
error, and a failed row carries `next_attempt_at` so the retry sweeper can
pick it up after a restart.
"""

import asyncio
import hashlib
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Alert,
    Notification,
    NotificationChannel,
    NotificationStatus,
    OutboundTarget,
    Service,
    Team,
    WorkerState,
)
from app.notifiers import msteams, webhook
from app.notifiers.base import DeliveryResult
from app.notifiers.render import build_context, render_card_safe
from app.services import mention, template_select

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempts: int) -> timedelta:
    """Exponential backoff with a ceiling.

    attempt 1 -> 5s, 2 -> 10s, 3 -> 20s ... capped at dispatch_max_backoff.
    Power Automate throttles under burst, so retrying tightly just burns the
    quota; a capped backoff keeps pressure low during a long outage while
    still recovering within a few minutes of the flow coming back.
    """
    settings = get_settings()
    seconds = min(
        settings.dispatch_base_backoff_seconds * (2 ** min(max(attempts - 1, 0), 20)),
        settings.dispatch_max_backoff_seconds,
    )
    return timedelta(seconds=seconds)


async def _targets(session: AsyncSession, alert: Alert) -> list[tuple[NotificationChannel, str]]:
    """Resolve which channels this alert goes to.

    A team may override the global Power Automate URL so each team gets its
    own Teams channel; otherwise the global default is used.
    """
    settings = get_settings()
    out: list[tuple[NotificationChannel, str]] = []

    if settings.msteams_enabled:
        url = settings.msteams_power_automate_url
        if alert.team_id:
            team = await session.get(Team, alert.team_id)
            if team and team.msteams_webhook_url:
                url = team.msteams_webhook_url
        if url:
            out.append((NotificationChannel.msteams, url))

    if settings.outbound_webhook_enabled and settings.outbound_webhook_url:
        out.append((NotificationChannel.webhook, settings.outbound_webhook_url))

    return out


async def _attribution(session: AsyncSession, alert: Alert) -> tuple[Service | None, Team | None]:
    """The service and team rows this alert was attributed to.

    The rows rather than their names: the card wants the display name, and
    template selection wants the key and the tier. One lookup serves both.
    """
    service = await session.get(Service, alert.service_id) if alert.service_id else None
    team = await session.get(Team, alert.team_id) if alert.team_id else None
    return service, team


async def _mention_for(session: AsyncSession, alert: Alert):
    """Owners to mention. Never raises: mentioning is an enhancement, and a
    problem here must not stop the alert from going out."""
    try:
        plan = await mention.plan_for(session, alert)
        if not plan.should_mention:
            return [], "", [o.mention_name for o in plan.owners]
        entities = mention.build_entities(plan.owners)
        if not entities:
            return [], "", [o.mention_name for o in plan.owners]
        return entities, mention.build_text(entities), [o.mention_name for o in plan.owners]
    except Exception:  # noqa: BLE001
        log.exception("mention resolution failed for alert %s; sending without it", alert.id)
        return [], "", []


async def snapshot(session: AsyncSession, alert: Alert, channel: NotificationChannel) -> dict:
    service, team = await _attribution(session, alert)
    if channel is NotificationChannel.webhook:
        return webhook.build_envelope(
            alert,
            service_name=service.name if service else None,
            team_name=team.name if team else None,
        )
    entities, text, owners = await _mention_for(session, alert)
    source, _ = await template_select.select_source(
        session, alert, channel, service=service, team=team
    )
    card, _ = render_card_safe(
        build_context(
            alert,
            service_name=service.name if service else None,
            team_name=team.name if team else None,
            detail_url=f"{get_settings().public_host.rstrip('/')}/alerts/{alert.id}",
            mention_entities=entities,
            mention_text=text,
            owner_names=owners,
        ),
        source,
    )
    return card


async def queue_for_alerts(session: AsyncSession, alert_ids: list[int]) -> list[int]:
    """Flush immutable payloads in the caller's transaction; NEVER commit here."""
    ids = []
    for alert_id in alert_ids:
        # Core UPSERT bypasses the identity map; always read the new state.
        alert = (
            await session.execute(
                select(Alert).where(Alert.id == alert_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        for channel, target in await _targets(session, alert):
            note = Notification(
                alert_id=alert.id,
                channel=channel,
                target=target,
                payload=await snapshot(session, alert, channel),
                event_snapshot={
                    "status": alert.status.value,
                    "alertname": alert.alertname,
                    "severity": alert.acting_severity,
                    "service_id": alert.service_id,
                    "starts_at": alert.starts_at.isoformat(),
                    "ends_at": alert.ends_at.isoformat() if alert.ends_at else None,
                },
                scope_team_id=alert.team_id,
                event_status=alert.status.value,
                status=NotificationStatus.pending,
                next_attempt_at=_now(),
            )
            session.add(note)
            await session.flush()
            ids.append(note.id)
    return ids


def target_key(channel: NotificationChannel, target: str) -> str:
    return hashlib.sha256(f"{channel.value}:{target}".encode()).hexdigest()


def digest_payload(notes: list[Notification], channel: NotificationChannel) -> dict:
    """Snapshot describes events as of a fixed cutoff, never live mutable state."""
    latest = {}
    for note in notes:
        latest[note.alert_id] = note
    firing = sum(n.event_status == "firing" for n in latest.values())
    resolved = sum(n.event_status == "resolved" for n in latest.values())
    unknown = len(latest) - firing - resolved
    summary = (
        f"OpsTrail backlog: {len(notes)} events / {len(latest)} alert episodes; "
        f"{firing} firing, {resolved} resolved, {unknown} unknown at snapshot. "
        f"Events {notes[0].created_at.isoformat()} — {notes[-1].created_at.isoformat()}."
    )
    links = [
        {
            "alert_id": n.alert_id,
            "status": n.event_status,
            "url": f"{get_settings().public_host.rstrip('/')}/alerts/{n.alert_id}",
            "event": n.event_snapshot,
        }
        for n in latest.values()
    ]
    if channel is NotificationChannel.webhook:
        return {
            "schema": "opstrail.digest.v1",
            "summary": summary,
            "alerts": links,
            "delivery_ids": [n.id for n in notes],
        }
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "text": summary, "wrap": True}]
        + [
            {
                "type": "TextBlock",
                "wrap": True,
                "text": f"Alert #{row['alert_id']}: {row['status']} — {row['url']}",
            }
            for row in links[:20]
        ]
        + [
            {
                "type": "TextBlock",
                "wrap": True,
                "text": "Full event membership and snapshot are in OpsTrail delivery details.",
            }
        ],
    }


@dataclass
class Claim:
    note_id: int
    key: str
    token: str


async def claim_next() -> Claim | None:
    settings = get_settings()
    now = _now()
    async with SessionLocal() as session, session.begin():
        candidates = (
            await session.execute(
                select(Notification.channel, Notification.target)
                .where(
                    Notification.status == NotificationStatus.pending,
                    Notification.digest_parent_id.is_(None),
                    Notification.next_attempt_at <= now,
                )
                .group_by(Notification.channel, Notification.target)
                .order_by(func.min(Notification.created_at), func.min(Notification.id))
                .limit(100)
            )
        ).all()
        for candidate in candidates:
            key = target_key(candidate.channel, candidate.target)
            await session.execute(
                pg_insert(OutboundTarget).values(key=key).on_conflict_do_nothing()
            )
            token = str(uuid.uuid4())
            claimed = (
                await session.execute(
                    update(OutboundTarget)
                    .where(
                        OutboundTarget.key == key,
                        or_(
                            OutboundTarget.lease_until.is_(None), OutboundTarget.lease_until <= now
                        ),
                        or_(
                            OutboundTarget.next_send_at.is_(None),
                            OutboundTarget.next_send_at <= now,
                        ),
                    )
                    .values(
                        claim_token=token,
                        lease_until=now + timedelta(seconds=settings.dispatch_lease_seconds),
                    )
                    .returning(OutboundTarget.key)
                )
            ).scalar_one_or_none()
            if not claimed:
                continue
            # Serialize this destination, including events still in backoff.
            note = (
                await session.execute(
                    select(Notification)
                    .where(
                        Notification.status == NotificationStatus.pending,
                        Notification.digest_parent_id.is_(None),
                        Notification.channel == candidate.channel,
                        Notification.target == candidate.target,
                    )
                    .order_by(Notification.created_at, Notification.id)
                    .limit(1)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if note is None or note.next_attempt_at is None or note.next_attempt_at > now:
                await session.execute(
                    update(OutboundTarget)
                    .where(OutboundTarget.key == key)
                    .values(claim_token=None, lease_until=None)
                )
                continue
            # Existing digest payload must never be regenerated on retry.
            is_digest = bool(
                note.payload
                and (
                    note.payload.get("schema") == "opstrail.digest.v1"
                    or note.payload.get("opstrail_digest")
                )
            )
            cutoff = now - timedelta(seconds=settings.backlog_digest_after_seconds)
            if not is_digest and note.created_at <= cutoff:
                members = (
                    (
                        await session.execute(
                            select(Notification)
                            .where(
                                Notification.status == NotificationStatus.pending,
                                Notification.digest_parent_id.is_(None),
                                Notification.channel == note.channel,
                                Notification.target == note.target,
                                Notification.scope_team_id.is_not_distinct_from(note.scope_team_id),
                                Notification.created_at <= cutoff,
                            )
                            .order_by(Notification.created_at, Notification.id)
                            .limit(settings.backlog_digest_max_events)
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                # A single stale event still gets a digest describing its snapshot.
                if members:
                    note.payload = digest_payload(members, note.channel)
                    note.payload["opstrail_digest"] = True
                    note.payload["delivery_ids"] = [n.id for n in members]
                    for member in members:
                        if member.id != note.id:
                            member.digest_parent_id = note.id
            if note.payload is None:
                # Existing rows before the migration have no historic snapshot.
                alert = await session.get(Alert, note.alert_id)
                note.payload = await snapshot(session, alert, note.channel)
                note.last_error = "legacy delivery: snapshot captured at first worker claim"
            note.worker_seen_at = now
            return Claim(note.id, key, token)
    return None


async def deliver_claim(claim: Claim, client: httpx.AsyncClient) -> bool:
    # No DB transaction is held during outbound I/O. Lease fencing prevents a
    # worker that lost ownership from overwriting the current owner's result.
    async with SessionLocal() as session:
        target = await session.get(OutboundTarget, claim.key)
        if target.claim_token != claim.token or target.lease_until <= _now():
            return False
        note = await session.get(Notification, claim.note_id)
        alert = await session.get(Alert, note.alert_id)
        payload, url, channel = note.payload, note.target, note.channel
    try:
        if channel is NotificationChannel.msteams:
            result = await msteams.send(alert, url, payload=payload, client=client)
        else:
            result = await webhook.send(
                alert,
                url,
                payload=payload,
                client=client,
                token=get_settings().outbound_webhook_token,
            )
    except Exception as exc:  # unexpected failure must not roll back earlier deliveries
        log.exception("delivery %s failed unexpectedly", claim.note_id)
        result = DeliveryResult(False, error=type(exc).__name__)
    async with SessionLocal() as session, session.begin():
        target = (
            await session.execute(
                select(OutboundTarget).where(OutboundTarget.key == claim.key).with_for_update()
            )
        ).scalar_one()
        if target.claim_token != claim.token:
            return False
        note = await session.get(Notification, claim.note_id)
        note.attempts += 1
        note.response_status = result.status_code
        note.duration_ms = result.duration_ms
        # Do not persist exception URLs or response bodies containing secrets.
        note.last_error = None if result.ok else f"outbound failure (HTTP {result.status_code})"
        settings = get_settings()
        terminal = (
            result.status_code is not None
            and 400 <= result.status_code < 500
            and (result.status_code not in (408, 409, 425, 429, 401, 403))
        )
        expired = _now() - note.created_at > timedelta(hours=settings.backlog_retention_hours)
        if result.ok:
            note.status, note.sent_at = NotificationStatus.sent, _now()
            note.next_attempt_at = None
            await session.execute(
                update(Alert)
                .where(Alert.id == note.alert_id)
                .values(notify_count=Alert.notify_count + 1)
            )
            await session.execute(
                update(Notification)
                .where(Notification.digest_parent_id == note.id)
                .values(status=NotificationStatus.sent, sent_at=note.sent_at, next_attempt_at=None)
            )
        elif terminal or expired:
            note.status, note.next_attempt_at = NotificationStatus.failed, None
            await session.execute(
                update(Notification)
                .where(Notification.digest_parent_id == note.id)
                .values(
                    status=NotificationStatus.failed,
                    next_attempt_at=None,
                    last_error="parent digest failed",
                )
            )
        else:
            delay = _backoff(note.attempts).total_seconds() * random.uniform(1, 1.2)
            delay = max(delay, result.retry_after_seconds or 0)
            note.next_attempt_at = _now() + timedelta(seconds=delay)
        target.claim_token, target.lease_until = None, None
        target.next_send_at = max(
            _now() + timedelta(seconds=settings.dispatch_target_interval_seconds),
            note.next_attempt_at if not result.ok and note.next_attempt_at else _now(),
        )
    return result.ok


async def dispatch_pending(limit: int = 100) -> int:
    settings = get_settings()
    sent = 0
    async with httpx.AsyncClient(timeout=settings.dispatch_timeout_seconds) as client:
        for _ in range(limit):
            claim = await claim_next()
            if claim is None:
                break
            sent += await deliver_claim(claim, client)
    return sent


async def heartbeat(name: str = "dispatch") -> None:
    async with SessionLocal() as session, session.begin():
        await session.execute(
            pg_insert(WorkerState)
            .values(
                name=name,
                updated_at=_now(),
                detail={"status": "running"},
            )
            .on_conflict_do_update(index_elements=[WorkerState.name], set_={"updated_at": _now()})
        )


async def dispatch_loop(interval_seconds: float = 1.0) -> None:
    async def lane():
        while True:
            try:
                await dispatch_pending(limit=1)
            except Exception:
                log.exception("dispatch sweep failed")
            await asyncio.sleep(interval_seconds)

    async def pulse():
        while True:
            await heartbeat()
            await asyncio.sleep(10)

    tasks = [asyncio.create_task(lane()) for _ in range(get_settings().dispatch_concurrency)]
    tasks.append(asyncio.create_task(pulse()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def requeue_failed(note_id: int) -> None:
    """Operator-only CLI recovery; preserve payload and historic timestamps."""
    from app.services import audit

    async with SessionLocal() as session, session.begin():
        note = (
            await session.execute(
                select(Notification).where(Notification.id == note_id).with_for_update()
            )
        ).scalar_one_or_none()
        if note is None or note.status is not NotificationStatus.failed:
            raise ValueError("delivery must exist and have failed status")
        if note.digest_parent_id is not None:
            raise ValueError(f"requeue parent digest #{note.digest_parent_id} instead")
        note.status, note.next_attempt_at = NotificationStatus.pending, _now()
        note.last_error = "manually requeued after operator review"
        await session.execute(
            update(Notification)
            .where(Notification.digest_parent_id == note.id)
            .values(status=NotificationStatus.pending)
        )
        await audit.record(
            session,
            None,
            "notification.requeue",
            object_type="notification",
            object_id=note.id,
            detail={"source": "operator-cli"},
        )
