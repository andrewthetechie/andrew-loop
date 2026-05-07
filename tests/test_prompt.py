"""Tests for rich prompt assembly."""

import pytest

from orch.prompt import build_dispatch_prompt


def _make_ticket_data() -> dict:
    return {
        "id": "ORCH-1",
        "title": "Add user login",
        "description": "Implement login endpoint",
        "acceptance_criteria": "- [ ] POST /login returns JWT",
        "state": "To Do",
        "risk_score": 3,
        "rework_loop_count": 0,
        "linked_pr": None,
        "file_paths": "src/auth.py",
        "test_expectations": "test login returns 200",
    }


def test_coder_prompt_has_required_sections() -> None:
    """Coder dispatch prompt includes ticket data, validation, memory, workflow."""
    ticket = _make_ticket_data()
    comments = [{"author": "human", "body": "Please use bcrypt"}]
    config = {"validation_commands": ["uv run pytest", "uv run ruff check ."]}
    models = {"codebase-conventions": "Use snake_case everywhere."}

    prompt = build_dispatch_prompt(
        ticket,
        "coder",
        comments=comments,
        config=config,
        mental_models=models,
    )

    # Ticket data
    assert "ORCH-1" in prompt
    assert "Add user login" in prompt
    assert "Implement login endpoint" in prompt
    assert "POST /login returns JWT" in prompt
    assert "risk_score" in prompt.lower() or "Risk" in prompt

    # Comments
    assert "bcrypt" in prompt

    # Validation commands
    assert "uv run pytest" in prompt
    assert "uv run ruff check" in prompt

    # Memory context
    assert "snake_case" in prompt

    # Workflow
    assert "Code Review" in prompt


def test_reviewer_prompt_has_pr_and_no_validation() -> None:
    """Reviewer prompt includes PR link and targets Ready to Merge."""
    ticket = _make_ticket_data()
    ticket["linked_pr"] = "https://github.com/org/repo/pull/42"
    ticket["state"] = "Code Review"

    prompt = build_dispatch_prompt(ticket, "reviewer")

    assert "https://github.com/org/repo/pull/42" in prompt
    assert "Validation Commands" not in prompt
    assert "Ready to Merge" in prompt


def test_merger_prompt_has_review_results() -> None:
    """Merger prompt includes PR, risk score, review results, targets Done."""
    ticket = _make_ticket_data()
    ticket["linked_pr"] = "https://github.com/org/repo/pull/42"
    ticket["state"] = "Ready to Merge"
    config = {
        "code_review_result": "Approved with no issues",
        "security_review_result": "Low risk, no concerns",
    }

    prompt = build_dispatch_prompt(ticket, "merger", config=config)

    assert "https://github.com/org/repo/pull/42" in prompt
    assert "Approved with no issues" in prompt
    assert "Low risk, no concerns" in prompt
    assert "Risk Score: 3" in prompt
    assert "Done" in prompt


def test_empty_mental_models_omits_memory_section() -> None:
    """When no mental models are provided, Memory Context section is absent."""
    ticket = _make_ticket_data()

    prompt = build_dispatch_prompt(ticket, "coder", mental_models={})

    assert "Memory Context" not in prompt
    # Should still have ticket and workflow
    assert "ORCH-1" in prompt
    assert "Code Review" in prompt


def test_none_mental_models_omits_memory_section() -> None:
    """When mental_models is None, Memory Context section is absent."""
    ticket = _make_ticket_data()

    prompt = build_dispatch_prompt(ticket, "coder", mental_models=None)

    assert "Memory Context" not in prompt


def test_rework_ticket_includes_rework_context() -> None:
    """When ticket is in Rework state, prompt includes rework instructions."""
    ticket = _make_ticket_data()
    ticket["state"] = "Rework"
    ticket["rework_loop_count"] = 2
    ticket["linked_pr"] = "https://github.com/org/repo/pull/42"
    comments = [
        {"author": "reviewer", "body": "Fix the SQL injection in login"},
        {"author": "human", "body": "Original design note"},
    ]

    prompt = build_dispatch_prompt(ticket, "coder", comments=comments)

    assert "Rework" in prompt
    assert "Fix the SQL injection" in prompt
    assert "Rework Loop Count: 2" in prompt


@pytest.mark.asyncio
async def test_fetch_mental_models_via_client() -> None:
    """fetch_mental_models calls client with stable IDs and returns content."""
    from orch.prompt import fetch_mental_models

    stored = {
        "codebase-conventions": "Use snake_case.",
        "common-review-findings": "Check null returns.",
        "validation-patterns": "Always test edge cases.",
    }

    async def fake_get(model_id: str) -> str | None:
        return stored.get(model_id)

    models = await fetch_mental_models(get_model=fake_get)

    assert models["codebase-conventions"] == "Use snake_case."
    assert models["common-review-findings"] == "Check null returns."
    assert models["validation-patterns"] == "Always test edge cases."


@pytest.mark.asyncio
async def test_fetch_mental_models_missing_gracefully() -> None:
    """When models don't exist yet, they are omitted from result."""
    from orch.prompt import fetch_mental_models

    async def empty_get(model_id: str) -> str | None:
        return None

    models = await fetch_mental_models(get_model=empty_get)

    assert models == {}
