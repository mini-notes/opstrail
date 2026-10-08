"""Token acquisition: caching, concurrency, and what never reaches the logs.

Three properties carry real weight here:

* a token is reused until it is nearly expired -- fetching one per alert is
  self-inflicted rate limiting, and an alert storm would make it a burst of
  identical requests;
* concurrent sends share ONE refresh, not one each;
* neither the client secret nor the token ever appears in a log record. A
  secret in a log file is a secret that has leaked, even on-premises.
"""

import asyncio
import logging

import httpx
import pytest

from app.config import get_settings
from app.notifiers import entra

SECRET = "super-secret-value-do-not-log"
TOKEN = "mock.access.token.value"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENTRA_TENANT_ID", "tenant-guid")
    monkeypatch.setenv("ENTRA_CLIENT_ID", "client-guid")
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("ENTRA_TOKEN_URL", "http://mock-entra:8081/token")
    get_settings.cache_clear()
    entra.invalidate()
    yield
    get_settings.cache_clear()
    entra.invalidate()


def _transport(calls: list, *, expires_in=3600, status=200, body=None):
    """An httpx transport that records every token request it serves."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json=body or {"error": "invalid_client"})
        return httpx.Response(
            200,
            json={"access_token": TOKEN, "expires_in": expires_in,
                  "token_type": "Bearer"},
        )

    return httpx.MockTransport(handler)


def _client(calls, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport(calls, **kw))


# ---------------------------------------------------------------------------
# the happy path, and the request Entra actually receives
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sends_client_credentials_grant():
    calls: list[httpx.Request] = []
    async with _client(calls) as client:
        assert await entra.get_token(client) == TOKEN

    assert len(calls) == 1
    body = calls[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=client-guid" in body
    # The double slash is load-bearing: a single slash yields a token that
    # Power Automate then rejects with MisMatchingOAuthClaims.
    assert get_settings().entra_scope.endswith("//.default")


@pytest.mark.asyncio
async def test_token_is_cached_across_sends():
    calls: list[httpx.Request] = []
    async with _client(calls) as client:
        for _ in range(5):
            assert await entra.get_token(client) == TOKEN

    assert len(calls) == 1, "five sends must not mean five token requests"


@pytest.mark.asyncio
async def test_near_expiry_token_is_refreshed():
    # Shorter than the 60s renewal margin, so it is never considered usable.
    calls: list[httpx.Request] = []
    async with _client(calls, expires_in=30) as client:
        await entra.get_token(client)
        await entra.get_token(client)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_invalidate_forces_a_fresh_fetch():
    calls: list[httpx.Request] = []
    async with _client(calls) as client:
        await entra.get_token(client)
        entra.invalidate()
        await entra.get_token(client)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_concurrent_sends_share_one_refresh():
    """An alert storm must not become a token storm."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": TOKEN, "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(*(entra.get_token(client) for _ in range(20)))

    assert results == [TOKEN] * 20
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_config_names_the_variable():
    calls: list[httpx.Request] = []
    import os
    os.environ.pop("ENTRA_CLIENT_SECRET")
    get_settings.cache_clear()

    async with _client(calls) as client:
        with pytest.raises(entra.EntraError, match="ENTRA_CLIENT_SECRET"):
            await entra.get_token(client)

    assert calls == [], "no point calling the endpoint with no credentials"


@pytest.mark.asyncio
async def test_rejected_credentials_surface_entra_error_code():
    calls: list[httpx.Request] = []
    async with _client(
        calls,
        status=401,
        body={"error": "invalid_client",
              "error_description": "AADSTS7000215: Invalid client secret."},
    ) as client:
        with pytest.raises(entra.EntraError) as exc:
            await entra.get_token(client)

    assert "401" in str(exc.value)
    assert "invalid_client" in str(exc.value)


@pytest.mark.asyncio
async def test_unreachable_endpoint_does_not_cache_anything():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(entra.EntraError, match="unreachable"):
            await entra.get_token(client)

    assert entra._cache is None


@pytest.mark.asyncio
async def test_response_without_access_token_is_an_error():
    def handler(request):
        return httpx.Response(200, json={"expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(entra.EntraError, match="access_token"):
            await entra.get_token(client)


# ---------------------------------------------------------------------------
# secrets must not reach the logs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_neither_secret_nor_token_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []
    async with _client(calls) as client:
        await entra.get_token(client)

    text = caplog.text
    assert SECRET not in text
    assert TOKEN not in text


@pytest.mark.asyncio
async def test_secret_is_not_logged_on_failure(caplog):
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []
    async with _client(
        calls, status=401,
        # A body that echoes the secret back -- some endpoints do. Logging the
        # raw body would leak it, which is why only `error` fields are logged.
        body={"error": "invalid_client", "error_description": f"bad: {SECRET}",
              "raw": SECRET},
    ) as client:
        with pytest.raises(entra.EntraError):
            await entra.get_token(client)

    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_error_message_itself_is_redacted():
    """The message is logged by the sender and stored on the notification
    row, so it must be safe even when the endpoint echoes the secret back."""
    calls: list[httpx.Request] = []
    async with _client(
        calls, status=400,
        body={"error": "invalid_client", "error_description": f"rejected {SECRET}"},
    ) as client:
        with pytest.raises(entra.EntraError) as exc:
            await entra.get_token(client)

    assert SECRET not in str(exc.value)
    assert "***" in str(exc.value)
