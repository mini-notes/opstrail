"""Deliver an alert to MS Teams through a Power Automate HTTP trigger.

Microsoft retired the Office 365 connector webhooks (`*.webhook.office.com`),
so the only supported target is a Power Automate flow whose trigger is
"When a Teams webhook request is received", followed by a
"Post card in a chat or channel" action posting as the Flow bot.
"""

import logging
import time

import httpx

from app.config import get_settings
from app.models import Alert
from app.notifiers import entra
from app.notifiers.base import DeliveryResult, retry_after
from app.notifiers.render import build_context, render_card_safe

log = logging.getLogger(__name__)

LEGACY_CONNECTOR_HOST = "webhook.office.com"


async def _auth_headers(client: httpx.AsyncClient | None) -> tuple[dict, str | None]:
    """Credentials to present to the flow, and a note if we could not get any.

    A failure here is reported, not raised: the card still goes out. If the
    flow requires authentication it will answer 401 and the normal retry path
    handles it -- which is a better outcome than OpsTrail deciding on its own
    that an alert should not be delivered.
    """
    settings = get_settings()
    mode = (settings.msteams_auth or "none").lower()

    if mode == "none":
        return {}, None

    if mode == "bearer":
        if not settings.msteams_auth_token:
            return {}, "msteams_auth=bearer but MSTEAMS_AUTH_TOKEN is empty"
        value = f"{settings.msteams_auth_scheme} {settings.msteams_auth_token}".strip()
        return {settings.msteams_auth_header: value}, None

    if mode == "entra":
        try:
            token = await entra.get_token(client)
        except entra.EntraError as exc:
            return {}, f"entra token unavailable: {exc}"
        return {settings.msteams_auth_header: f"Bearer {token}"}, None

    return {}, f"unknown msteams_auth mode {mode!r}"


async def send(
    alert: Alert,
    target_url: str,
    *,
    service_name: str | None = None,
    team_name: str | None = None,
    mention_entities: list[dict] | None = None,
    mention_text: str | None = None,
    owner_names: list[str] | None = None,
    template_source: str | None = None,
    client: httpx.AsyncClient | None = None,
    payload: dict | None = None,
    delivery_key: str | None = None,
) -> DeliveryResult:
    settings = get_settings()

    if LEGACY_CONNECTOR_HOST in target_url:
        # Fail loudly rather than posting a card the retired connector will
        # drop. Nudge the operator to the Power Automate flow instead.
        return DeliveryResult(
            ok=False,
            error=(
                "target is a retired Office 365 connector webhook "
                f"({LEGACY_CONNECTOR_HOST}); use a Power Automate HTTP trigger URL"
            ),
        )

    detail_url = f"{settings.public_host.rstrip('/')}/alerts/{alert.id}"
    context = build_context(
        alert,
        service_name=service_name,
        team_name=team_name,
        detail_url=detail_url,
        mention_entities=mention_entities,
        mention_text=mention_text,
        owner_names=owner_names,
    )
    try:
        card, fallback_reason = (
            (payload, None) if payload is not None else render_card_safe(context, template_source)
        )
    except Exception as exc:  # noqa: BLE001
        # Even the built-in template failed. Not retryable -- a retry would
        # render the same broken thing -- so report it and stop.
        return DeliveryResult(ok=False, error=f"render failed: {exc}")

    # Internal digest membership belongs in DB, not the Adaptive Card schema.
    card = {
        key: value for key, value in card.items() if key not in {"opstrail_digest", "delivery_ids"}
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.dispatch_timeout_seconds)
    started = time.perf_counter()
    try:
        headers, auth_note = await _auth_headers(client)
        if auth_note:
            log.warning("msteams: %s", auth_note.split(": ", 1)[0])

        if delivery_key:
            headers["X-OpsTrail-Delivery-Id"] = delivery_key
        resp = await client.post(target_url, json=card, headers=headers)

        # A token can expire between being cached and being used, and a
        # tenant can rotate credentials underneath us. Both self-correct on
        # the next token, so spend one extra round trip here rather than
        # making the alert wait out the dispatch backoff.
        if resp.status_code in (401, 403) and get_settings().msteams_auth == "entra":
            log.info("msteams: %s from flow, refreshing token and retrying once", resp.status_code)
            entra.invalidate()
            retry_headers, retry_note = await _auth_headers(client)
            if not retry_note:
                if delivery_key:
                    retry_headers["X-OpsTrail-Delivery-Id"] = delivery_key
                resp = await client.post(target_url, json=card, headers=retry_headers)

        elapsed = (time.perf_counter() - started) * 1000
        if 200 <= resp.status_code < 300:
            # A fallback still counts as delivered; the reason is surfaced so
            # the broken template is visible instead of silently tolerated.
            note = fallback_reason or auth_note
            return DeliveryResult(True, resp.status_code, elapsed, note)
        return DeliveryResult(
            False,
            resp.status_code,
            elapsed,
            f"MS Teams returned {resp.status_code}: {resp.text[:500]}",
            retry_after_seconds=retry_after(resp),
        )
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return DeliveryResult(False, None, elapsed, f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            await client.aclose()
