"""Add persisted backend runtime state tables.

Revision ID: 002
Revises: 001
Create Date: 2026-05-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if "backend_leases" not in existing_tables:
        op.create_table(
            "backend_leases",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("backend_id", sa.Text(), nullable=False),
            sa.Column("logical_agent", sa.Text(), nullable=False),
            sa.Column("physical_alias", sa.Text(), nullable=False),
            sa.Column("ticket_id", sa.Text(), nullable=False),
            sa.Column("step_reserve", sa.Integer(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("lease_started_at", sa.Text(), nullable=False),
            sa.Column("stale_after", sa.Text(), nullable=False),
            sa.Column("completed_at", sa.Text(), nullable=True),
            sa.Column("stale_marked_at", sa.Text(), nullable=True),
            sa.Column("actual_steps", sa.Integer(), nullable=True),
            sa.CheckConstraint(
                "status IN ('active', 'completed', 'stale')",
                name="valid_backend_lease_status",
            ),
        )
        op.create_index(
            "ix_backend_leases_active_backend",
            "backend_leases",
            ["backend_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
        )

    if "backend_step_usage" not in existing_tables:
        op.create_table(
            "backend_step_usage",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("backend_id", sa.Text(), nullable=False),
            sa.Column("logical_agent", sa.Text(), nullable=False),
            sa.Column("ticket_id", sa.Text(), nullable=False),
            sa.Column("reserved_steps", sa.Integer(), nullable=False),
            sa.Column("actual_steps", sa.Integer(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("reserved_at", sa.Text(), nullable=False),
            sa.Column("reconciled_at", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "status IN ('reserved', 'reconciled')",
                name="valid_backend_step_usage_status",
            ),
        )

    if "backend_cooldowns" not in existing_tables:
        op.create_table(
            "backend_cooldowns",
            sa.Column("backend_id", sa.Text(), primary_key=True),
            sa.Column("logical_agent", sa.Text(), nullable=False),
            sa.Column("cooldown_until", sa.Text(), nullable=False),
            sa.Column("failure_classification", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("set_at", sa.Text(), nullable=False),
        )

    if "backend_dispatch_attempts" not in existing_tables:
        op.create_table(
            "backend_dispatch_attempts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticket_id", sa.Text(), nullable=False),
            sa.Column("logical_agent", sa.Text(), nullable=False),
            sa.Column("selected_backend_id", sa.Text(), nullable=False),
            sa.Column("physical_alias", sa.Text(), nullable=False),
            sa.Column("failure_classification", sa.Text(), nullable=True),
            sa.Column("skipped_reason", sa.Text(), nullable=True),
            sa.Column("attempted_at", sa.Text(), nullable=False),
        )

    if "backend_failure_state" not in existing_tables:
        op.create_table(
            "backend_failure_state",
            sa.Column("backend_id", sa.Text(), primary_key=True),
            sa.Column("logical_agent", sa.Text(), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("last_failure_classification", sa.Text(), nullable=False),
            sa.Column("last_failure_at", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("backend_failure_state")
    op.drop_table("backend_dispatch_attempts")
    op.drop_table("backend_cooldowns")
    op.drop_table("backend_step_usage")
    op.drop_index("ix_backend_leases_active_backend", table_name="backend_leases")
    op.drop_table("backend_leases")
