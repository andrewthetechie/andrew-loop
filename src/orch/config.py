"""Three-tier configuration loading for orch.

Precedence: CLI overrides > env vars > repo config > global config > defaults.
Uses tomllib (stdlib) for TOML parsing and Pydantic v2 for validation.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class WebhookConfig(BaseModel):
    url: str = ""
    triggers: list[str] = ["Needs Human Review", "Human Merge"]


class HarnessConfig(BaseModel):
    command: str = "opencode run --agent {agent} --format json --dir {worktree_dir}"


class ValidationConfig(BaseModel):
    commands: list[str] = []


class RouterConfig(BaseModel):
    poll_interval: float = 10.0


class HindsightConfig(BaseModel):
    url: str = "http://localhost:8888"
    bank_id: str = ""
    api_key: str = ""


class FirecrawlConfig(BaseModel):
    url: str = ""
    api_key: str = ""


class Context7Config(BaseModel):
    api_key: str = ""


class PullmdConfig(BaseModel):
    url: str = ""


class McpConfig(BaseModel):
    firecrawl: FirecrawlConfig = FirecrawlConfig()
    context7: Context7Config = Context7Config()
    pullmd: PullmdConfig = PullmdConfig()


class AgentModelConfig(BaseModel):
    model: str = ""


class AgentsConfig(BaseModel):
    coder: AgentModelConfig = AgentModelConfig()
    reviewer: AgentModelConfig = AgentModelConfig()
    merger: AgentModelConfig = AgentModelConfig()
    decomposer: AgentModelConfig = AgentModelConfig()


class StateConfig(BaseModel):
    base_dir: str = "~/.local/share/orch"


class GithubConfig(BaseModel):
    prd_labels: list[str] = []


class Config(BaseModel):
    webhook: WebhookConfig = WebhookConfig()
    harness: HarnessConfig = HarnessConfig()
    validation: ValidationConfig = ValidationConfig()
    router: RouterConfig = RouterConfig()
    hindsight: HindsightConfig = HindsightConfig()
    mcp: McpConfig = McpConfig()
    agents: AgentsConfig = AgentsConfig()
    state: StateConfig = StateConfig()
    github: GithubConfig = GithubConfig()

    @classmethod
    def load(
        cls,
        repo_root: Path | None = None,
        *,
        global_config_path: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Config:
        """Load config with three-tier precedence: overrides > state_dir > global > defaults.

        Config is loaded from (in ascending priority):
          1. Defaults
          2. ~/.config/orchestra/config.toml  (global)
          3. ~/.local/share/orch/{repo_id}/config.toml  (state dir, repo-specific)
          4. Explicit overrides
        """
        merged: dict[str, Any] = {}

        # Load global config
        if global_config_path is None:
            global_config_path = Path.home() / ".config" / "orchestra" / "config.toml"
        global_data = _load_toml(global_config_path)
        _deep_merge(merged, global_data)

        # Load state-dir config (repo-specific, replaces old .orchestra/config.toml)
        if repo_root is not None:
            # Try new state dir location first
            try:
                from orch.state import resolve_state_dir

                # Read base_dir from merged so far (in case global config overrides it)
                base_dir = merged.get("state", {}).get("base_dir")
                state_dir = resolve_state_dir(repo_root, base_dir=base_dir)
                state_config = _load_toml(state_dir / "config.toml")
                _deep_merge(merged, state_config)
            except Exception:
                pass

            # Backward compat: also load from old .orchestra/config.toml if it exists
            old_path = repo_root / ".orchestra" / "config.toml"
            if old_path.is_file():
                _deep_merge(merged, _load_toml(old_path))

        # Apply explicit overrides
        if overrides:
            _deep_merge(merged, overrides)

        return cls.model_validate(merged)


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning empty dict if it doesn't exist."""
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Deep merge override into base, mutating base in place."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
