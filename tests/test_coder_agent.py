"""Tests for coder agent config and prompt."""

from orch.agents.config import (
    AGENTS_SOURCE_DIR,
    AgentConfig,
    coder_agent_config,
    compile_all_agents,
)


def test_coder_factory_returns_agent_config() -> None:
    """coder_agent_config() returns an AgentConfig with coder identity."""
    config = coder_agent_config()

    assert isinstance(config, AgentConfig)
    assert config.name == "coder"
    assert config.description == "Implements tickets using test-first development"
    assert config.mode == "primary"
    assert config.temperature == 0.1
    assert config.steps == 50
    assert config.prompt_file == "coder.md"


def test_coder_permissions_allow_editing_tools() -> None:
    """Coder allows edit, bash, read, glob, grep, list tools."""
    config = coder_agent_config()

    for tool in ("edit", "bash", "read", "glob", "grep", "list"):
        assert config.permission[tool] == "allow", f"{tool} should be allowed"


def test_coder_permissions_deny_research_tools() -> None:
    """Coder denies task, websearch, webfetch tools."""
    config = coder_agent_config()

    for tool in ("task", "websearch", "webfetch"):
        assert config.permission[tool] == "deny", f"{tool} should be denied"


def test_coder_permissions_allow_mcp_servers() -> None:
    """Coder allows all required MCP server tool patterns."""
    config = coder_agent_config()

    for pattern in (
        "serena_*",
        "gitnexus_*",
        "context7_*",
        "pullmd_*",
        "hindsight_*",
        "firecrawl_*",
    ):
        assert config.permission[pattern] == "allow", f"{pattern} should be allowed"


def test_coder_serializes_to_opencode_json() -> None:
    """Coder config serializes to valid opencode JSON with all permissions."""
    config = coder_agent_config()
    result = config.to_opencode_json()

    assert result["mode"] == "primary"
    assert result["temperature"] == 0.1
    assert result["steps"] == 50
    assert result["prompt"] == "{file:./.opencode/prompts/coder.md}"
    assert result["permission"]["edit"] == "allow"
    assert result["permission"]["task"] == "deny"
    assert result["permission"]["serena_*"] == "allow"
    assert "model" not in result


def test_compile_all_agents_includes_coder() -> None:
    """compile_all_agents produces coder entry with factory permissions."""
    result = compile_all_agents({"coder": "qwen/qwen-2.5-coder"})

    assert "coder" in result
    assert result["coder"]["model"] == "qwen/qwen-2.5-coder"
    assert result["coder"]["permission"]["edit"] == "allow"
    assert result["coder"]["permission"]["serena_*"] == "allow"
    assert result["coder"]["permission"]["websearch"] == "deny"


# --- Prompt content tests ---

PROMPT_PATH = AGENTS_SOURCE_DIR / "coder" / "prompt.md"


def _read_prompt() -> str:
    assert PROMPT_PATH.is_file(), f"Coder prompt not found at {PROMPT_PATH}"
    return PROMPT_PATH.read_text()


def test_prompt_has_required_sections() -> None:
    """Coder prompt contains all required section headers."""
    prompt = _read_prompt()

    required_sections = [
        "## Tool Preferences",
        "## Coder Loop",
        "## Ticket States",
        "## Stop Conditions",
        "## Validation",
        "## Commit",
    ]
    for section in required_sections:
        assert section in prompt, f"Missing section: {section}"


def test_prompt_tool_preferences_has_required_tools() -> None:
    """Tool Preferences references Serena, GitNexus, Context7, PullMD, Firecrawl."""
    prompt = _read_prompt()

    for tool in ("Serena", "GitNexus", "Context7", "PullMD", "Firecrawl"):
        assert tool in prompt, f"Missing tool reference: {tool}"

    # Specific tool names from acceptance criteria
    for tool_name in (
        "find_symbol",
        "gitnexus_impact",
        "gitnexus_detect_changes",
        "resolve-library-id",
    ):
        assert tool_name in prompt, f"Missing tool name: {tool_name}"


def test_prompt_expects_dispatch_payload() -> None:
    """Prompt expects rich dispatch payload via file, not ticket-read tool."""
    prompt = _read_prompt()

    assert "dispatch payload" in prompt.lower() or "dispatch" in prompt.lower()
    # Should not instruct the coder to fetch ticket data via tools
    assert "ticket-read" not in prompt


def test_prompt_uses_mechanical_checks_not_self_review() -> None:
    """Self-review step replaced with mechanical checks."""
    prompt = _read_prompt()

    # Should NOT have self-review subagent dispatch
    assert "review subagent" not in prompt.lower()
    assert "dispatch one `review`" not in prompt


def test_prompt_uses_custom_pr_tools() -> None:
    """Prompt uses pr-create/pr-update custom tools, not raw gh."""
    prompt = _read_prompt()

    assert "pr-create" in prompt or "pr_create" in prompt
    assert "pr-update" in prompt or "pr_update" in prompt


def test_prompt_has_conventional_commits_with_ticket_id() -> None:
    """Commit messages use conventional commit format referencing ticket ID."""
    prompt = _read_prompt()

    assert "conventional commit" in prompt.lower()
    assert "ticket" in prompt.lower()


def test_prompt_has_rework_handling() -> None:
    """Prompt handles rework from dispatch payload."""
    prompt = _read_prompt()

    assert "rework" in prompt.lower()


def test_prompt_has_coder_loop_state_markers() -> None:
    """Prompt retains CODER LOOP STATE markers."""
    prompt = _read_prompt()

    assert "CODER LOOP STATE" in prompt


def test_prompt_has_hindsight_retain() -> None:
    """Prompt retains validation failures to Hindsight via MCP."""
    prompt = _read_prompt()

    assert "hindsight" in prompt.lower()
    assert "validation" in prompt.lower()


def test_prompt_has_escalation_stop_condition() -> None:
    """Stop conditions include escalation for under-specified tickets."""
    prompt = _read_prompt()

    assert "escalate" in prompt.lower()
    assert "under-specified" in prompt.lower() or "underspecified" in prompt.lower()


def test_prompt_excludes_removed_states() -> None:
    """State list does not include Merged or Ready for Security Review."""
    prompt = _read_prompt()

    # These states were explicitly removed in v1 decisions
    assert "Merged" not in prompt.split("## ")[0]  # Not in state list area
    # Check the Ticket States section specifically
    states_section = ""
    for section in prompt.split("## "):
        if section.startswith("Ticket States"):
            states_section = section
            break
    assert states_section, "Ticket States section not found"
    assert "Merged" not in states_section
    assert "Ready for Security Review" not in states_section
