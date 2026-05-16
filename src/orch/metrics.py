"""Token metrics queries and recording."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from orch.db import Database


async def list_issue_totals(db: Database) -> list[dict]:
    """Return total tokens aggregated by issue."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT t.issue_id AS issue_id,
                       COUNT(DISTINCT m.ticket_id) AS ticket_count,
                       SUM(m.total_tokens) AS total_tokens
                FROM ticket_metrics AS m
                JOIN tickets AS t ON t.id = m.ticket_id
                GROUP BY t.issue_id
                ORDER BY t.issue_id
                """
            )
        )
        rows = result.mappings().all()
    return [dict(row) for row in rows]


async def get_issue_breakdown(db: Database, issue_id: int) -> tuple[str | None, list[dict]]:
    """Return the issue title and per-ticket per-agent metrics rows for one issue."""
    async with db.engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT t.id AS ticket_id,
                       t.title AS title,
                       m.agent_type AS agent_type,
                       m.model AS model,
                       m.total_tokens AS total_tokens,
                       m.dispatched_at AS dispatched_at
                FROM ticket_metrics AS m
                JOIN tickets AS t ON t.id = m.ticket_id
                WHERE t.issue_id = :issue_id
                ORDER BY m.ticket_id, m.dispatched_at, m.id
                """
            ),
            {"issue_id": issue_id},
        )
        rows = result.mappings().all()

    if not rows:
        return None, []
    return str(rows[0]["title"]), [dict(row) for row in rows]


async def record_ticket_metric(
    db: Database,
    *,
    ticket_id: str,
    agent_type: str,
    logical_agent: str | None = None,
    model: str,
    total_tokens: int,
    backend_id: str | None = None,
    physical_alias: str | None = None,
    allocation_reason: str | None = None,
    dispatched_at: str | None = None,
) -> None:
    """Insert one append-only metrics row for a dispatch."""
    if dispatched_at is None:
        dispatched_at = datetime.now(UTC).isoformat()

    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO ticket_metrics
                    (
                        ticket_id,
                        agent_type,
                        logical_agent,
                        model,
                        total_tokens,
                        backend_id,
                        physical_alias,
                        allocation_reason,
                        dispatched_at
                    )
                VALUES
                    (
                        :ticket_id,
                        :agent_type,
                        :logical_agent,
                        :model,
                        :total_tokens,
                        :backend_id,
                        :physical_alias,
                        :allocation_reason,
                        :dispatched_at
                    )
                """
            ),
            {
                "ticket_id": ticket_id,
                "agent_type": agent_type,
                "logical_agent": logical_agent,
                "model": model,
                "total_tokens": total_tokens,
                "backend_id": backend_id,
                "physical_alias": physical_alias,
                "allocation_reason": allocation_reason,
                "dispatched_at": dispatched_at,
            },
        )
