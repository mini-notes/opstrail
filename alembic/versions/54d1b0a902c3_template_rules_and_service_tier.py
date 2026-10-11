"""template rules and service tier

Adds `template_rules` (which template an alert gets), `services.tier` (the
criticality band a rule can match on), and the uniqueness that
`message_templates` should always have had.

That last part is a repair, not just a constraint. Nothing enforced a single
active template per channel, and both readers select it with LIMIT 1 and no
ORDER BY -- so an install that somehow has two active rows has been sending
whichever one Postgres felt like. The UPDATEs below settle that BEFORE the
indexes are created, because a migration must not be the thing that refuses
to upgrade exactly the install that already has the problem.

Revision ID: 54d1b0a902c3
Revises: cf3d49d2123e
Create Date: 2026-10-11 20:53:45.670881
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "54d1b0a902c3"
down_revision: str | None = "cf3d49d2123e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("services", sa.Column("tier", sa.Integer(), nullable=True))

    op.create_table(
        "template_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("match", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["message_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_template_rules_priority"), "template_rules", ["priority"], unique=False
    )
    op.create_index(
        op.f("ix_template_rules_template_id"),
        "template_rules",
        ["template_id"],
        unique=False,
    )

    # Repair before constraining. Keep the most recently updated row as the
    # catch-all; the others stay as templates a rule can point at, so nobody
    # loses work they typed.
    op.execute(
        """
        UPDATE message_templates SET is_active = false
        WHERE is_active AND id NOT IN (
            SELECT DISTINCT ON (channel) id FROM message_templates
            WHERE is_active ORDER BY channel, updated_at DESC, id DESC
        )
        """
    )
    # The old save path hardcoded the name 'msteams-custom' and nothing
    # stopped a second row reusing it.
    op.execute(
        """
        UPDATE message_templates SET name = name || '-' || id
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY channel, name ORDER BY id
                ) AS rn
                FROM message_templates
            ) dup WHERE rn > 1
        )
        """
    )
    op.create_unique_constraint(
        "uq_message_templates_channel_name", "message_templates", ["channel", "name"]
    )
    op.create_index(
        "ux_message_templates_default",
        "message_templates",
        ["channel"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    # The renames and the deactivations are not undone: they repaired data
    # that was ambiguous, and restoring the ambiguity would be worse than
    # leaving it settled.
    op.drop_index(
        "ux_message_templates_default",
        table_name="message_templates",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_constraint(
        "uq_message_templates_channel_name", "message_templates", type_="unique"
    )
    op.drop_index(op.f("ix_template_rules_template_id"), table_name="template_rules")
    op.drop_index(op.f("ix_template_rules_priority"), table_name="template_rules")
    op.drop_table("template_rules")
    op.drop_column("services", "tier")
