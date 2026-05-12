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
    sections.append(_workflow_section(ticket, agent_type, target, config=config))

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
    """Render validation results or commands depending on agent role.

    For reviewer/merger: the router pre-runs validators and passes results here.
    For coder: lists the commands to run.

    Plain-text format is used throughout — more reliable than JSON for
    cost-optimised (Qwen-class) models reading a large context document.
    """
    results = config.get("validation_results")
    if results:
        n_pass = sum(1 for r in results if r.get("passed"))
        n_fail = len(results) - n_pass
        overall = "ALL PASS" if n_fail == 0 else f"{n_fail} FAILED, {n_pass} PASSED"

        lines = [
            "## Validation Results",
            "These were run by the router before dispatch. Do not re-run them.",
            f"Overall: {overall}",
            "",
        ]

        for r in results:
            exit_code = r.get("exit_code", 0)
            status = "PASS" if r.get("passed") else "FAIL"
            lines.append(f"### Validator: `{r['command']}`")
            lines.append(f"Exit code: {exit_code} ({status})")

            stdout = str(r.get("stdout", "")).strip()
            stderr = str(r.get("stderr", "")).strip()

            if stdout:
                lines.append("Stdout:")
                lines.append("```")
                lines.append(stdout)
                lines.append("```")
            else:
                lines.append("Stdout: (none)")

            if stderr:
                lines.append("Stderr:")
                lines.append("```")
                lines.append(stderr)
                lines.append("```")
            else:
                lines.append("Stderr: (none)")

            lines.append("")

        return "\n".join(lines)

    # Coder case — list commands to run
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

    # Extract the most recent REVIEW_DECISION marker from ticket comments.
    # The reviewer writes a comment beginning with "## REVIEW_DECISION: <DECISION>"
    # as the authoritative review record (GitHub formal reviews are unavailable
    # in single-user workflows where the PR author and reviewer share an account).
    decision = config.get("review_decision", "Not available")
    review_comment = config.get("review_comment", "")
    lines.append(f"- Decision: {decision}")
    if review_comment:
        lines.append(f"- Review Summary: {review_comment[:400]}")

    # Legacy keys kept for backward compatibility
    for key in ("code_review_result", "security_review_result"):
        val = config.get(key)
        if val:
            label = key.replace("_", " ").title()
            lines.append(f"- {label}: {val}")

    return "\n".join(lines)


def _memory_section(models: dict[str, str]) -> str:
    lines = ["## Memory Context"]
    for model_id, content in models.items():
        lines.append(f"### {model_id}")
        lines.append(content)
    return "\n".join(lines)


def _workflow_section(
    ticket: dict[str, Any],
    agent_type: str,
    target_state: str,
    config: dict[str, Any] | None = None,
) -> str:
    config = config or {}
    worktree_dir = config.get("worktree_dir", "")
    step_budget = config.get("step_budget", 60) if config else 60
    # For high-risk reviewer dispatches, warn to write decision earlier.
    # Risk 4+ requires thorough security review which consumes many steps; the
    # reviewer must write REVIEW_DECISION before deep review so the ticket advances
    # even if the step budget runs out mid-investigation.
    risk_score = ticket.get("risk_score") or 0
    if agent_type == "reviewer" and isinstance(risk_score, int) and risk_score >= 4:
        write_decision_by = max(int(step_budget) // 2, int(step_budget) - 15)
    else:
        write_decision_by = int(step_budget) - 5

    if agent_type == "coder":
        budget_warning = (
            f" (commit by step {write_decision_by}; if not finished,"
            f" call ticket-comment with a handoff summary by step {write_decision_by})"
        )
    elif agent_type == "reviewer":
        budget_warning = (
            f" (write REVIEW_DECISION and update ticket no later than step {write_decision_by})"
        )
    else:
        # merger and others: no reviewer-specific language
        budget_warning = (
            f" (complete merge and move ticket to final state by step {write_decision_by})"
        )

    lines = [
        "## Workflow Instructions",
        f"- Agent Role: {agent_type}",
        f"- Current State (pre-dispatch): {ticket['state']}",
        "  Note: ticket will read as 'In Progress' at runtime — this is normal.",
        f"- Target State: {target_state}",
        f"- Step Budget: {step_budget}{budget_warning}",
    ]
    if agent_type == "reviewer" and isinstance(risk_score, int) and risk_score >= 4:
        lines.append(
            f"- **Risk {risk_score} ticket**: Deep security review required but step budget"
            f" is limited. Write `## REVIEW_DECISION:` ticket comment by step"
            f" {write_decision_by} at the latest. Do deep review after — revise if needed."
        )
    if worktree_dir:
        lines += [
            f"- Working Directory: {worktree_dir}",
            "- **All commands must run from the working directory above.**"
            " Never `cd` outside it. All file paths are relative to this directory.",
        ]
    return "\n".join(lines)


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
