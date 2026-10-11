"""Fan an ingested alert out to every configured channel, with retry.

Why a `notifications` table instead of fire-and-forget: when Teams is down an
operator must be able to answer "did the alert go out?" without grepping
container logs. Each attempt is recorded with its HTTP status, duration and
error, and a failed row carries `next_attempt_at` so the retry sweeper can
pick it up after a restart.
"""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Alert,
    Notification,
    NotificationChannel,
    NotificationStatus,
    Service,
    Team,
)
from app.notifiers import msteams, webhook
from app.notifiers.base import DeliveryResult
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
        settings.dispatch_base_backoff_seconds * (2 ** max(attempts - 1, 0)),
        settings.dispatch_max_backoff_seconds,
    )
    return timedelta(seconds=seconds)


def _exhausted(note: Notification) -> bool:
    """Terminal only once the notification is OLDER than the give-up window.

    Bounding by attempt count instead would collapse to a ~30s tolerance as
    soon as backoff grows -- long enough for a container restart, nowhere
    near long enough for a real Teams incident.
    """
    settings = get_settings()
    created = note.created_at
    if created is None:
        return note.attempts >= settings.dispatch_max_attempts
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    too_old = _now() - created > timedelta(minutes=settings.dispatch_give_up_after_minutes)
    return too_old or note.attempts >= settings.dispatch_max_attempts


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


async def _attribution(
    session: AsyncSession, alert: Alert
) -> tuple[Service | None, Team | None]:
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


async def _deliver(
    session: AsyncSession,
    alert: Alert,
    note: Notification,
    service: Service | None,
    team: Team | None,
    client: httpx.AsyncClient,
) -> tuple[DeliveryResult, str | None]:
    """Send, and report any note about how the send was degraded."""
    settings = get_settings()
    service_name = service.name if service else None
    team_name = team.name if team else None

    if note.channel is NotificationChannel.msteams:
        entities, text, owner_names = await _mention_for(session, alert)
        source, select_note = await template_select.select_source(
            session, alert, note.channel, service=service, team=team
        )
        result = await msteams.send(
            alert,
            note.target,
            service_name=service_name,
            team_name=team_name,
            mention_entities=entities,
            mention_text=text,
            owner_names=owner_names,
            template_source=source,
            client=client,
        )
        return result, select_note

    return (
        await webhook.send(
            alert,
            note.target,
            token=settings.outbound_webhook_token,
            service_name=service_name,
            team_name=team_name,
            client=client,
        ),
        None,
    )


async def _attempt(session: AsyncSession, note: Notification, client: httpx.AsyncClient) -> None:
    alert = await session.get(Alert, note.alert_id)
    if alert is None:
        note.status = NotificationStatus.failed
        note.last_error = "alert row disappeared"
        return

    service, team = await _attribution(session, alert)
    result, select_note = await _deliver(session, alert, note, service, team, client)
    if select_note:
        # Rides out beside the delivery result the same way a template
        # fallback does, so a degraded choice is visible on /deliveries
        # rather than only in the logs.
        result = replace(
            result,
            error="; ".join(filter(None, (select_note, result.error))),
        )

    note.attempts += 1
    note.response_status = result.status_code
    note.duration_ms = result.duration_ms

    if result.ok:
        note.status = NotificationStatus.sent
        note.sent_at = _now()
        # `error` on a successful result means a degraded send (e.g. a custom
        # template failed and the built-in was used). Keep it visible.
        note.last_error = (result.error or None) and result.error[:2000]
        note.next_attempt_at = None
        alert.notify_count = (alert.notify_count or 0) + 1
    else:
        note.last_error = (result.error or "")[:2000]
        if _exhausted(note):
            note.status = NotificationStatus.failed
            note.next_attempt_at = None
            log.error(
                "notification %s to %s gave up after %d attempts over %s: %s",
                note.id, note.channel.value, note.attempts,
                _now() - (note.created_at or _now()), note.last_error,
            )
        else:
            note.status = NotificationStatus.pending
            note.next_attempt_at = _now() + _backoff(note.attempts)
            log.warning(
                "notification %s to %s failed (attempt %d), retrying at %s: %s",
                note.id, note.channel.value, note.attempts,
                note.next_attempt_at, note.last_error,
            )


async def queue_for_alerts(session: AsyncSession, alert_ids: list[int]) -> list[int]:
    """Create pending notification rows. Called inside the request, before
    the response, so an alert can never be acknowledged without a delivery
    record existing for it."""
    note_ids: list[int] = []
    for alert_id in alert_ids:
        alert = await session.get(Alert, alert_id)
        if alert is None:
            continue
        for channel, target in await _targets(session, alert):
            note = Notification(
                alert_id=alert_id,
                channel=channel,
                target=target,
                status=NotificationStatus.pending,
                next_attempt_at=_now(),
            )
            session.add(note)
            await session.flush()
            note_ids.append(note.id)
    await session.commit()
    return note_ids


async def dispatch_pending(limit: int = 100) -> int:
    """Deliver every due notification. Runs as a background task after
    intake and again on a timer, which is what makes delivery survive a
    restart mid-retry."""
    sent = 0
    async with SessionLocal() as session:
        due = (
            (
                await session.execute(
                    select(Notification)
                    .where(
                        Notification.status == NotificationStatus.pending,
                        Notification.next_attempt_at <= _now(),
                    )
                    .order_by(Notification.next_attempt_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not due:
            return 0

        settings = get_settings()
        async with httpx.AsyncClient(timeout=settings.dispatch_timeout_seconds) as client:
            for note in due:
                await _attempt(session, note, client)
                if note.status is NotificationStatus.sent:
                    sent += 1
        await session.commit()
    return sent


async def dispatch_loop(interval_seconds: float = 10.0) -> None:
    """Background sweeper started on app startup."""
    while True:
        try:
            await dispatch_pending()
        except Exception:  # noqa: BLE001 - a sweeper must never die
            log.exception("dispatch sweep failed")
        await asyncio.sleep(interval_seconds)
