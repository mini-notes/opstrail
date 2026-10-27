"""Immutable delivery snapshots, destination leases and worker heartbeat."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "ab1200261027"
down_revision = "06405e35c63f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notifications", sa.Column("event_snapshot", postgresql.JSONB()))
    op.add_column("notifications", sa.Column("payload", postgresql.JSONB(), nullable=True))
    op.add_column("notifications", sa.Column("digest_parent_id", sa.Integer(), nullable=True))
    op.add_column("notifications", sa.Column("scope_team_id", sa.Integer(), nullable=True))
    op.add_column("notifications", sa.Column("event_status", sa.String(16), nullable=True))
    op.add_column("notifications", sa.Column("worker_seen_at", sa.DateTime(timezone=True)))
    op.create_index("ix_notifications_digest_parent_id", "notifications", ["digest_parent_id"])
    op.create_table("outbound_targets",
                    sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("claim_token", sa.String(36)),
                    sa.Column("lease_until", sa.DateTime(timezone=True)),
                    sa.Column("next_send_at", sa.DateTime(timezone=True)))
    op.create_table("worker_state",
                    sa.Column("name", sa.String(64), primary_key=True),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("detail", postgresql.JSONB(), nullable=False))
    # Preserve scope on existing rows; historic status/payload cannot be reconstructed.
    op.execute("UPDATE notifications n SET scope_team_id=a.team_id "
               "FROM alerts a WHERE a.id=n.alert_id")


def downgrade():
    op.drop_table("worker_state")
    op.drop_table("outbound_targets")
    op.drop_index("ix_notifications_digest_parent_id", table_name="notifications")
    for name in ("worker_seen_at", "event_status", "scope_team_id", "digest_parent_id", "payload", "event_snapshot"):
        op.drop_column("notifications", name)
