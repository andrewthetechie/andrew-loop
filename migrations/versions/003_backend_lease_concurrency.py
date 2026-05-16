"""Allow multiple active leases per backend.

Revision ID: 003
Revises: 002
Create Date: 2026-05-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    indexes = {index["name"] for index in inspector.get_indexes("backend_leases")}
    if "ix_backend_leases_active_backend" in indexes:
        op.drop_index("ix_backend_leases_active_backend", table_name="backend_leases")


def downgrade() -> None:
    op.create_index(
        "ix_backend_leases_active_backend",
        "backend_leases",
        ["backend_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
