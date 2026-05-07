"""Rich prompt assembly for agent dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

GetModel = Callable[[str], Awaitable[str | None]]

MENTAL_MODEL_IDS = [
    "codebase-conventions",
    "common-review-findings",
    "validation-patterns",
]

AGENT_TARGET_STATE = {
    "coder": "Code Review",
    "reviewer": "Ready to Merge",
    "merger": "Done",
}


def build_dispatch_prompt(
    ticket: dict[str, Any],
    agent_type: str,
    *,
    comments: list[dict[str, str]] | None = None,
    config: dict[str, Any] | None = None,
    mental_models: dict[str, str] | None = None,
) -> str:
    """Build the full dispatch prompt for an agent."""
    sections: list[str] = []
    config = config or {}
    comments = comments or []
    mental_models = mental_models or {}

    # Ticket data
    sections.append(_ticket_section(ticket))

    # Comments
    if comments:
        sections.append(_comments_section(comments))

    # Agent-specific sections
    if agent_type == "coder":
        sections.append(_validation_section(config))
    elif agent_type == "reviewer":
        sections.append(_pr_section(ticket))
    elif agent_type == "merger":
        sections.append(_pr_section(ticket))
        sections.append(_review_section(config))

    # Memory context
    if mental_models:
        sections.append(_memory_section(mental_models))

    # Workflow instructions
    target = AGENT_TARGET_STATE.get(agent_type, "Code Review")
    sections.append(_workflow_section(ticket, agent_type, target))

    return "\n\n".join(sections)


def _ticket_section(ticket: dict[str, Any]) -> str:
    lines = [
        "## Ticket",
        f"- ID: {ticket['id']}",
        f"- Title: {ticket['title']}",
        f"- State: {ticket['state']}",
        f"- Risk Score: {ticket.get('risk_score', '-')}",
        f"- Rework Loop Count: {ticket.get('rework_loop_count', 0)}",
        f"- Linked PR: {ticket.get('linked_pr') or 'None'}",
        "",
        "### Description",
        ticket["description"],
        "",
        "### Acceptance Criteria",
        ticket["acceptance_criteria"],
    ]
    if ticket.get("file_paths"):
        lines.extend(["", "### File Paths", ticket["file_paths"]])
    if ticket.get("test_expectations"):
        lines.extend(["", "### Test Expectations", ticket["test_expectations"]])
    return "\n".join(lines)


def _comments_section(comments: list[dict[str, str]]) -> str:
    lines = ["## Comments"]
    for c in comments:
        lines.append(f"- **{c['author']}**: {c['body']}")
    return "\n".join(lines)


def _validation_section(config: dict[str, Any]) -> str:
    cmds = config.get("validation_commands", [])
    lines = ["## Validation Commands"]
    if cmds:
        for cmd in cmds:
            lines.append(f"- `{cmd}`")
    else:
        lines.append("No validation commands configured.")
    return "\n".join(lines)


def _pr_section(ticket: dict[str, Any]) -> str:
    pr = ticket.get("linked_pr") or "None"
    return f"## Pull Request\n- URL: {pr}"


def _review_section(config: dict[str, Any]) -> str:
    lines = ["## Review Results"]
    for key in ("code_review_result", "security_review_result"):
        val = config.get(key, "Not available")
        label = key.replace("_", " ").title()
        lines.append(f"- {label}: {val}")
    return "\n".join(lines)


def _memory_section(models: dict[str, str]) -> str:
    lines = ["## Memory Context"]
    for model_id, content in models.items():
        lines.append(f"### {model_id}")
        lines.append(content)
    return "\n".join(lines)


def _workflow_section(ticket: dict[str, Any], agent_type: str, target_state: str) -> str:
    return "\n".join(
        [
            "## Workflow Instructions",
            f"- Agent Role: {agent_type}",
            f"- Current State: {ticket['state']}",
            f"- Target State: {target_state}",
        ]
    )


async def fetch_mental_models(
    *,
    get_model: GetModel,
    model_ids: list[str] | None = None,
) -> dict[str, str]:
    """Fetch mental models by stable ID, skipping any that are missing."""
    ids = model_ids or MENTAL_MODEL_IDS
    models: dict[str, str] = {}
    for model_id in ids:
        content = await get_model(model_id)
        if content is not None:
            models[model_id] = content
    return models
