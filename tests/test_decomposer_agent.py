"""Tests for decomposer agent config and prompt."""

from orch.agents.config import (
    AGENTS_SOURCE_DIR,
    AgentConfig,
    compile_all_agents,
    decomposer_agent_config,
)


def test_decomposer_factory_returns_agent_config() -> None:
    """decomposer_agent_config() returns an AgentConfig with decomposer identity."""
    config = decomposer_agent_config()

    assert isinstance(config, AgentConfig)
    assert config.name == "decomposer"
    assert config.mode == "primary"
    assert config.temperature == 0.5
    assert config.steps == 100
    assert config.prompt_file == "decomposer.md"


def test_decomposer_permissions_allow_read_and_bash() -> None:
    """Decomposer allows read, glob, grep, list, bash (for custom tools)."""
    config = decomposer_agent_config()

    for tool in ("read", "glob", "grep", "list", "bash"):
        assert config.permission[tool] == "allow", f"{tool} should be allowed"


def test_decomposer_permissions_deny_editing_and_research() -> None:
    """Decomposer denies edit, task, websearch, webfetch."""
    config = decomposer_agent_config()

    for tool in ("edit", "task", "websearch", "webfetch"):
        assert config.permission[tool] == "deny", f"{tool} should be denied"


def test_decomposer_permissions_allow_all_mcp_servers() -> None:
    """Decomposer allows all 6 MCP tool patterns (heavy research agent)."""
    config = decomposer_agent_config()

    for pattern in (
        "serena_*",
        "gitnexus_*",
        "context7_*",
        "pullmd_*",
        "hindsight_*",
        "firecrawl_*",
    ):
        assert config.permission[pattern] == "allow", f"{pattern} should be allowed"


def test_compile_all_agents_includes_decomposer() -> None:
    """compile_all_agents produces decomposer entry with correct settings."""
    result = compile_all_agents(
        {
            "coder": "qwen/qwen-2.5-coder",
            "reviewer": "anthropic/claude-sonnet-4-20250514",
            "merger": "anthropic/claude-sonnet-4-20250514",
            "decomposer": "zhipu/glm-5.1",
        }
    )

    assert "decomposer" in result
    assert result["decomposer"]["model"] == "zhipu/glm-5.1"
    assert result["decomposer"]["temperature"] == 0.5
    assert result["decomposer"]["steps"] == 100
    assert result["decomposer"]["permission"]["bash"] == "allow"
    assert result["decomposer"]["permission"]["edit"] == "deny"


# --- Prompt content tests ---

PROMPT_PATH = AGENTS_SOURCE_DIR / "decomposer" / "prompt.md"


def _read_prompt() -> str:
    assert PROMPT_PATH.is_file(), f"Decomposer prompt not found at {PROMPT_PATH}"
    return PROMPT_PATH.read_text()


def test_prompt_grill_me_interactive_style() -> None:
    """Prompt describes interactive grilling: one question at a time, probes ambiguities."""
    prompt = _read_prompt()

    assert "one question" in prompt.lower() or "one at a time" in prompt.lower()
    assert "ambiguit" in prompt.lower()


def test_prompt_uses_ticket_custom_tools() -> None:
    """Prompt references ticket-create and ticket-list custom tools."""
    prompt = _read_prompt()

    assert "ticket-create" in prompt or "ticket_create" in prompt
    assert "ticket-list" in prompt or "ticket_list" in prompt


def test_prompt_tickets_land_in_draft() -> None:
    """Created tickets land in Draft state."""
    prompt = _read_prompt()

    assert "Draft" in prompt


def test_prompt_tickets_include_risk_and_dependencies() -> None:
    """Each ticket includes suggested risk score and dependency links."""
    prompt = _read_prompt()

    assert "risk score" in prompt.lower()
    assert "dependenc" in prompt.lower()


def test_prompt_tool_preferences() -> None:
    """Tool Preferences references all 5 MCP servers with specific tool names."""
    prompt = _read_prompt()

    assert "## Tool Preferences" in prompt
    for tool in ("Context7", "PullMD", "Firecrawl", "GitNexus", "Hindsight"):
        assert tool in prompt, f"Missing tool reference: {tool}"

    for name in ("resolve-library-id", "query-docs", "read_url", "gitnexus_query"):
        assert name in prompt, f"Missing tool name: {name}"


def test_prompt_decomposer_loop_state_markers() -> None:
    """Prompt has DECOMPOSER LOOP STATE markers."""
    prompt = _read_prompt()

    assert "DECOMPOSER LOOP STATE" in prompt


def test_prompt_hindsight_recall() -> None:
    """Recalls lessons-learned mental model via Hindsight."""
    prompt = _read_prompt()

    assert "hindsight" in prompt.lower()
    assert "lessons-learned" in prompt


def test_prompt_ticket_quality() -> None:
    """Tickets include file paths, acceptance criteria, test expectations."""
    prompt = _read_prompt()

    assert "file path" in prompt.lower()
    assert "acceptance criteria" in prompt.lower()
    assert "test expectation" in prompt.lower() or "test" in prompt.lower()


def test_prompt_checks_for_duplicates() -> None:
    """Prompt instructs to check for existing tickets before creating."""
    prompt = _read_prompt()

    assert "duplicate" in prompt.lower() or "existing ticket" in prompt.lower()
