"""alert acknowledgement

Revision ID: 06405e35c63f
Revises: 1c90c5c5ab2d
Create Date: 2026-10-14 22:07:32.331825
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '06405e35c63f'
down_revision: Union[str, None] = '1c90c5c5ab2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Two nullable columns and a partial index.

    The index is built CONCURRENTLY: intake writes to `alerts` on every
    webhook, and a plain CREATE INDEX over months of history holds a lock on
    exactly the table the tool exists to write to. CONCURRENTLY cannot run
    inside a transaction, hence the autocommit block.
    """
    op.add_column('alerts', sa.Column('acked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('alerts', sa.Column('acked_by_id', sa.Integer(), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_alerts_unacked', 'alerts', ['starts_at'], unique=False,
            postgresql_where="status = 'firing' AND acked_at IS NULL",
            postgresql_concurrently=True, if_not_exists=True,
        )
    # Named explicitly. Autogenerate left this as None, which renders a
    # downgrade that cannot run -- and a migration that only goes one way is
    # not a migration you can roll back in an incident.
    op.create_foreign_key(
        'fk_alerts_acked_by_id', 'alerts', 'users', ['acked_by_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_alerts_acked_by_id', 'alerts', type_='foreignkey')
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_alerts_unacked', table_name='alerts',
            postgresql_where="status = 'firing' AND acked_at IS NULL",
            postgresql_concurrently=True, if_exists=True,
        )
    op.drop_column('alerts', 'acked_by_id')
    op.drop_column('alerts', 'acked_at')
