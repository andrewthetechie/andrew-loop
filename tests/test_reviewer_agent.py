"""Tests for reviewer agent config and prompt."""

from orch.agents.config import (
    AGENTS_SOURCE_DIR,
    AgentConfig,
    compile_all_agents,
    reviewer_agent_config,
)


def test_reviewer_factory_returns_agent_config() -> None:
    """reviewer_agent_config() returns an AgentConfig with reviewer identity."""
    config = reviewer_agent_config()

    assert isinstance(config, AgentConfig)
    assert config.name == "reviewer"
    assert config.mode == "primary"
    assert config.temperature == 0.3
    assert config.steps == 50
    assert config.prompt_file == "reviewer.md"


def test_reviewer_permissions_allow_read_and_bash() -> None:
    """Reviewer allows read, glob, grep, list, bash (for gh commands)."""
    config = reviewer_agent_config()

    for tool in ("read", "glob", "grep", "list", "bash"):
        assert config.permission[tool] == "allow", f"{tool} should be allowed"


def test_reviewer_permissions_deny_editing_and_research() -> None:
    """Reviewer denies edit, task, websearch, webfetch."""
    config = reviewer_agent_config()

    for tool in ("edit", "task", "websearch", "webfetch"):
        assert config.permission[tool] == "deny", f"{tool} should be denied"


def test_reviewer_permissions_allow_mcp_servers() -> None:
    """Reviewer allows serena, gitnexus, context7, hindsight MCP tools."""
    config = reviewer_agent_config()

    for pattern in ("serena_*", "gitnexus_*", "context7_*", "hindsight_*"):
        assert config.permission[pattern] == "allow", f"{pattern} should be allowed"


def test_reviewer_no_pullmd_or_firecrawl() -> None:
    """Reviewer does not need pullmd or firecrawl MCP tools."""
    config = reviewer_agent_config()

    assert "pullmd_*" not in config.permission
    assert "firecrawl_*" not in config.permission


def test_compile_all_agents_includes_reviewer() -> None:
    """compile_all_agents produces reviewer entry with correct permissions."""
    result = compile_all_agents(
        {
            "coder": "qwen/qwen-2.5-coder",
            "reviewer": "anthropic/claude-sonnet-4-20250514",
        }
    )

    assert "reviewer" in result
    assert result["reviewer"]["model"] == "anthropic/claude-sonnet-4-20250514"
    assert result["reviewer"]["temperature"] == 0.3
    assert result["reviewer"]["permission"]["bash"] == "allow"
    assert result["reviewer"]["permission"]["edit"] == "deny"


# --- Prompt content tests ---

PROMPT_PATH = AGENTS_SOURCE_DIR / "reviewer" / "prompt.md"


def _read_prompt() -> str:
    assert PROMPT_PATH.is_file(), f"Reviewer prompt not found at {PROMPT_PATH}"
    return PROMPT_PATH.read_text()


def test_prompt_covers_code_quality_and_security() -> None:
    """Single prompt covers both code quality and security review."""
    prompt = _read_prompt()

    assert "code quality" in prompt.lower()
    assert "security" in prompt.lower()
    # Should NOT reference separate security review agent
    assert "Security Review agent" not in prompt


def test_prompt_risk_based_security_depth() -> None:
    """Security depth varies by risk score: 1-2 light, 3+ thorough."""
    prompt = _read_prompt()

    assert "risk" in prompt.lower()
    # Should describe the two tiers
    assert "1-2" in prompt
    assert "3" in prompt


def test_prompt_remote_to_local_handoff() -> None:
    """Blocking findings must include concrete diffs/patches."""
    prompt = _read_prompt()

    assert "diff" in prompt.lower() or "patch" in prompt.lower()
    # The key constraint: findings must be actionable by a junior engineer
    assert "junior" in prompt.lower() or "mechanically" in prompt.lower()


def test_prompt_structured_rework_format() -> None:
    """Rework comments written to ticket with explicit patches."""
    prompt = _read_prompt()

    assert "rework" in prompt.lower()
    assert "ticket" in prompt.lower()
    # Should mention structured format
    assert "patch" in prompt.lower() or "diff" in prompt.lower()


def test_prompt_state_transitions() -> None:
    """Correct state transitions: pass, blocking, high-security."""
    prompt = _read_prompt()

    assert "Ready to Merge" in prompt
    assert "Rework" in prompt
    assert "Needs Human Review" in prompt


def test_prompt_can_challenge_risk_score() -> None:
    """Reviewer can challenge the risk score."""
    prompt = _read_prompt()

    assert "challenge" in prompt.lower()
    assert "risk score" in prompt.lower()


def test_prompt_tool_preferences() -> None:
    """Tool Preferences references Serena, GitNexus, Context7, Hindsight."""
    prompt = _read_prompt()

    assert "## Tool Preferences" in prompt
    for tool in ("Serena", "GitNexus", "Context7", "Hindsight"):
        assert tool in prompt, f"Missing tool reference: {tool}"

    # Specific tool names from acceptance criteria
    for name in ("find_referencing_symbols", "gitnexus_impact", "query-docs"):
        assert name in prompt, f"Missing tool name: {name}"


def test_prompt_hindsight_retain() -> None:
    """Retains findings to Hindsight with review-finding context."""
    prompt = _read_prompt()

    assert "hindsight" in prompt.lower()
    assert "review-finding" in prompt or "review_finding" in prompt


def test_prompt_excludes_removed_states() -> None:
    """State list does not include Merged or Ready for Security Review."""
    prompt = _read_prompt()

    states_section = ""
    for section in prompt.split("## "):
        if section.startswith("Ticket States"):
            states_section = section
            break
    assert states_section, "Ticket States section not found"
    assert "Merged" not in states_section
    assert "Ready for Security Review" not in states_section


def test_prompt_reviewer_loop_state_markers() -> None:
    """Prompt has REVIEWER LOOP STATE markers."""
    prompt = _read_prompt()

    assert "REVIEWER LOOP STATE" in prompt


def test_prompt_uses_gh_for_pr_comments() -> None:
    """Reviewer uses gh directly for PR review comments."""
    prompt = _read_prompt()

    assert "gh" in prompt.lower()


def test_prompt_bash_restricted_to_gh() -> None:
    """Prompt constrains bash usage to gh commands."""
    prompt = _read_prompt()

    assert "gh" in prompt
    # Should mention restriction
    assert "only" in prompt.lower() or "restrict" in prompt.lower()
