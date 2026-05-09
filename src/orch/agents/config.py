"""Agent configuration models for opencode compilation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

AGENTS_SOURCE_DIR = Path(__file__).parent

# .opencode/ lives at the repo root — 4 levels up from src/orch/agents/
_OPENCODE_SOURCE_DIR = Path(__file__).parent.parent.parent.parent / ".opencode"
TOOLS_SOURCE_DIR = _OPENCODE_SOURCE_DIR / "tools"


class AgentConfig(BaseModel):
    """Base agent config that serializes to opencode JSON format."""

    name: str
    description: str
    mode: str = "primary"
    temperature: float = 0.1
    steps: int = 50
    prompt_file: str = ""
    permission: dict[str, Any] = {}

    def to_opencode_json(self) -> dict[str, Any]:
        """Serialize to an opencode agent JSON config block (without model)."""
        result: dict[str, Any] = {
            "description": self.description,
            "mode": self.mode,
            "temperature": self.temperature,
            "steps": self.steps,
        }
        if self.prompt_file:
            result["prompt"] = f"{{file:./.opencode/prompts/{self.prompt_file}}}"
        if self.permission:
            result["permission"] = dict(self.permission)
        return result


def compile_agent(config: AgentConfig, *, model: str) -> dict[str, Any]:
    """Compile an AgentConfig with a model into a complete opencode agent JSON block."""
    result = config.to_opencode_json()
    result["model"] = model
    return result


class MergeResult(BaseModel):
    """Result of merging agent configs into opencode.json."""

    model_config = {"arbitrary_types_allowed": True}

    config: dict[str, Any]
    diffs: dict[str, dict[str, Any]] = {}


def merge_opencode_json(
    opencode_path: Path,
    agents: dict[str, dict[str, Any]],
) -> MergeResult:
    """Merge compiled agent configs into an existing opencode.json.

    Only updates orch-managed agent keys. Preserves all other config.
    Returns a MergeResult with the merged config and any diffs detected.
    """
    existing = json.loads(opencode_path.read_text()) if opencode_path.is_file() else {}

    if "agent" not in existing:
        existing["agent"] = {}

    diffs: dict[str, dict[str, Any]] = {}

    for name, new_config in agents.items():
        old_config = existing["agent"].get(name)
        if old_config is not None and old_config != new_config:
            diffs[name] = {"old": old_config, "new": new_config}
        existing["agent"][name] = new_config

    return MergeResult(config=existing, diffs=diffs)


def coder_agent_config() -> AgentConfig:
    """Factory for the coder agent config with v1 permissions."""
    return AgentConfig(
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
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "allow",
            "ticket-update": "allow",
            "ticket-comment": "allow",
            "ticket-list": "allow",
            "serena_*": "allow",
            "gitnexus_*": "allow",
            "context7_*": "allow",
            "pullmd_*": "allow",
            "hindsight_*": "allow",
            "firecrawl_*": "allow",
        },
    )


def reviewer_agent_config() -> AgentConfig:
    """Factory for the reviewer agent config with v1 permissions."""
    return AgentConfig(
        name="reviewer",
        description="Reviews pull requests for code quality and security",
        mode="primary",
        temperature=0.3,
        steps=50,
        prompt_file="reviewer.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "bash": "allow",
            "edit": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "allow",
            "ticket-update": "allow",
            "ticket-comment": "allow",
            "serena_*": "allow",
            "gitnexus_*": "allow",
            "context7_*": "allow",
            "hindsight_*": "allow",
        },
    )


def merger_agent_config() -> AgentConfig:
    """Factory for the merger agent config with v1 permissions."""
    return AgentConfig(
        name="merger",
        description="Verifies scope match and merges eligible pull requests",
        mode="primary",
        temperature=0.1,
        steps=50,
        prompt_file="merger.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "bash": "allow",
            "edit": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "allow",
            "ticket-update": "allow",
            "ticket-comment": "allow",
            "ticket-list": "allow",
            "pr-status": "allow",
            "pr-merge": "allow",
            "serena_*": "allow",
            "gitnexus_*": "allow",
            "context7_*": "allow",
            "hindsight_*": "allow",
        },
    )


def decomposer_agent_config() -> AgentConfig:
    """Factory for the decomposer agent config with v1 permissions."""
    return AgentConfig(
        name="decomposer",
        description="Grills PRDs and decomposes them into well-specified tickets",
        mode="primary",
        temperature=0.5,
        steps=100,
        prompt_file="decomposer.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "bash": "allow",
            "edit": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "serena_*": "allow",
            "gitnexus_*": "allow",
            "context7_*": "allow",
            "pullmd_*": "allow",
            "hindsight_*": "allow",
            "firecrawl_*": "allow",
        },
    )


KNOWN_AGENTS: list[AgentConfig] = [
    coder_agent_config(),
    reviewer_agent_config(),
    merger_agent_config(),
    decomposer_agent_config(),
]


def compile_all_agents(
    agent_models: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Compile all known agents with models from orch config.

    Only includes agents that have a model configured.
    """
    result: dict[str, dict[str, Any]] = {}
    for agent in KNOWN_AGENTS:
        model = agent_models.get(agent.name)
        if model:
            result[agent.name] = compile_agent(agent, model=model)
    return result


def copy_tool_files(target_repo: Path) -> None:
    """Copy opencode custom tool TypeScript files to target repo's .opencode/tools/.

    Copies all *.ts files from the orch source .opencode/tools/ to the target
    repo. Idempotent — overwrites existing files to keep tools in sync.
    """
    if not TOOLS_SOURCE_DIR.is_dir():
        return
    tools_dir = target_repo / ".opencode" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for tool_file in sorted(TOOLS_SOURCE_DIR.glob("*.ts")):
        dst = tools_dir / tool_file.name
        if tool_file.resolve() != dst.resolve():
            shutil.copy2(tool_file, dst)


def setup_opencode_deps(target_repo: Path) -> None:
    """Ensure .opencode/package.json exists and bun install has been run.

    Copies package.json from the orch source .opencode/ if absent, then runs
    bun install in the target's .opencode/ directory when node_modules is missing.
    """
    import subprocess

    opencode_dir = target_repo / ".opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    pkg_source = _OPENCODE_SOURCE_DIR / "package.json"
    pkg_target = opencode_dir / "package.json"
    if not pkg_target.is_file() and pkg_source.is_file():
        shutil.copy2(pkg_source, pkg_target)

    if not (opencode_dir / "node_modules").is_dir():
        subprocess.run(
            ["bun", "install"],
            cwd=opencode_dir,
            check=False,
            capture_output=True,
        )


def copy_prompt_files(
    configs: list[AgentConfig],
    *,
    source_base: Path,
    target_repo: Path,
) -> None:
    """Copy agent prompt files from source to target repo's .opencode/prompts/."""
    prompts_dir = target_repo / ".opencode" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    for config in configs:
        if not config.prompt_file:
            continue
        source = source_base / config.name / "prompt.md"
        if source.is_file():
            dst = prompts_dir / config.prompt_file
            if source.resolve() != dst.resolve():
                shutil.copy2(source, dst)
