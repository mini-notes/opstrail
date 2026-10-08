"""What the sender presents to Power Automate, in each auth mode.

The point worth stating up front, because it is the easiest thing to get
wrong: sending a header protects nothing unless the FLOW requires it. These
tests cover OpsTrail's half -- that the right header arrives, and that a
credential problem never stops an alert.

That last property is deliberate and is tested here rather than assumed. If
OpsTrail refused to send when it could not get a token, a misconfigured
client secret would turn into silence during an incident -- the one moment
the tool exists for.
"""

import datetime as dt
import logging

import httpx
import pytest

from app.config import get_settings
from app.models import Alert, AlertStatus
from app.notifiers import entra, msteams

URL = "https://prod-00.westus.logic.azure.com/workflows/x/triggers/manual/paths/invoke"
TOKEN = "mock.access.token"
STATIC = "static-shared-secret-32-characters"


def make_alert() -> Alert:
    return Alert(
        id=1,
        fingerprint="fp1",
        starts_at=dt.datetime(2026, 10, 7, 8, 30, tzinfo=dt.UTC),
        ends_at=None,
        status=AlertStatus.firing,
        alertname="PostgresqlDown",
        severity="critical",
        summary="Postgres down",
        description=None,
        labels={},
        annotations={},
        generator_url=None,
        external_url=None,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("MSTEAMS_AUTH", "MSTEAMS_AUTH_TOKEN", "MSTEAMS_AUTH_HEADER",
                "MSTEAMS_AUTH_SCHEME", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID",
                "ENTRA_CLIENT_SECRET", "ENTRA_TOKEN_URL"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    entra.invalidate()
    yield
    get_settings.cache_clear()
    entra.invalidate()


def recorder(*, flow_status=202, token_status=200):
    """Transport standing in for both the flow and the token endpoint."""
    seen = {"flow": [], "token": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/token" in request.url.path:
            seen["token"].append(request)
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": TOKEN, "expires_in": 3600})
        seen["flow"].append(request)
        return httpx.Response(flow_status, json={"ok": True})

    return seen, httpx.MockTransport(handler)


def use_entra(monkeypatch):
    monkeypatch.setenv("MSTEAMS_AUTH", "entra")
    monkeypatch.setenv("ENTRA_CLIENT_ID", "client-guid")
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "secret-value-long-enough")
    monkeypatch.setenv("ENTRA_TOKEN_URL", "https://login.example/token")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# none -- the existing behaviour, unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_sends_no_authorization_header():
    """Proof the default install is untouched by any of this."""
    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok
    assert len(seen["flow"]) == 1
    assert "authorization" not in {k.lower() for k in seen["flow"][0].headers}
    assert seen["token"] == []


# ---------------------------------------------------------------------------
# bearer -- a static shared secret
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bearer_sends_the_token(monkeypatch):
    monkeypatch.setenv("MSTEAMS_AUTH", "bearer")
    monkeypatch.setenv("MSTEAMS_AUTH_TOKEN", STATIC)
    get_settings.cache_clear()

    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok
    assert seen["flow"][0].headers["Authorization"] == f"Bearer {STATIC}"


@pytest.mark.asyncio
async def test_custom_header_and_scheme(monkeypatch):
    """A gateway in front of Power Automate may want X-Api-Key with no prefix."""
    monkeypatch.setenv("MSTEAMS_AUTH", "bearer")
    monkeypatch.setenv("MSTEAMS_AUTH_TOKEN", STATIC)
    monkeypatch.setenv("MSTEAMS_AUTH_HEADER", "X-Api-Key")
    monkeypatch.setenv("MSTEAMS_AUTH_SCHEME", "")
    get_settings.cache_clear()

    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        await msteams.send(make_alert(), URL, client=client)

    assert seen["flow"][0].headers["X-Api-Key"] == STATIC
    assert "authorization" not in {k.lower() for k in seen["flow"][0].headers}


@pytest.mark.asyncio
async def test_bearer_without_a_token_still_delivers(monkeypatch, caplog):
    """Misconfiguration must not become silence during an incident."""
    monkeypatch.setenv("MSTEAMS_AUTH", "bearer")
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING)

    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok, "the card must go out even with no credential to present"
    assert len(seen["flow"]) == 1
    assert "MSTEAMS_AUTH_TOKEN" in caplog.text


# ---------------------------------------------------------------------------
# entra
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entra_fetches_then_presents_a_token(monkeypatch):
    use_entra(monkeypatch)
    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok
    assert len(seen["token"]) == 1
    assert seen["flow"][0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_token_reused_across_five_alerts(monkeypatch):
    use_entra(monkeypatch)
    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(5):
            await msteams.send(make_alert(), URL, client=client)

    assert len(seen["flow"]) == 5
    assert len(seen["token"]) == 1


@pytest.mark.asyncio
async def test_401_refreshes_the_token_and_retries_once(monkeypatch):
    """A token can expire between being cached and being used. One extra round
    trip beats making the alert wait out the dispatch backoff."""
    use_entra(monkeypatch)
    seen = {"flow": [], "token": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/token" in request.url.path:
            seen["token"].append(request)
            n = len(seen["token"])
            return httpx.Response(
                200, json={"access_token": f"{TOKEN}.{n}", "expires_in": 3600})
        seen["flow"].append(request)
        # Reject the first presentation, accept the second.
        return httpx.Response(202 if len(seen["flow"]) > 1 else 401, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok, "the alert must arrive without waiting for a retry cycle"
    assert len(seen["token"]) == 2, "the rejected token must not be replayed"
    assert seen["flow"][1].headers["Authorization"] == f"Bearer {TOKEN}.2"


@pytest.mark.asyncio
async def test_retry_happens_only_once(monkeypatch):
    """A flow that always answers 401 must not spin here."""
    use_entra(monkeypatch)
    seen, transport = recorder(flow_status=401)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert not result.ok
    assert len(seen["flow"]) == 2


@pytest.mark.asyncio
async def test_token_failure_still_delivers_the_card(monkeypatch, caplog):
    use_entra(monkeypatch)
    caplog.set_level(logging.WARNING)
    seen, transport = recorder(token_status=401)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok
    assert len(seen["flow"]) == 1
    assert "authorization" not in {k.lower() for k in seen["flow"][0].headers}
    assert "entra token unavailable" in caplog.text


@pytest.mark.asyncio
async def test_unknown_mode_warns_and_delivers(monkeypatch, caplog):
    monkeypatch.setenv("MSTEAMS_AUTH", "oauth2")  # plausible but not a mode
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING)

    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(make_alert(), URL, client=client)

    assert result.ok
    assert "oauth2" in caplog.text


@pytest.mark.asyncio
async def test_no_credential_reaches_the_logs(monkeypatch, caplog):
    use_entra(monkeypatch)
    caplog.set_level(logging.DEBUG)
    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        await msteams.send(make_alert(), URL, client=client)

    assert TOKEN not in caplog.text
    assert "secret-value-long-enough" not in caplog.text


@pytest.mark.asyncio
async def test_auth_is_not_attempted_for_a_retired_connector_url(monkeypatch):
    """The legacy-host refusal comes first; no token is spent on a dead URL."""
    use_entra(monkeypatch)
    seen, transport = recorder()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await msteams.send(
            make_alert(), "https://x.webhook.office.com/webhookb2/abc", client=client)

    assert not result.ok
    assert seen["token"] == []
