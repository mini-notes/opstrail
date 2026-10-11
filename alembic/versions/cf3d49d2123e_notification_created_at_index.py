"""notification created_at index

The delivery history page orders by `created_at` and the notification purge
scans it; `ix_notifications_retry` serves neither, because it leads on
`status`.

Built CONCURRENTLY: a plain CREATE INDEX takes a lock that blocks writes to
`notifications`, and intake writes a row there before every send. On an
install with months of delivery history that is an outage of the thing this
tool exists to do. CONCURRENTLY cannot run inside a transaction, hence the
autocommit block.

Revision ID: cf3d49d2123e
Revises: dd71fbc8e2c8
Create Date: 2026-10-11 20:42:49.497287
"""
from collections.abc import Sequence

from alembic import op

revision: str = "cf3d49d2123e"
down_revision: str | None = "dd71fbc8e2c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_notifications_created_at",
            "notifications",
            ["created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_notifications_created_at",
            table_name="notifications",
            postgresql_concurrently=True,
            if_exists=True,
        )
