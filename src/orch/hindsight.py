"""Hindsight retain hooks for lifecycle events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orch.db import Database
from orch.tickets import get_ticket

logger = logging.getLogger(__name__)

_MENTAL_MODELS = [
    ("codebase-conventions", "Codebase Conventions", "What are the coding conventions, patterns, and standards used in this codebase?"),
    ("common-review-findings", "Common Review Findings", "What are the most common code review findings and recurring issues in this codebase?"),
    ("validation-patterns", "Validation Patterns", "What validation patterns, test strategies, and verification approaches are used?"),
    ("security-patterns", "Security Patterns", "What security patterns, threat mitigations, and sensitive areas exist in this codebase?"),
    ("lessons-learned", "Lessons Learned", "What lessons have been learned from completed tickets, failures, and human interventions?"),
]


@dataclass
class HindsightSetupResult:
    """Result of hindsight setup during orch init."""

    bank_status: str = "skipped"
    model_statuses: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.bank_status == "failed":
            return "failed"
        if any(v == "failed" for v in self.model_statuses.values()):
            return "failed"
        if self.bank_status == "skipped":
            return "skipped"
        return "created"


async def setup_hindsight(url: str, bank_id: str, api_key: str) -> HindsightSetupResult:
    """Create or verify Hindsight bank and mental models for a repo.

    Uses acreate_bank (idempotent PUT) and only creates mental models
    that don't already exist.
    """
    from hindsight_client import Hindsight
    from hindsight_client_api.models import create_mental_model_request, mental_model_trigger_input

    result = HindsightSetupResult()
    client = Hindsight(base_url=url, api_key=api_key or None, timeout=15.0)

    try:
        # Create or update bank with tuned disposition (acreate_bank is idempotent PUT)
        try:
            await client.acreate_bank(
                bank_id=bank_id,
                disposition_skepticism=4,
                disposition_literalism=5,
                disposition_empathy=1,
                enable_observations=True,
            )
            result.bank_status = "created"
        except Exception as exc:
            logger.debug("acreate_bank failed for %s: %s — checking if bank already exists", bank_id, exc)
            # acreate_bank failed — check if the bank already exists and is usable
            try:
                await client.banks.get_bank_profile(bank_id)
                result.bank_status = "exists"
            except Exception:
                result.bank_status = "failed"
                return result

        # Fetch existing mental models to skip already-created ones
        try:
            existing_response = await client.mental_models.list_mental_models(bank_id)
            items = getattr(existing_response, "items", None) or []
            existing_ids = {m.id for m in items}
        except Exception as exc:
            logger.debug("Failed to list mental models for bank %s: %s", bank_id, exc)
            existing_ids = set()

        trigger = mental_model_trigger_input.MentalModelTriggerInput(refresh_after_consolidation=True)

        for model_id, name, source_query in _MENTAL_MODELS:
            if model_id in existing_ids:
                result.model_statuses[model_id] = "exists"
                continue
            try:
                req = create_mental_model_request.CreateMentalModelRequest(
                    id=model_id,
                    name=name,
                    source_query=source_query,
                    trigger=trigger,
                )
                await client.mental_models.create_mental_model(bank_id, req)
                result.model_statuses[model_id] = "created"
            except Exception as exc:
                logger.debug("Failed to create mental model %s: %s", model_id, exc)
                result.model_statuses[model_id] = "failed"
    finally:
        await client.aclose()

    return result


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


async def retain_validation_results(
    client: object,
    ticket_id: str,
    ticket_title: str,
    *,
    validation_results: list[dict],
    bank_id: str,
) -> None:
    """Retain pre-dispatch validation results to Hindsight.

    Called by the router when validation failures are found before reviewer
    dispatch. Feeds the validation-patterns mental model over time.
    """
    failures = [r for r in validation_results if not r.get("passed")]
    if not failures:
        return  # Only retain when there are failures worth learning from

    lines = [f"Ticket {ticket_id}: {ticket_title}", "", "Validation failures before review:"]
    for r in failures:
        lines.append(f"\nCommand: {r['command']}")
        lines.append(f"Exit code: {r.get('exit_code', '?')}")
        stdout = r.get("stdout", "").strip()
        stderr = r.get("stderr", "").strip()
        if stdout:
            lines.append(f"Stdout:\n{stdout[:400]}")
        if stderr:
            lines.append(f"Stderr:\n{stderr[:400]}")

    content = "\n".join(lines)
    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context="validation-failure",
            timestamp=datetime.now(UTC).isoformat(),
            document_id=f"validation:{ticket_id}",
        )
    except Exception:
        logger.exception("Failed to retain validation results for %s", ticket_id)
