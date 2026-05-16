"""Agent configuration models for opencode compilation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orch.backends import BackendDefinition

AGENTS_SOURCE_DIR = Path(__file__).parent

# .opencode/ lives at the repo root — 4 levels up from src/orch/agents/
_OPENCODE_SOURCE_DIR = Path(__file__).parent.parent.parent.parent / ".opencode"
TOOLS_SOURCE_DIR = _OPENCODE_SOURCE_DIR / "tools"
PLUGIN_SOURCE_DIR = _OPENCODE_SOURCE_DIR / "plugin"
HIDDEN_HELPER_SEMAPHORE_PLUGIN = "./.opencode/plugin/hidden-helper-semaphore.js"


class AgentConfig(BaseModel):
    """Base agent config that serializes to opencode JSON format."""

    name: str
    description: str
    mode: str = "primary"
    hidden: bool = False
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
        if self.hidden:
            result["hidden"] = True
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
    plugins = existing.get("plugin")
    if not isinstance(plugins, list):
        plugins = []
    if HIDDEN_HELPER_SEMAPHORE_PLUGIN not in plugins:
        plugins.append(HIDDEN_HELPER_SEMAPHORE_PLUGIN)
    existing["plugin"] = plugins

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
        steps=90,
        prompt_file="coder.md",
        permission={
            "edit": "allow",
            "bash": "allow",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "task": {
                "*": "deny",
                "codebase-scout": "allow",
                "leaf-coder": "allow",
                "patch-reviewer": "allow",
            },
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "allow",
            "ticket-update": "allow",
            "ticket-comment": "allow",
            "ticket-list": "allow",
            "delegation-record": "allow",
            "validate": "allow",
            "pr-create": "allow",
            "pr-update": "allow",
            "serena_*": "deny",
            "gitnexus_*": "deny",
            "context7_*": "deny",
            "pullmd_*": "deny",
            "hindsight_*": "deny",
            "firecrawl_*": "deny",
            "skill": "deny",
        },
    )


def reviewer_agent_config() -> AgentConfig:
    """Factory for the reviewer agent config with v1 permissions."""
    return AgentConfig(
        name="reviewer",
        description="Reviews pull requests for code quality and security",
        mode="primary",
        temperature=0.3,
        steps=85,
        prompt_file="reviewer.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "bash": "allow",
            "external_directory": {
                "/tmp/**": "allow",
                "/private/tmp/**": "allow",
            },
            "edit": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "allow",
            "ticket-update": "allow",
            "ticket-comment": "allow",
            "gitnexus_*": "allow",
            # Serena: read-only
            "serena_*": "deny",
            "serena_activate_project": "allow",
            "serena_find_*": "allow",
            "serena_get_*": "allow",
            "serena_search_*": "allow",
            "serena_read_*": "allow",
            "serena_list_*": "allow",
            "serena_check_*": "allow",
            "serena_onboarding": "allow",
            "serena_initial_instructions": "allow",
            # Hindsight: retain and recall only
            "hindsight_*": "deny",
            "hindsight_retain": "allow",
            "hindsight_sync_retain": "allow",
            "hindsight_recall": "allow",
            "hindsight_reflect": "allow",
            # Deny unused servers
            "context7_*": "deny",
            "pullmd_*": "deny",
            "firecrawl_*": "deny",
        },
    )


def merger_agent_config() -> AgentConfig:
    """Factory for the merger agent config with v1 permissions."""
    return AgentConfig(
        name="merger",
        description="Verifies scope match and merges eligible pull requests",
        mode="primary",
        temperature=0.1,
        steps=30,
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
            "pr-status": "allow",
            "pr-merge": "allow",
            "serena_*": "deny",
            "gitnexus_*": "deny",
            "context7_*": "deny",
            "pullmd_*": "deny",
            "hindsight_*": "deny",
            "firecrawl_*": "deny",
            "skill": "deny",
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
            "ticket-create": "allow",
            "ticket-list": "allow",
            "gitnexus_*": "allow",
            # Serena: read-only
            "serena_*": "deny",
            "serena_activate_project": "allow",
            "serena_find_*": "allow",
            "serena_get_*": "allow",
            "serena_search_*": "allow",
            "serena_read_*": "allow",
            "serena_list_*": "allow",
            "serena_check_*": "allow",
            "serena_onboarding": "allow",
            "serena_initial_instructions": "allow",
            # Hindsight: recall only (decomposer reads, does not write)
            "hindsight_*": "deny",
            "hindsight_recall": "allow",
            "hindsight_reflect": "allow",
            "hindsight_list_mental_models": "allow",
            "hindsight_get_mental_model": "allow",
            # Context7: keep for library lookups
            "context7_*": "allow",
            # Deny unused
            "pullmd_*": "deny",
            "firecrawl_*": "deny",
        },
    )


def leaf_coder_agent_config() -> AgentConfig:
    """Factory for the internal leaf-coder helper config."""
    return AgentConfig(
        name="leaf-coder",
        description="Internal hidden helper that implements one bounded code slice",
        mode="subagent",
        hidden=True,
        temperature=0.1,
        steps=45,
        prompt_file="leaf-coder.md",
        permission={
            "edit": "allow",
            "bash": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "deny",
            "ticket-update": "deny",
            "ticket-comment": "deny",
            "ticket-list": "deny",
            "delegation-record": "deny",
            "pr-create": "deny",
            "pr-update": "deny",
            "pr-status": "deny",
            "pr-merge": "deny",
            "serena_*": "deny",
            "gitnexus_*": "deny",
            "context7_*": "deny",
            "pullmd_*": "deny",
            "hindsight_*": "deny",
            "firecrawl_*": "deny",
            "skill": "deny",
        },
    )


def codebase_scout_agent_config() -> AgentConfig:
    """Factory for the internal codebase-scout helper config."""
    return AgentConfig(
        name="codebase-scout",
        description="Internal hidden helper that gathers codebase context without editing",
        mode="subagent",
        hidden=True,
        temperature=0.1,
        steps=30,
        prompt_file="codebase-scout.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "edit": "deny",
            "bash": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "deny",
            "ticket-update": "deny",
            "ticket-comment": "deny",
            "ticket-list": "deny",
            "delegation-record": "deny",
            "pr-create": "deny",
            "pr-update": "deny",
            "pr-status": "deny",
            "pr-merge": "deny",
            # Serena: read-only
            "serena_*": "deny",
            "serena_activate_project": "allow",
            "serena_find_*": "allow",
            "serena_get_*": "allow",
            "serena_search_*": "allow",
            "serena_read_*": "allow",
            "serena_list_*": "allow",
            "serena_check_*": "allow",
            "serena_onboarding": "allow",
            "serena_initial_instructions": "allow",
            "gitnexus_*": "allow",
            "context7_*": "deny",
            "pullmd_*": "deny",
            "hindsight_*": "deny",
            "firecrawl_*": "deny",
        },
    )


def patch_reviewer_agent_config() -> AgentConfig:
    """Factory for the internal patch-reviewer helper config."""
    return AgentConfig(
        name="patch-reviewer",
        description="Internal hidden helper that critiques diffs before external review",
        mode="subagent",
        hidden=True,
        temperature=0.2,
        steps=30,
        prompt_file="patch-reviewer.md",
        permission={
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "edit": "deny",
            "bash": "deny",
            "task": "deny",
            "websearch": "deny",
            "webfetch": "deny",
            "ticket-read": "deny",
            "ticket-update": "deny",
            "ticket-comment": "deny",
            "ticket-list": "deny",
            "delegation-record": "deny",
            "pr-create": "deny",
            "pr-update": "deny",
            "pr-status": "deny",
            "pr-merge": "deny",
            # Serena: read-only
            "serena_*": "deny",
            "serena_activate_project": "allow",
            "serena_find_*": "allow",
            "serena_get_*": "allow",
            "serena_search_*": "allow",
            "serena_read_*": "allow",
            "serena_list_*": "allow",
            "serena_check_*": "allow",
            "serena_onboarding": "allow",
            "serena_initial_instructions": "allow",
            "gitnexus_*": "allow",
            "context7_*": "deny",
            "pullmd_*": "deny",
            "hindsight_*": "deny",
            "firecrawl_*": "deny",
        },
    )


VISIBLE_AGENTS: list[AgentConfig] = [
    coder_agent_config(),
    reviewer_agent_config(),
    merger_agent_config(),
    decomposer_agent_config(),
]

HIDDEN_HELPER_AGENTS: list[AgentConfig] = [
    leaf_coder_agent_config(),
    codebase_scout_agent_config(),
    patch_reviewer_agent_config(),
]

KNOWN_AGENTS: list[AgentConfig] = [*VISIBLE_AGENTS, *HIDDEN_HELPER_AGENTS]


_CODER_INHERITING_HELPERS = frozenset({"leaf-coder"})


def compile_all_agents(
    agent_models: dict[str, str],
    *,
    configured_backends: list[BackendDefinition] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compile all known agents with models from orch config.

    Visible agents use their own configured model. leaf-coder inherits the
    coder model (it shares the active coder backend). Other hidden helpers
    (patch-reviewer, codebase-scout) require independent model configuration
    and are excluded when not configured.
    """
    result: dict[str, dict[str, Any]] = {}
    coder_model = agent_models.get("coder")
    for agent in KNOWN_AGENTS:
        model = agent_models.get(agent.name)
        if model is None and agent.hidden and agent.name in _CODER_INHERITING_HELPERS:
            model = coder_model
        if model:
            result[agent.name] = compile_agent(agent, model=model)
    if coder_model:
        for backend in configured_backends or []:
            if "coder" not in backend.logical_agents:
                continue
            alias_config = coder_agent_config()
            alias_config.name = backend.physical_alias
            result[backend.physical_alias] = compile_agent(alias_config, model=backend.model)
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


def copy_plugin_files(target_repo: Path) -> None:
    """Copy OpenCode plugin files to target repo's .opencode/plugin/."""
    plugin_dir = target_repo / ".opencode" / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for plugin_file in sorted(PLUGIN_SOURCE_DIR.glob("*.js")):
        dst = plugin_dir / plugin_file.name
        if plugin_file.resolve() != dst.resolve():
            shutil.copy2(plugin_file, dst)


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
