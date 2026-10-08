"""Runtime configuration, read from the environment (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Deliberately `.env.local`, NOT `.env`: docker compose also reads `.env`
    # from the project root for ${VAR} interpolation, so a host-scoped value
    # there (http://localhost:8080) silently overrides the in-network service
    # name and notifications are posted to the wrong address. Keep the two
    # files separate:
    #   .env        -> docker compose interpolation (service names)
    #   .env.local  -> running uvicorn directly on the host
    # Real environment variables always win over either file.
    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    # --- app ---
    app_name: str = "OpsTrail"
    debug: bool = False
    public_host: str = "http://localhost:8000"

    # --- database ---
    # asyncpg driver; alembic rewrites this to psycopg for sync migrations.
    database_url: str = (
        "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail"
    )

    # --- intake auth ---
    # Alertmanager sends this as `Authorization: Bearer <token>`
    # (configure under the receiver's http_config.authorization).
    # Empty disables the check -- only acceptable on a trusted network.
    intake_token: str = ""

    # --- session auth (admin UI / API) ---
    session_secret: str = "change-me-in-production"
    session_max_age_seconds: int = 8 * 60 * 60

    # --- MS Teams ---
    # Power Automate HTTP trigger URL. Office 365 connector webhooks
    # (webhook.office.com) are retired by Microsoft and NOT supported.
    msteams_enabled: bool = True
    msteams_power_automate_url: str = ""
    msteams_template: str = "msteams/default.json.j2"
    # Which identifier goes into an Adaptive Card mention. Teams tenants
    # differ: some resolve the UPN/email, others only the AAD object id. If a
    # card shows the right name but does not notify the person, switch this.
    msteams_mention_id_field: str = "upn"   # upn | aad

    # --- authenticating the call to Power Automate ---
    # The trigger URL alone is a bearer credential: whoever has it can post
    # into the channel. These modes let the flow demand proof of identity on
    # top of it. NOTE: sending a header only helps if the FLOW is configured
    # to require it -- see docs/msteams-auth.md.
    #   none   : URL only (default; unchanged behaviour)
    #   bearer : a static secret, for a flow that checks a header or for an
    #            API gateway sitting in front
    #   entra  : OAuth client credentials against Microsoft Entra ID, which is
    #            what "Specific users in my tenant" on the trigger expects
    msteams_auth: str = "none"
    msteams_auth_token: str = ""
    # A gateway may want its own header name instead of Authorization.
    msteams_auth_header: str = "Authorization"
    msteams_auth_scheme: str = "Bearer"

    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    # The double slash is NOT a typo. Power Automate rejects the token with
    # MisMatchingOAuthClaims when the scope is written with a single slash.
    entra_scope: str = "https://service.flow.microsoft.com//.default"
    # Empty derives the real Entra endpoint from the tenant id. Set it to
    # point at a local mock when demonstrating the flow without a tenant.
    entra_token_url: str = ""

    @property
    def entra_token_endpoint(self) -> str:
        if self.entra_token_url:
            return self.entra_token_url
        return (
            f"https://login.microsoftonline.com/{self.entra_tenant_id}"
            "/oauth2/v2.0/token"
        )
    # Mention owners from this severity upwards, when a service or team has
    # not set its own threshold.
    mention_severity_default: str = "critical"

    # --- generic outbound webhook ("webhook other") ---
    outbound_webhook_enabled: bool = False
    outbound_webhook_url: str = ""
    outbound_webhook_token: str = ""

    # --- dispatch ---
    # Give up on AGE, not attempt count: a Power Automate outage or a Teams
    # throttle lasts minutes to hours, and an attempt budget silently turns
    # into a ~30s tolerance once backoff is applied.
    dispatch_give_up_after_minutes: int = 60
    dispatch_max_attempts: int = 50  # safety net only; age is the real bound
    dispatch_base_backoff_seconds: float = 5.0
    dispatch_max_backoff_seconds: float = 300.0
    dispatch_timeout_seconds: float = 15.0

    # --- retention ---
    alert_retention_months: int = 6
    retention_hour_utc: int = 3  # daily purge time

    @property
    def sync_database_url(self) -> str:
        """Alembic runs migrations synchronously, so swap the async driver
        for psycopg. Stated explicitly rather than stripping the suffix:
        a bare `postgresql://` lets SQLAlchemy pick a default driver, which
        changed between 2.0 (psycopg2) and 2.1 (psycopg)."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
