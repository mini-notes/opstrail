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
    environment: str = "development"
    run_background_tasks: bool = True
    dispatch_lease_seconds: int = 120
    dispatch_concurrency: int = 8
    dispatch_target_interval_seconds: float = 1.0
    backlog_digest_after_seconds: int = 300
    backlog_digest_max_events: int = 100
    backlog_retention_hours: int = 168
    ops_metrics_token: str = ""
    public_host: str = "http://localhost:8000"

    # --- database ---
    # asyncpg driver; alembic rewrites this to psycopg for sync migrations.
    database_url: str = "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail"

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
    msteams_mention_id_field: str = "upn"  # upn | aad

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
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/oauth2/v2.0/token"

    # Mention owners from this severity upwards, when a service or team has
    # not set its own threshold.
    mention_severity_default: str = "critical"

    # --- generic outbound webhook ("webhook other") ---
    outbound_webhook_enabled: bool = False
    outbound_webhook_url: str = ""
    outbound_webhook_token: str = ""

    # --- dispatch ---
    # Deprecated compatibility fields: durable workers use BACKLOG_RETENTION_HOURS.
    dispatch_give_up_after_minutes: int = 60
    dispatch_max_attempts: int = 50
    dispatch_base_backoff_seconds: float = 5.0
    dispatch_max_backoff_seconds: float = 300.0
    dispatch_timeout_seconds: float = 15.0

    # An alert firing longer than this is flagged on the firing worklist.
    # Usually it means the receiver lacks `send_resolved: true`, so OpsTrail
    # never learned it ended -- see docs/alertmanager.md. Nothing resolves it
    # automatically: inventing an ending the system was never told about is
    # worse than showing a stale row and saying why.
    firing_stale_hours: int = 24

    # --- business calendar ---
    # The timezone severity_rules windows are read in: the exchange's clock.
    # This is NOT the display timezone, which follows each viewer's browser.
    # Sharing one setting would let somebody opening the dashboard from
    # Singapore change what "during the trading session" means for everyone.
    business_timezone: str = "Asia/Ho_Chi_Minh"

    # --- retention ---
    alert_retention_months: int = 6
    # Deliveries age out faster than the alerts they belong to: "did it go
    # out" is asked within days, and the rows are bulkier (one per channel
    # per attempt, and a broken template parks its whole rendered output in
    # last_error). Only has any effect while it is SHORTER than the alert
    # window -- ON DELETE CASCADE gets there first otherwise.
    notification_retention_months: int = 3
    retention_hour_utc: int = 3  # daily purge time

    def validate_runtime(self) -> None:
        if not 1 <= self.dispatch_concurrency <= 32:
            raise ValueError("DISPATCH_CONCURRENCY must be 1..32")
        if self.dispatch_timeout_seconds <= 0 or self.dispatch_lease_seconds < (
            self.dispatch_timeout_seconds * 4 + 10
        ):
            raise ValueError("dispatch lease must exceed four HTTP timeouts plus 10 seconds")
        if not 1 <= self.backlog_digest_max_events <= 100:
            raise ValueError("BACKLOG_DIGEST_MAX_EVENTS must be 1..100")
        if self.backlog_retention_hours <= 0 or self.backlog_digest_after_seconds < 0:
            raise ValueError("invalid backlog windows")
        if self.dispatch_target_interval_seconds < 0:
            raise ValueError("target interval must be nonnegative")
        if self.environment not in ("development", "test", "production"):
            raise ValueError("ENVIRONMENT must be development, test or production")
        if self.environment == "production":
            if any(
                value.startswith("REPLACE")
                for value in (
                    self.session_secret,
                    self.intake_token,
                    self.ops_metrics_token,
                )
            ):
                raise ValueError("replace all example secrets before production startup")
            if self.msteams_enabled and not self.msteams_power_automate_url:
                raise ValueError("enabled Teams delivery requires a default target URL")
            if self.outbound_webhook_enabled and not self.outbound_webhook_url:
                raise ValueError("enabled outbound webhook requires a URL")
            if len(self.ops_metrics_token) < 32:
                raise ValueError("production requires a private OPS_METRICS_TOKEN")
            if len(self.session_secret) < 32 or self.session_secret in {
                "change-me-in-production",
                "dev-session-secret-change-me",
                "change-me-32-chars-min",
            }:
                raise ValueError(
                    "production requires a private SESSION_SECRET of at least 32 chars"
                )
            if len(self.intake_token) < 32 or self.intake_token in {
                "dev-intake-token",
                "change-me",
            }:
                raise ValueError("production requires a private INTAKE_TOKEN of at least 32 chars")
            if not self.public_host.startswith("https://"):
                raise ValueError("production PUBLIC_HOST must use HTTPS")
            if self.msteams_auth not in ("none", "bearer", "entra"):
                raise ValueError("unknown Teams auth mode")
            if self.msteams_auth == "bearer" and not self.msteams_auth_token:
                raise ValueError("bearer auth requires a token")
            if self.msteams_auth == "entra" and not all(
                (
                    self.entra_client_id,
                    self.entra_client_secret,
                    self.entra_tenant_id,
                )
            ):
                raise ValueError("Entra auth requires tenant and client credentials")

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
