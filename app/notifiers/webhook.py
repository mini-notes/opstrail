"""Generic outbound webhook ("webhook other").

Posts a stable, documented JSON envelope so downstream services (an SMS
gateway, a ticketing bridge, a chat relay) do not have to parse the raw
Alertmanager payload. The original labels/annotations are passed through
untouched under `labels` / `annotations`.
"""

import logging
import time

import httpx

from app.config import get_settings
from app.models import Alert
from app.notifiers.base import DeliveryResult
from app.notifiers.render import band_severity

log = logging.getLogger(__name__)


def build_envelope(
    alert: Alert, *, service_name: str | None = None, team_name: str | None = None
) -> dict:
    settings = get_settings()
    return {
        "schema": "opstrail.alert.v1",
        "id": alert.id,
        "fingerprint": alert.fingerprint,
        "status": alert.status.value,
        "alertname": alert.alertname,
        "severity": band_severity(alert.acting_severity),
        "severity_raw": alert.severity,
        # Present only when a severity rule moved it, so a consumer can tell
        # an adjusted alert from one Prometheus called critical itself.
        "severity_effective": alert.effective_severity,
        "service": service_name,
        "team": team_name,
        "summary": alert.summary,
        "description": alert.description,
        "starts_at": alert.starts_at.isoformat() if alert.starts_at else None,
        "ends_at": alert.ends_at.isoformat() if alert.ends_at else None,
        "labels": alert.labels or {},
        "annotations": alert.annotations or {},
        "generator_url": alert.generator_url,
        "detail_url": f"{settings.public_host.rstrip('/')}/alerts/{alert.id}",
    }


async def send(
    alert: Alert,
    target_url: str,
    *,
    token: str = "",
    service_name: str | None = None,
    team_name: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> DeliveryResult:
    settings = get_settings()
    payload = build_envelope(alert, service_name=service_name, team_name=team_name)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.dispatch_timeout_seconds)
    started = time.perf_counter()
    try:
        resp = await client.post(target_url, json=payload, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        if 200 <= resp.status_code < 300:
            return DeliveryResult(True, resp.status_code, elapsed)
        return DeliveryResult(
            False,
            resp.status_code,
            elapsed,
            f"webhook returned {resp.status_code}: {resp.text[:500]}",
        )
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return DeliveryResult(False, None, elapsed, f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            await client.aclose()
