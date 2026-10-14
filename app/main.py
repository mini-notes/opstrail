"""OpsTrail application entrypoint."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import (
    auth,
    labels,
    owners,
    retention_admin,
    severity_admin,
    stats,
    templates_admin,
    ui,
    users,
    webhooks,
)
from app.config import get_settings
from app.scheduler import build_scheduler
from app.security import NotAuthenticated
from app.services import dispatch, severity_rules
from app.templating import TEMPLATE_DIR  # noqa: F401 - ensures templates resolve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("opstrail")

# Known-weak values that appear in the repository: app/config.py defaults,
# docker-compose.yml fallbacks and .env.example.
PUBLISHED_SECRETS = frozenset(
    {
        "change-me-in-production",
        "dev-session-secret-change-me",
        "change-me",
        "change-me-32-chars-min",
    }
)
PUBLISHED_TOKENS = frozenset({"dev-intake-token", "change-me"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.msteams_enabled and not settings.msteams_power_automate_url:
        log.warning(
            "MSTEAMS_ENABLED is on but MSTEAMS_POWER_AUTOMATE_URL is empty -- "
            "alerts will be stored but not delivered to Teams"
        )

    # Every value here ships in the public repository, so an install using one
    # is an install anyone can forge an admin cookie for. The list covers BOTH
    # the code default and the docker-compose default -- checking only the
    # former silently misses the path most people actually take.
    if settings.session_secret in PUBLISHED_SECRETS:
        log.warning(
            "SESSION_SECRET is still the built-in default -- session cookies "
            "can be forged by anyone who has seen the source. Set it before "
            "exposing this beyond a local machine."
        )
    if not settings.intake_token:
        log.warning(
            "INTAKE_TOKEN is empty -- the alert intake endpoint accepts "
            "unauthenticated POSTs from anyone who can reach it."
        )
    elif settings.intake_token in PUBLISHED_TOKENS:
        log.warning(
            "INTAKE_TOKEN is a value published in the repository -- anyone "
            "can post fake alerts. Set it before exposing this beyond a "
            "local machine."
        )

    mode = (settings.msteams_auth or "none").lower()
    if mode == "bearer" and not settings.msteams_auth_token:
        log.warning(
            "MSTEAMS_AUTH=bearer but MSTEAMS_AUTH_TOKEN is empty -- cards "
            "will be sent without credentials."
        )
    elif mode == "entra":
        missing = [
            name for name, value in (
                ("ENTRA_CLIENT_ID", settings.entra_client_id),
                ("ENTRA_CLIENT_SECRET", settings.entra_client_secret),
                ("ENTRA_TENANT_ID", settings.entra_tenant_id or settings.entra_token_url),
            ) if not value
        ]
        if missing:
            log.warning(
                "MSTEAMS_AUTH=entra but %s not set -- cards will be sent "
                "without credentials.", ", ".join(missing),
            )
    elif mode not in ("none", "bearer", "entra"):
        log.warning("MSTEAMS_AUTH=%r is not a known mode; treating as none", mode)

    # Delivery rows are removed with their alert by ON DELETE CASCADE, so a
    # notification window at or beyond the alert window never gets to run --
    # the operator has configured something that cannot take effect.
    if 0 < settings.alert_retention_months <= settings.notification_retention_months:
        log.warning(
            "NOTIFICATION_RETENTION_MONTHS=%d is not shorter than "
            "ALERT_RETENTION_MONTHS=%d -- delivery records are already removed "
            "with their alert, so the setting has no effect.",
            settings.notification_retention_months, settings.alert_retention_months,
        )

    sweeper = asyncio.create_task(dispatch.dispatch_loop())
    # Separate from the dispatch sweeper and much slower: this one exists so
    # a severity window that opens while an alert is already firing takes
    # effect within a minute instead of waiting for Alertmanager's next
    # repeat, which is typically hours away.
    severity_sweeper = asyncio.create_task(severity_rules.reevaluate_loop())
    scheduler = build_scheduler()
    scheduler.start()
    log.info("%s started", settings.app_name)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        # Both are cancelled and both are awaited: cancel() only asks, and a
        # task that is never awaited can still be mid-transaction when the
        # process exits.
        for task in (sweeper, severity_sweeper):
            task.cancel()
        for task in (sweeper, severity_sweeper):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="OpsTrail", version="0.1.0", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret,
    max_age=_settings.session_max_age_seconds,
    same_site="lax",
    # Secure cookies only make sense once the console is served over HTTPS;
    # forcing it on a plain-HTTP on-prem install would silently break login.
    https_only=_settings.public_host.startswith("https://"),
)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(webhooks.router)
app.include_router(auth.router)
app.include_router(stats.router)
app.include_router(users.router)
app.include_router(labels.router)
app.include_router(owners.router)
app.include_router(templates_admin.router)
app.include_router(retention_admin.router)
app.include_router(severity_admin.router)
app.include_router(ui.router)


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated):
    """Send a browser to the login form instead of a raw 401 JSON body.

    API clients (Accept: application/json) still get the 401 they expect.
    """
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    return {"status": "ok"}
