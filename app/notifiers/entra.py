"""Microsoft Entra ID tokens for calling a Power Automate flow.

A Power Automate HTTP trigger set to "Specific users in my tenant" drops the
SAS signature from its URL and instead demands an OAuth bearer token. OpsTrail
is a daemon with no signed-in user, so it authenticates as a service principal
using the client-credentials grant; the flow's allowed-callers list accepts
service principal object ids alongside user emails.

The token is cached. Fetching one per alert would be self-inflicted rate
limiting -- and during an alert storm, a burst of identical token requests is
exactly what gets a tenant throttled.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Renew slightly early so a token never expires between the check and the call
# it is used for.
EXPIRY_MARGIN_SECONDS = 60
TOKEN_TIMEOUT_SECONDS = 15.0


class EntraError(RuntimeError):
    """Token acquisition failed. The message is safe to log and to store on a
    notification row: it never carries the secret or the token."""


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.monotonic() < self.expires_at - EXPIRY_MARGIN_SECONDS


_cache: _CachedToken | None = None
# One refresh at a time. Without this an alert storm sends one token request
# per alert, all of them asking for the same thing.
_lock = asyncio.Lock()


def invalidate() -> None:
    """Drop the cached token.

    Called when Power Automate answers 401/403: the token may have been
    revoked, or the tenant rotated something, and the next send should fetch
    a fresh one rather than replay a token already known to be rejected.
    """
    global _cache
    _cache = None


def _redact(text: str) -> str:
    """Blank out the client secret wherever it appears in `text`."""
    secret = get_settings().entra_client_secret
    # A very short secret would match too much to be worth substituting.
    if secret and len(secret) >= 8:
        return text.replace(secret, "***")
    return text


def _missing_config() -> list[str]:
    settings = get_settings()
    missing = []
    if not settings.entra_client_id:
        missing.append("ENTRA_CLIENT_ID")
    if not settings.entra_client_secret:
        missing.append("ENTRA_CLIENT_SECRET")
    if not settings.entra_tenant_id and not settings.entra_token_url:
        missing.append("ENTRA_TENANT_ID")
    return missing


async def _fetch(client: httpx.AsyncClient | None = None) -> _CachedToken:
    settings = get_settings()
    missing = _missing_config()
    if missing:
        raise EntraError(f"missing config: {', '.join(missing)}")

    owns = client is None
    client = client or httpx.AsyncClient(timeout=TOKEN_TIMEOUT_SECONDS)
    try:
        resp = await client.post(
            settings.entra_token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.entra_client_id,
                "client_secret": settings.entra_client_secret,
                "scope": settings.entra_scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise EntraError(f"token endpoint unreachable: {type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()

    if resp.status_code != 200:
        # Only the named error fields, never the raw body. And redacted even
        # then: this message is logged by the sender and stored on the
        # notification row, so an endpoint that echoes the submitted secret
        # back in its error text must not be able to park it in a log file.
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('error_description', '')[:200]}"
        except Exception:  # noqa: BLE001
            detail = "unparseable error body"
        raise EntraError(
            f"token request returned {resp.status_code} -- {_redact(detail)}"
        )

    try:
        payload = resp.json()
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
    except (KeyError, ValueError, TypeError) as exc:
        raise EntraError("token response missing access_token") from exc

    log.info("entra: acquired token, valid for %ss", expires_in)
    return _CachedToken(value=token, expires_at=time.monotonic() + expires_in)


async def get_token(client: httpx.AsyncClient | None = None) -> str:
    """A usable access token, from cache when possible.

    Raises EntraError; callers decide whether that stops the send. It should
    not stop the alert from being retried -- a token problem is usually
    transient or a configuration fix away.
    """
    global _cache
    if _cache is not None and _cache.usable:
        return _cache.value

    async with _lock:
        # Someone may have refreshed it while this coroutine waited.
        if _cache is not None and _cache.usable:
            return _cache.value
        _cache = await _fetch(client)
        return _cache.value
