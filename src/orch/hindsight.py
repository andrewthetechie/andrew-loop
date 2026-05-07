"""Hindsight retain hooks for lifecycle events."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from orch.db import Database
from orch.tickets import get_ticket

logger = logging.getLogger(__name__)


async def retain_ticket_outcome(
    client: object,
    db: Database,
    ticket_id: str,
    *,
    bank_id: str,
) -> None:
    """Retain a ticket outcome to Hindsight when a ticket moves to Done."""
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        return

    content = (
        f"Ticket {ticket.id}: {ticket.title}\n\n"
        f"Description:\n{ticket.description}\n\n"
        f"Acceptance Criteria:\n{ticket.acceptance_criteria}"
    )

    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context="ticket-outcome",
            timestamp=datetime.now(UTC).isoformat(),
            document_id=f"ticket:{ticket_id}",
        )
    except Exception:
        logger.exception("Failed to retain ticket outcome for %s", ticket_id)


async def retain_human_intervention(
    client: object,
    db: Database,
    ticket_id: str,
    *,
    comment_body: str,
    bank_id: str,
) -> None:
    """Retain a human intervention to Hindsight when a human resolves Needs Human Review."""
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        return

    content = f"Ticket {ticket.id}: {ticket.title}\n\nHuman Decision:\n{comment_body}"

    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context="human-intervention",
            timestamp=datetime.now(UTC).isoformat(),
            document_id=f"intervention:{ticket_id}",
        )
    except Exception:
        logger.exception("Failed to retain human intervention for %s", ticket_id)
