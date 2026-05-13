"""Hindsight retain hooks for lifecycle events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orch.db import Database
from orch.tickets import (
    get_ticket,
    get_ticket_comments,
    list_delegation_context,
    list_events,
    list_tickets,
)

logger = logging.getLogger(__name__)

_MENTAL_MODELS = [
    (
        "codebase-conventions",
        "Codebase Conventions",
        "What are the coding conventions, patterns, and standards used in this codebase?",
    ),
    (
        "common-review-findings",
        "Common Review Findings",
        "What are the most common code review findings and recurring issues in this codebase?",
    ),
    (
        "validation-patterns",
        "Validation Patterns",
        "What validation patterns, test strategies, and verification approaches are used?",
    ),
    (
        "security-patterns",
        "Security Patterns",
        "What security patterns, threat mitigations, and sensitive areas exist in this codebase?",
    ),
    (
        "lessons-learned",
        "Lessons Learned",
        "What lessons have been learned from completed tickets,"
        " failures, and human interventions?",
    ),
]

_CODING_AGENT_RETAIN_MISSION = (
    "Extract durable engineering learnings from agent workflow events: technical decisions "
    "and their rationale, architecture patterns, codebase conventions, validation failure "
    "signatures and fixes, recurring review findings, security-sensitive areas, tool/setup "
    "pitfalls, and human intervention reasons. Ignore raw task instructions, boilerplate "
    "ticket text, transient command output unless it explains a failure, and implementation "
    "details that are unlikely to recur."
)

_CODING_AGENT_OBSERVATIONS_MISSION = (
    "Synthesize stable project learnings useful for future decomposition, implementation, "
    "review, and validation. Prefer recurring patterns, durable conventions, repeated "
    "failure modes, architecture constraints, and human correction patterns. Do not preserve "
    "one-off ticket instructions unless they reveal a reusable lesson."
)

_CODING_AGENT_BANK_CONFIG = {
    "retain_mission": _CODING_AGENT_RETAIN_MISSION,
    "retain_extraction_mode": "verbose",
    "enable_observations": True,
    "observations_mission": _CODING_AGENT_OBSERVATIONS_MISSION,
}

_HINDSIGHT_CONTEXT_LABELS = {
    "coder": (
        "Codebase conventions",
        "Validation failures",
        "Review findings",
        "Similar ticket outcomes",
    ),
    "reviewer": (
        "Common review findings",
        "Security patterns",
        "Review findings",
        "Architecture constraints",
    ),
    "decomposer": (
        "Lessons learned",
        "Conventions",
        "Architecture decisions",
        "Prior outcomes",
    ),
}


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
            logger.debug(
                "acreate_bank failed for %s: %s — checking if bank already exists", bank_id, exc
            )
            # acreate_bank failed — check if the bank already exists and is usable
            try:
                await client.banks.get_bank_profile(bank_id)
                result.bank_status = "exists"
            except Exception:
                result.bank_status = "failed"
                return result

        try:
            await _aupdate_bank_config(client, bank_id, _CODING_AGENT_BANK_CONFIG)
        except Exception as exc:
            logger.debug("Failed to configure Hindsight bank %s: %s", bank_id, exc)
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

        trigger = mental_model_trigger_input.MentalModelTriggerInput(
            refresh_after_consolidation=True
        )

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


async def reset_hindsight_bank(url: str, bank_id: str, api_key: str) -> HindsightSetupResult:
    """Explicitly delete and recreate a Hindsight bank with orch defaults."""
    from hindsight_client import Hindsight

    client = Hindsight(base_url=url, api_key=api_key or None, timeout=15.0)
    try:
        await _adelete_bank(client, bank_id)
    except Exception as exc:
        logger.debug("Failed to delete Hindsight bank %s before reset: %s", bank_id, exc)
    finally:
        await client.aclose()

    return await setup_hindsight(url, bank_id, api_key)


async def _adelete_bank(client: object, bank_id: str) -> None:
    """Delete a bank across current and older Hindsight SDK surfaces."""
    banks = getattr(client, "banks", None)
    for owner in (banks, client):
        if owner is None:
            continue
        for method_name in ("adelete_bank", "delete_bank"):
            method = getattr(owner, method_name, None)
            if method is None:
                continue
            result = method(bank_id)
            if hasattr(result, "__await__"):
                await result
            return


async def backfill_hindsight(
    client: object,
    db: Database,
    *,
    bank_id: str,
    issue_id: int | None = None,
    ticket_id: str | None = None,
) -> int:
    """Backfill curated Hindsight learning events from local lifecycle records."""
    tickets = await _tickets_for_backfill(db, issue_id=issue_id, ticket_id=ticket_id)
    retained = 0
    for ticket in tickets:
        comments = await get_ticket_comments(db, ticket.id)
        delegations = await list_delegation_context(db, ticket.id)
        events = await list_events(db, ticket_id=ticket.id)

        if getattr(ticket, "state", "") == "Done":
            await _retain_backfill_event(
                client,
                bank_id=bank_id,
                context="ticket-outcome",
                document_id=f"ticket:{ticket.id}",
                content=_format_backfill_ticket_outcome(ticket, comments, delegations),
            )
            retained += 1

        for comment in comments:
            body = str(comment.body)
            if "## REVIEW_DECISION:" in body and "APPROVED" not in body:
                await retain_review_finding(
                    client,
                    ticket.id,
                    str(getattr(ticket, "title", "")),
                    finding=_compact_text(body, max_chars=900),
                    bank_id=bank_id,
                )
                retained += 1

            if "VALIDATION_FAILED" in body or "FAILED " in body:
                await _retain_backfill_event(
                    client,
                    bank_id=bank_id,
                    context="validation-failure",
                    document_id=f"validation:{ticket.id}:backfill:{comment.id}",
                    content=_format_backfill_validation_failure(ticket, body),
                )
                retained += 1

        saw_human_review = any(event.new_state == "Needs Human Review" for event in events)
        if saw_human_review:
            for comment in comments:
                if str(comment.author).lower() == "human":
                    await retain_human_intervention(
                        client,
                        db,
                        ticket.id,
                        comment_body=str(comment.body),
                        bank_id=bank_id,
                    )
                    retained += 1

        for delegation in delegations:
            helper_role = str(delegation.step).removeprefix("delegation:")
            await retain_delegation_summary(
                client,
                ticket.id,
                str(getattr(ticket, "title", "")),
                helper_role=helper_role,
                assigned_slice=str(getattr(ticket, "title", "")) or "Unknown",
                output_summary=str(delegation.output),
                usefulness="Backfilled from recorded hidden-helper output.",
                bank_id=bank_id,
            )
            retained += 1

    return retained


async def _tickets_for_backfill(
    db: Database,
    *,
    issue_id: int | None,
    ticket_id: str | None,
) -> list[object]:
    if issue_id is not None and ticket_id is not None:
        msg = "Backfill can be scoped by issue or ticket, not both."
        raise ValueError(msg)
    if ticket_id is not None:
        ticket = await get_ticket(db, ticket_id)
        return [ticket] if ticket is not None else []

    tickets = await list_tickets(db)
    if issue_id is not None:
        return [ticket for ticket in tickets if getattr(ticket, "issue_id", None) == issue_id]
    return tickets


async def _retain_backfill_event(
    client: object,
    *,
    bank_id: str,
    context: str,
    document_id: str,
    content: str,
) -> None:
    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context=context,
            timestamp=datetime.now(UTC).isoformat(),
            document_id=document_id,
        )
    except Exception:
        logger.exception("Failed to backfill Hindsight event %s", document_id)


def _format_backfill_ticket_outcome(
    ticket: object,
    comments: list[object],
    delegations: list[object],
) -> str:
    files = _list_lines(getattr(ticket, "file_paths", None))
    tests = _list_lines(getattr(ticket, "test_expectations", None))
    linked_pr = str(getattr(ticket, "linked_pr", "") or "None")
    review_comments = [
        _compact_text(getattr(comment, "body", ""), max_chars=220)
        for comment in comments
        if str(getattr(comment, "author", "")).lower() == "reviewer"
    ]
    human_comments = [
        _compact_text(getattr(comment, "body", ""), max_chars=220)
        for comment in comments
        if str(getattr(comment, "author", "")).lower() == "human"
    ]
    delegation_outputs = [
        _compact_text(getattr(delegation, "output", ""), max_chars=220)
        for delegation in delegations
    ]

    lines = [
        "Event: ticket-outcome",
        f"Ticket: {getattr(ticket, 'id', '')}",
        f"Title: {getattr(ticket, 'title', '')}",
        "",
        "Changed files:",
        *([f"- {path}" for path in files] or ["- Not recorded"]),
        "",
        "PR metadata:",
        f"- Linked PR: {linked_pr}",
        "",
        "Validation:",
        *([f"- {item}" for item in tests] or ["- Validation result not recorded"]),
        "",
        "Review signals:",
        *([f"- {item}" for item in review_comments] or ["- No review comments recorded"]),
        "",
        "Human intervention signals:",
        *([f"- {item}" for item in human_comments] or ["- None recorded"]),
        "",
        "Delegation outputs:",
        *([f"- {item}" for item in delegation_outputs] or ["- None recorded"]),
        "",
        "Reusable lesson:",
        "- Prefer these observed review, validation, PR, and delegation signals over raw ticket "
        "spec text when handling similar work.",
    ]
    return "\n".join(lines)


def _format_backfill_validation_failure(ticket: object, comment_body: str) -> str:
    return "\n".join(
        [
            "Event: validation-failure",
            f"Ticket: {getattr(ticket, 'id', '')}",
            f"Title: {getattr(ticket, 'title', '')}",
            "",
            "Command: Unknown (backfilled from lifecycle comment)",
            "",
            "Failure signature:",
            _compact_text(comment_body, max_chars=700),
            "",
            "Root cause: Unknown",
            "Resolution: Pending",
        ]
    )


async def fetch_hindsight_context(
    client: object,
    *,
    bank_id: str,
    ticket: object,
    agent_type: str,
) -> dict[str, str]:
    """Recall compact role-specific Hindsight context for an agent dispatch."""
    queries = build_hindsight_context_queries(ticket, agent_type)
    context: dict[str, str] = {}
    for label, query in queries.items():
        response = await _arecall_hindsight(client, bank_id=bank_id, query=query)
        content = _format_recall_response(response)
        if content:
            context[label] = content
    return context


def build_hindsight_context_queries(ticket: object, agent_type: str) -> dict[str, str]:
    """Build role-specific memory queries from ticket fields."""
    labels = _HINDSIGHT_CONTEXT_LABELS.get(agent_type, ())
    if not labels:
        return {}

    base = "\n".join(
        [
            f"Ticket: {getattr(ticket, 'title', '')}",
            f"State: {getattr(ticket, 'state', '')}",
            f"Risk: {getattr(ticket, 'risk_score', '-')}",
            f"Description: {_compact_text(getattr(ticket, 'description', ''), max_chars=500)}",
            f"Expected files: {_compact_text(getattr(ticket, 'file_paths', ''), max_chars=300)}",
            (
                "Validation expectations: "
                f"{_compact_text(getattr(ticket, 'test_expectations', ''), max_chars=300)}"
            ),
        ]
    )

    return {label: f"{label} relevant to this dispatch.\n{base}" for label in labels}


async def _arecall_hindsight(client: object, *, bank_id: str, query: str) -> object:
    if hasattr(client, "arecall"):
        return await client.arecall(  # type: ignore[attr-defined]
            bank_id=bank_id,
            query=query,
            types=["world", "experience", "observation"],
            budget="low",
            max_tokens=700,
        )

    return client.recall(  # type: ignore[attr-defined]
        bank_id=bank_id,
        query=query,
        types=["world", "experience", "observation"],
        budget="low",
        max_tokens=700,
    )


def _format_recall_response(response: object, *, max_chars: int = 1200) -> str:
    results = getattr(response, "results", response)
    if not results:
        return ""

    lines: list[str] = []
    for result in list(results)[:4]:
        text = _compact_text(getattr(result, "text", result), max_chars=280)
        if text:
            lines.append(f"- {text}")

    return _compact_text("\n".join(lines), max_chars=max_chars)


async def _aupdate_bank_config(
    client: object,
    bank_id: str,
    updates: dict[str, object],
) -> None:
    """Update Hindsight bank config across async and sync client versions."""
    if hasattr(client, "_aupdate_bank_config"):
        await client._aupdate_bank_config(bank_id, dict(updates))  # type: ignore[attr-defined]
        return

    client.update_bank_config(bank_id, **updates)  # type: ignore[attr-defined]


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

    content = _format_ticket_outcome_event(ticket)

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


def _format_ticket_outcome_event(ticket: object) -> str:
    files = _list_lines(getattr(ticket, "file_paths", None))
    tests = _list_lines(getattr(ticket, "test_expectations", None))
    description = _compact_text(getattr(ticket, "description", ""))

    lines = [
        "Event: ticket-outcome",
        f"Ticket: {getattr(ticket, 'id', '')}",
        f"Title: {getattr(ticket, 'title', '')}",
        "",
        "Changed files:",
        *[f"- {path}" for path in files],
        "",
        "What changed:",
        description or "- Ticket completed successfully.",
        "",
        "Why it matters:",
        "- This ticket satisfied its acceptance criteria and reached Done.",
        "",
        "Validation:",
        *(
            [f"- {item}" for item in tests]
            if tests
            else ["- Configured validators passed before Done."]
        ),
        "",
        "Reusable lesson:",
        "- Reuse the expected files, validation commands, and implementation pattern when a "
        "future ticket touches similar code.",
    ]
    return "\n".join(lines)


def _list_lines(value: object) -> list[str]:
    if value is None:
        return []
    return [line.strip("-* \t") for line in str(value).splitlines() if line.strip("-* \t")]


def _compact_text(value: object, *, max_chars: int = 700) -> str:
    text = " ".join(str(value).split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


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

    content = _format_human_intervention_event(ticket, comment_body)

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


async def retain_review_finding(
    client: object,
    ticket_id: str,
    ticket_title: str,
    *,
    finding: str,
    bank_id: str,
) -> None:
    """Retain a reviewer finding as a durable learning event."""
    content = _format_review_finding_event(ticket_id, ticket_title, finding)

    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context="review-finding",
            timestamp=datetime.now(UTC).isoformat(),
            document_id=f"review:{ticket_id}",
        )
    except Exception:
        logger.exception("Failed to retain review finding for %s", ticket_id)


async def retain_delegation_summary(
    client: object,
    ticket_id: str,
    ticket_title: str,
    *,
    helper_role: str,
    assigned_slice: str,
    output_summary: str,
    usefulness: str,
    bank_id: str,
) -> None:
    """Retain a hidden-helper delegation summary as a durable learning event."""
    content = _format_delegation_summary_event(
        ticket_id,
        ticket_title,
        helper_role=helper_role,
        assigned_slice=assigned_slice,
        output_summary=output_summary,
        usefulness=usefulness,
    )

    try:
        await client.aretain(
            bank_id=bank_id,
            content=content,
            context="delegation-summary",
            timestamp=datetime.now(UTC).isoformat(),
            document_id=f"delegation:{ticket_id}:{helper_role}",
        )
    except Exception:
        logger.exception("Failed to retain delegation summary for %s", ticket_id)


def _format_delegation_summary_event(
    ticket_id: str,
    ticket_title: str,
    *,
    helper_role: str,
    assigned_slice: str,
    output_summary: str,
    usefulness: str,
) -> str:
    return "\n".join(
        [
            "Event: delegation-summary",
            f"Ticket: {ticket_id}",
            f"Title: {ticket_title}",
            f"Helper role: {helper_role}",
            "",
            "Assigned slice:",
            _compact_text(assigned_slice),
            "",
            "Output summary:",
            _compact_text(output_summary),
            "",
            "Usefulness:",
            _compact_text(usefulness),
        ]
    )


def _format_review_finding_event(ticket_id: str, ticket_title: str, finding: str) -> str:
    severity = _extract_severity(finding)
    return "\n".join(
        [
            "Event: review-finding",
            f"Ticket: {ticket_id}",
            f"Title: {ticket_title}",
            f"Severity: {severity}",
            "",
            "Finding:",
            _compact_text(finding),
            "",
            "Root cause:",
            "- See finding text; reviewer identified this as the implementation concern.",
            "",
            "Fix pattern:",
            f"- {_compact_text(finding, max_chars=240)}",
        ]
    )


def _extract_severity(text: str) -> str:
    first = text.strip().split(":", 1)[0].strip().upper()
    if first in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        return first
    return "Unknown"


def _format_human_intervention_event(ticket: object, comment_body: str) -> str:
    return "\n".join(
        [
            "Event: human-intervention",
            f"Ticket: {getattr(ticket, 'id', '')}",
            f"Title: {getattr(ticket, 'title', '')}",
            "",
            "Human decision:",
            _compact_text(comment_body),
            "",
            "Why automation failed:",
            "- Human intervention was required; use the decision above as the evidence.",
            "",
            "Future agents should:",
            "- Apply this correction pattern when a similar ticket, validation failure, or "
            "blocker appears.",
        ]
    )


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

    content = _format_validation_failure_event(ticket_id, ticket_title, failures)
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


def _format_validation_failure_event(
    ticket_id: str,
    ticket_title: str,
    failures: list[dict],
) -> str:
    lines = [
        "Event: validation-failure",
        f"Ticket: {ticket_id}",
        f"Title: {ticket_title}",
        "",
        "Failures:",
    ]
    for result in failures:
        signature = _failure_signature(result)
        lines.extend(
            [
                f"- Command: {result.get('command', '<unknown>')}",
                f"  Exit code: {result.get('exit_code', '?')}",
                f"  Failure signature: {signature}",
                "  Root cause: Unknown",
                "  Resolution: Pending",
            ]
        )
    return "\n".join(lines)


def _failure_signature(result: dict) -> str:
    output = "\n".join(
        str(result.get(key, "")).strip() for key in ("stdout", "stderr") if result.get(key)
    )
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return _compact_text(stripped, max_chars=180)
    return "No output captured"
