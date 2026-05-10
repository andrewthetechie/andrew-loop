"""Add issue_id to tickets and create ticket_metrics table.

Revision ID: 001
Revises:
Create Date: 2026-05-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Add nullable issue_id column to tickets if not already present.
    # Databases created via create_all before Alembic was introduced may
    # already have this column — skip rather than fail.
    existing_cols = {col["name"] for col in inspector.get_columns("tickets")}
    if "issue_id" not in existing_cols:
        with op.batch_alter_table("tickets") as batch_op:
            batch_op.add_column(sa.Column("issue_id", sa.Integer(), nullable=True))

    # Create ticket_metrics only if it does not already exist.
    if "ticket_metrics" not in inspector.get_table_names():
        op.create_table(
            "ticket_metrics",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "ticket_id",
                sa.Text(),
                sa.ForeignKey("tickets.id"),
                nullable=False,
            ),
            sa.Column("agent_type", sa.Text(), nullable=False),
            sa.Column("model", sa.Text(), nullable=False),
            sa.Column("total_tokens", sa.Integer(), nullable=False),
            sa.Column("dispatched_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("ticket_metrics")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("issue_id")
