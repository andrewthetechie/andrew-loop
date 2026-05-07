"""Tests for merger agent config and prompt."""

from orch.agents.config import (
    AGENTS_SOURCE_DIR,
    AgentConfig,
    compile_all_agents,
    merger_agent_config,
)


def test_merger_factory_returns_agent_config() -> None:
    """merger_agent_config() returns an AgentConfig with merger identity."""
    config = merger_agent_config()

    assert isinstance(config, AgentConfig)
    assert config.name == "merger"
    assert config.mode == "primary"
    assert config.temperature == 0.1
    assert config.steps == 50
    assert config.prompt_file == "merger.md"


def test_merger_permissions_allow_read_and_bash() -> None:
    """Merger allows read, glob, grep, list, bash (for gh merge commands)."""
    config = merger_agent_config()

    for tool in ("read", "glob", "grep", "list", "bash"):
        assert config.permission[tool] == "allow", f"{tool} should be allowed"


def test_merger_permissions_deny_editing_and_research() -> None:
    """Merger denies edit, task, websearch, webfetch."""
    config = merger_agent_config()

    for tool in ("edit", "task", "websearch", "webfetch"):
        assert config.permission[tool] == "deny", f"{tool} should be denied"


def test_merger_permissions_allow_mcp_servers() -> None:
    """Merger allows serena, gitnexus, context7, hindsight MCP tools."""
    config = merger_agent_config()

    for pattern in ("serena_*", "gitnexus_*", "context7_*", "hindsight_*"):
        assert config.permission[pattern] == "allow", f"{pattern} should be allowed"


def test_merger_no_pullmd_or_firecrawl() -> None:
    """Merger does not need pullmd or firecrawl MCP tools."""
    config = merger_agent_config()

    assert "pullmd_*" not in config.permission
    assert "firecrawl_*" not in config.permission


def test_compile_all_agents_includes_merger() -> None:
    """compile_all_agents produces merger entry with correct permissions."""
    result = compile_all_agents(
        {
            "coder": "qwen/qwen-2.5-coder",
            "reviewer": "anthropic/claude-sonnet-4-20250514",
            "merger": "anthropic/claude-sonnet-4-20250514",
        }
    )

    assert "merger" in result
    assert result["merger"]["model"] == "anthropic/claude-sonnet-4-20250514"
    assert result["merger"]["temperature"] == 0.1
    assert result["merger"]["permission"]["bash"] == "allow"
    assert result["merger"]["permission"]["edit"] == "deny"


# --- Prompt content tests ---

PROMPT_PATH = AGENTS_SOURCE_DIR / "merger" / "prompt.md"


def _read_prompt() -> str:
    assert PROMPT_PATH.is_file(), f"Merger prompt not found at {PROMPT_PATH}"
    return PROMPT_PATH.read_text()


def test_prompt_terminal_state_is_done() -> None:
    """Terminal state is Done, not Merged — in states section."""
    prompt = _read_prompt()

    states_section = ""
    for section in prompt.split("## "):
        if section.startswith("Ticket States"):
            states_section = section
            break
    assert states_section, "Ticket States section not found"
    assert "Done" in states_section
    assert "Merged" not in states_section


def test_prompt_no_ready_for_security_review() -> None:
    """No references to Ready for Security Review anywhere."""
    prompt = _read_prompt()

    assert "Ready for Security Review" not in prompt


def test_prompt_single_reviewer_verification() -> None:
    """Verifies single combined review, not separate code + security."""
    prompt = _read_prompt()

    assert "review" in prompt.lower()
    # Should NOT reference separate security review agent/stage
    assert "security review" not in prompt.lower() or "separate security" not in prompt.lower()
    # Should not require both code review AND security review as separate checks
    assert "security review passed" not in prompt.lower()


def test_prompt_risk_based_routing() -> None:
    """Risk 1-3 merge to Done, risk 4-5 to Human Merge."""
    prompt = _read_prompt()

    assert "Done" in prompt
    assert "Human Merge" in prompt
    # Risk thresholds
    assert "3" in prompt
    assert "4" in prompt
    assert "5" in prompt


def test_prompt_scope_mismatch_to_todo() -> None:
    """Scope mismatch moves ticket to To Do with comment."""
    prompt = _read_prompt()

    assert "To Do" in prompt
    assert "mismatch" in prompt.lower() or "gap" in prompt.lower()


def test_prompt_merger_loop_state_markers() -> None:
    """Prompt has CODE MERGER LOOP STATE markers."""
    prompt = _read_prompt()

    assert "CODE MERGER LOOP STATE" in prompt


def test_prompt_uses_gh_for_merge() -> None:
    """Merger uses gh for merge operations, bash restricted to gh."""
    prompt = _read_prompt()

    assert "gh" in prompt
    assert "only" in prompt.lower() or "restrict" in prompt.lower()


def test_prompt_excludes_merged_state_globally() -> None:
    """The word Merged should not appear as a ticket state anywhere."""
    prompt = _read_prompt()

    # Check that Merged doesn't appear as a state transition target
    assert "move the ticket to `Merged`" not in prompt
    assert "move to `Merged`" not in prompt


def test_prompt_tool_preferences() -> None:
    """Tool Preferences references Serena, GitNexus, Context7, Hindsight."""
    prompt = _read_prompt()

    assert "## Tool Preferences" in prompt
    for tool in ("Serena", "GitNexus", "Context7", "Hindsight"):
        assert tool in prompt, f"Missing tool reference: {tool}"


def test_prompt_hindsight_retain() -> None:
    """Retains merge outcomes to Hindsight."""
    prompt = _read_prompt()

    assert "hindsight" in prompt.lower()
