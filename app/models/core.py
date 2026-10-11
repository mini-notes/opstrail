"""OpsTrail tables.

Design notes
------------
* `alerts` is keyed on (fingerprint, starts_at). Alertmanager repeats a
  notification every `repeat_interval`, so intake must UPSERT on that pair --
  inserting blindly would multiply one alert into dozens of rows.
* `alerts.incident_id`, `alerts.assignee_id` and `users.upn` are deliberate
  Phase 2 seams: nullable, unused today, so grouping / assignment / Teams
  mentions land without a migration.
* `label_source` records HOW a label was resolved, which turns "unlabelled
  alerts" into an actionable backlog of missing label_rules.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import (
    AlertStatus,
    IncidentStatus,
    LabelSource,
    NotificationChannel,
    NotificationStatus,
    OwnerRole,
    UserRole,
)


def _enum(python_enum, name: str):
    """Native PG enum that stores the VALUE, not the python member name."""
    return Enum(python_enum, name=name, values_callable=lambda e: [m.value for m in e])


class Team(Base):
    """An owning team. Named `teams` for the org unit -- never for MS Teams,
    which is always spelled out as `msteams_*`."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    # Optional per-team Power Automate URL; falls back to the global setting.
    msteams_webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Mention the owners from this severity upwards. Compared by RANK, not
    # alphabetically -- see app/services/mention.py.
    mention_severity: Mapped[str | None] = mapped_column(
        String(32), nullable=True, server_default="critical"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    services: Mapped[list["Service"]] = relationship(back_populates="team")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Free-form classification used by the dashboards: api / worker / db / ...
    app_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # NULL means "inherit from the team"; the team's own NULL falls back to
    # the global default.
    mention_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    team: Mapped["Team | None"] = relationship(back_populates="services")


class LabelRule(Base):
    """Fallback attribution for alerts whose Prometheus rule carries no
    service/team label.

    `match` is a flat {label: value} map; ALL entries must match the alert's
    labels. Lower `priority` wins, so a specific rule can be placed above a
    catch-all. Matching on several labels matters: one alertname
    (KubePodCrashLooping) legitimately spans many services.
    """

    __tablename__ = "label_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100", index=True)
    match: Mapped[dict] = mapped_column(JSONB)
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=True
    )
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Incident(Base):
    """Phase 2 groups alerts into incidents; the table exists from Phase 1 so
    that `alerts.incident_id` is a real FK from day one.

    DORA's MTTR must be computed over THIS table, not over `alerts`: a
    flapping alert produces dozens of firing/resolved pairs for one outage.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), nullable=True, index=True
    )
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[IncidentStatus] = mapped_column(
        _enum(IncidentStatus, "incident_status"),
        default=IncidentStatus.open,
        server_default="open",
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Entra ID UPN. Phase 2 needs it to mention the right person in a Teams
    # Adaptive Card; filled by hand now, by OIDC later.
    upn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        _enum(UserRole, "user_role"), default=UserRole.viewer, server_default="viewer"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Owner(Base):
    """Someone responsible for a service or team.

    Deliberately NOT the `users` table: the people who need to be mentioned
    when something breaks are rarely the same set as the people who log into
    OpsTrail, and requiring an account for each would mean the directory is
    always out of date. `user_id` links the two when they do overlap.

    Both `upn` and `aad_object_id` are kept because Teams Adaptive Card
    mentions accept different identifiers depending on the tenant -- which one
    actually rings a phone has to be verified per install, so both are stored
    and `msteams_mention_id_field` picks.
    """

    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aad_object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # For the out-of-hours SMS escalation planned in a later phase. Cheaper to
    # add the column now than to migrate once the directory is populated.
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def mention_name(self) -> str:
        return self.name or self.email or self.upn or f"owner-{self.id}"


class ServiceOwner(Base):
    __tablename__ = "service_owners"

    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owners.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[OwnerRole] = mapped_column(
        _enum(OwnerRole, "owner_role"),
        default=OwnerRole.primary,
        server_default="primary",
    )

    owner: Mapped["Owner"] = relationship(lazy="joined")


class TeamOwner(Base):
    __tablename__ = "team_owners"

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owners.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[OwnerRole] = mapped_column(
        _enum(OwnerRole, "owner_role"),
        default=OwnerRole.primary,
        server_default="primary",
    )

    owner: Mapped["Owner"] = relationship(lazy="joined")


class MessageTemplate(Base):
    """An operator-edited notification template.

    Stored in the database so changing a card does not need a redeploy. The
    file in `app/templates/msteams/` stays as the fallback: if no row is
    active, or if a saved template fails to render for a particular alert,
    the built-in one is used so notifications never stop.
    """

    __tablename__ = "message_templates"
    __table_args__ = (
        Index("ix_message_templates_active", "channel", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel")
    )
    name: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        # Alertmanager re-notifies on repeat_interval -- this pair is the
        # natural identity of one alert occurrence.
        UniqueConstraint("fingerprint", "starts_at", name="uq_alert_fingerprint_start"),
        Index("ix_alerts_starts_at_desc", "starts_at"),
        Index("ix_alerts_service_starts_at", "service_id", "starts_at"),
        Index(
            "ix_alerts_firing",
            "starts_at",
            postgresql_where="status = 'firing'",
        ),
        Index("ix_alerts_labels_gin", "labels", postgresql_using="gin"),
        # The /unlabelled backlog page scans exactly this predicate and is
        # opened routinely; a partial index keeps it off a full table scan.
        Index(
            "ix_alerts_unlabelled",
            "starts_at",
            postgresql_where="label_source IS NULL",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- identity ---
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[AlertStatus] = mapped_column(_enum(AlertStatus, "alert_status"))

    # --- content ---
    alertname: Mapped[str | None] = mapped_column(String(255), index=True)
    severity: Mapped[str | None] = mapped_column(String(32), index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    annotations: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    generator_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- attribution ---
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label_source: Mapped[LabelSource | None] = mapped_column(
        _enum(LabelSource, "label_source"), nullable=True
    )

    # --- Phase 2 seams (nullable, unused in Phase 1) ---
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- bookkeeping ---
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    notify_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    service: Mapped["Service | None"] = relationship()
    team: Mapped["Team | None"] = relationship()


class Notification(Base):
    """One delivery attempt record per (alert, channel, target).

    Written BEFORE the HTTP call so a crash mid-send is visible, and updated
    after. This is what makes "Teams was down" diagnosable instead of silent.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_retry", "status", "next_attempt_at"),
        # The delivery history page sorts on this by default and the
        # notification purge scans it; the retry index serves neither, since
        # it leads on `status`.
        Index("ix_notifications_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel")
    )
    target: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.pending,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The delivery list shows the alert name and filters by its service/team;
    # without this the page is one query per row.
    alert: Mapped["Alert"] = relationship(lazy="raise")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
