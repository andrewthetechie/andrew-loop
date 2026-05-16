"""Add backend execution detail columns to ticket_metrics.

Revision ID: 004
Revises: 003
Create Date: 2026-05-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())
    if "ticket_metrics" not in existing_tables:
        return
    existing_cols = {col["name"] for col in inspector.get_columns("ticket_metrics")}

    with op.batch_alter_table("ticket_metrics") as batch_op:
        if "logical_agent" not in existing_cols:
            batch_op.add_column(sa.Column("logical_agent", sa.Text(), nullable=True))
        if "backend_id" not in existing_cols:
            batch_op.add_column(sa.Column("backend_id", sa.Text(), nullable=True))
        if "physical_alias" not in existing_cols:
            batch_op.add_column(sa.Column("physical_alias", sa.Text(), nullable=True))
        if "allocation_reason" not in existing_cols:
            batch_op.add_column(sa.Column("allocation_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ticket_metrics") as batch_op:
        batch_op.drop_column("allocation_reason")
        batch_op.drop_column("physical_alias")
        batch_op.drop_column("backend_id")
        batch_op.drop_column("logical_agent")
