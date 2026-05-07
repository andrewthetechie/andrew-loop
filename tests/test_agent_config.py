"""Tests for agent config infrastructure."""

import json
from pathlib import Path

from orch.agents.config import (
    AgentConfig,
    compile_agent,
    copy_prompt_files,
    merge_opencode_json,
)


def test_agent_config_serializes_to_opencode_json() -> None:
    """AgentConfig produces a valid opencode agent JSON block."""
    config = AgentConfig(
        name="coder",
        description="Implements tickets using test-first development",
        mode="primary",
        temperature=0.1,
        steps=50,
        prompt_file="coder.md",
        permission={
            "edit": "allow",
            "bash": "allow",
            "read": "allow",
            "websearch": "deny",
            "webfetch": "deny",
        },
    )

    result = config.to_opencode_json()

    assert result["description"] == "Implements tickets using test-first development"
    assert result["mode"] == "primary"
    assert result["temperature"] == 0.1
    assert result["steps"] == 50
    assert result["prompt"] == "{file:./.opencode/prompts/coder.md}"
    assert result["permission"]["edit"] == "allow"
    assert result["permission"]["bash"] == "allow"
    assert result["permission"]["websearch"] == "deny"
    # model is NOT in the output — injected later from orch config
    assert "model" not in result


def test_compile_agent_injects_model() -> None:
    """compile_agent merges model from orch config into the opencode JSON block."""
    config = AgentConfig(
        name="coder",
        description="Implements tickets",
        mode="primary",
        prompt_file="coder.md",
    )

    result = compile_agent(config, model="anthropic/claude-sonnet-4-20250514")

    assert result["model"] == "anthropic/claude-sonnet-4-20250514"
    assert result["description"] == "Implements tickets"
    assert result["prompt"] == "{file:./.opencode/prompts/coder.md}"


def test_orch_config_has_per_agent_model() -> None:
    """Orch Config supports [agents.coder] model = '...' per-agent model config."""
    from orch.config import Config

    config = Config.load(
        overrides={
            "agents": {
                "coder": {"model": "qwen/qwen-2.5-coder"},
                "reviewer": {"model": "anthropic/claude-sonnet-4-20250514"},
            }
        }
    )

    assert config.agents.coder.model == "qwen/qwen-2.5-coder"
    assert config.agents.reviewer.model == "anthropic/claude-sonnet-4-20250514"


def test_merge_preserves_existing_opencode_json(tmp_path: Path) -> None:
    """Merging agent configs preserves user's other keys in opencode.json."""
    existing = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"anthropic": {"apiKey": "sk-xxx"}},
        "agent": {
            "my-custom-agent": {"description": "User's custom agent", "mode": "subagent"},
        },
    }
    opencode_path = tmp_path / "opencode.json"
    opencode_path.write_text(json.dumps(existing))

    agents_to_merge = {
        "coder": {"description": "Implements tickets", "mode": "primary", "model": "qwen/coder"},
    }

    merge_result = merge_opencode_json(opencode_path, agents_to_merge)

    # User's custom agent preserved
    assert "my-custom-agent" in merge_result.config["agent"]
    assert merge_result.config["agent"]["my-custom-agent"]["description"] == "User's custom agent"
    # Orch agent added
    assert "coder" in merge_result.config["agent"]
    assert merge_result.config["agent"]["coder"]["model"] == "qwen/coder"
    # Other top-level keys preserved
    assert merge_result.config["$schema"] == "https://opencode.ai/config.json"
    assert merge_result.config["provider"]["anthropic"]["apiKey"] == "sk-xxx"
    # No diffs — coder didn't exist before
    assert merge_result.diffs == {}


def test_merge_detects_diffs_on_existing_agent(tmp_path: Path) -> None:
    """When an orch-managed agent already exists with different config, diffs are returned."""
    existing = {
        "agent": {
            "coder": {"description": "Old description", "mode": "primary", "model": "old/model"},
        },
    }
    opencode_path = tmp_path / "opencode.json"
    opencode_path.write_text(json.dumps(existing))

    agents_to_merge = {
        "coder": {"description": "New description", "mode": "primary", "model": "new/model"},
    }

    merge_result = merge_opencode_json(opencode_path, agents_to_merge)

    assert "coder" in merge_result.diffs
    assert merge_result.diffs["coder"]["old"]["description"] == "Old description"
    assert merge_result.diffs["coder"]["new"]["description"] == "New description"
    # Config still gets updated
    assert merge_result.config["agent"]["coder"]["description"] == "New description"


def test_copy_prompt_files_to_target(tmp_path: Path) -> None:
    """copy_prompt_files copies agent prompts from source to .opencode/prompts/."""
    # Create a source prompt
    source_dir = tmp_path / "source" / "agents" / "coder"
    source_dir.mkdir(parents=True)
    (source_dir / "prompt.md").write_text("# Coder Prompt\nYou are a coder.")

    configs = [
        AgentConfig(name="coder", description="Coder", prompt_file="coder.md"),
    ]

    target_repo = tmp_path / "repo"
    target_repo.mkdir()

    copy_prompt_files(
        configs,
        source_base=tmp_path / "source" / "agents",
        target_repo=target_repo,
    )

    target = target_repo / ".opencode" / "prompts" / "coder.md"
    assert target.is_file()
    assert "You are a coder." in target.read_text()


async def test_init_compiles_agents_into_opencode_json(tmp_path: Path) -> None:
    """orch init creates opencode.json with compiled agent configs when models are configured."""
    # Create a repo with orch config that has agent models
    repo = tmp_path / "repo"
    repo.mkdir()
    orchestra_dir = repo / ".orchestra"
    orchestra_dir.mkdir()
    config_toml = orchestra_dir / "config.toml"
    config_toml.write_text('[agents.coder]\nmodel = "qwen/qwen-2.5-coder"\n')

    # Create a source prompt for the coder agent
    from orch.agents.config import AGENTS_SOURCE_DIR

    coder_dir = AGENTS_SOURCE_DIR / "coder"
    has_prompt = coder_dir.is_dir() and (coder_dir / "prompt.md").is_file()

    from orch.init import init_project

    result = await init_project(repo)

    opencode_path = repo / "opencode.json"
    if has_prompt:
        assert opencode_path.is_file()
        data = json.loads(opencode_path.read_text())
        assert "agent" in data
        assert "coder" in data["agent"]
        assert data["agent"]["coder"]["model"] == "qwen/qwen-2.5-coder"
    assert result.agents_status in ("created", "skipped")
