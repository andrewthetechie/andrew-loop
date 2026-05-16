"""Project initialization for orch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orch.config import Config
from orch.db import Database

logger = logging.getLogger(__name__)

ExternalRunner = Callable[[str, list[str]], Awaitable[bool]]
StepCallback = Callable[[str, str], None]  # (step_name, status)


_GITIGNORE_ENTRIES = [
    "# orch agent system",
    ".orchestra/",
    "opencode.json",
    "ORCH_DISPATCH_*.md",
    "# opencode local config (managed by orch init)",
    ".opencode/",
    "# serena project files",
    ".serena/",
]


def _ensure_gitignore(repo_root: Path) -> None:
    """Add orch-managed entries to .gitignore if not already present."""
    gitignore = repo_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.is_file() else ""
    additions = [e for e in _GITIGNORE_ENTRIES if e not in existing and not e.startswith("#")]
    if not additions:
        return
    sep = "\n" if existing and not existing.endswith("\n") else ""
    block = "\n".join(["", "# orch agent system", *additions, ""])
    gitignore.write_text(existing + sep + block)


def _update_config_bank_id(config_path: Path, new_bank_id: str) -> None:
    """Write the new bank_id into .orchestra/config.toml in-place."""
    text = config_path.read_text()
    updated = re.sub(r'(?m)^bank_id\s*=\s*"[^"]*"', f'bank_id = "{new_bank_id}"', text)
    if updated == text:
        # bank_id line not present yet — append to [hindsight] section
        updated = re.sub(
            r"(\[hindsight\][^\[]*)",
            lambda m: m.group(0).rstrip() + f'\nbank_id = "{new_bank_id}"\n',
            text,
        )
    config_path.write_text(updated)


def _make_alembic_cfg(db_path: Path) -> object:
    from alembic.config import Config as AlembicConfig

    # Locate alembic.ini and migrations/ relative to this file so the paths
    # are correct regardless of the caller's working directory.
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    migrations_dir = alembic_ini.parent / "migrations"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _alembic_upgrade(db_path: Path) -> None:
    """Run ``alembic upgrade head`` against an existing database."""
    from alembic import command as alembic_command

    alembic_command.upgrade(_make_alembic_cfg(db_path), "head")


def _alembic_stamp(db_path: Path, revision: str = "head") -> None:
    """Stamp a freshly-created database so Alembic knows it is already at head."""
    from alembic import command as alembic_command

    alembic_command.stamp(_make_alembic_cfg(db_path), revision)


_DEFAULT_CONFIG = """\
[router]
poll_interval = 10.0
max_rework_loops = 3

[webhook]
url = ""
triggers = ["Needs Human Review", "Human Merge"]

[harness]
command = "opencode run --agent {agent} --format json --dir {worktree_dir}"

[validation]
commands = []
"""


@dataclass
class InitResult:
    """Result of an init_project run, with per-step status."""

    config_status: str = "skipped"
    db_status: str = "skipped"
    agents_status: str = "skipped"
    hindsight_status: str = "skipped"
    serena_status: str = "skipped"
    gitnexus_status: str = "skipped"


async def default_runner(name: str, args: list[str]) -> bool:
    """Default external tool runner — runs subprocess commands."""

    cmd_map = {
        "hindsight": ["hindsight", *args],
        "serena": ["serena", *args],
        "gitnexus": ["npx", "gitnexus", *args],
    }
    cmd = cmd_map.get(name, [name, *args])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.wait()
    return proc.returncode == 0


async def init_project(
    repo_root: Path,
    *,
    run_external: ExternalRunner | None = None,
    on_step: StepCallback | None = None,
    on_hindsight_bank_failed: Callable[[str], Awaitable[str | None]] | None = None,
) -> InitResult:
    """Initialize orch state for a repo.

    State lives in ~/.local/share/orch/{repo_id}/ (outside the git repo) to
    prevent data loss from git operations. The repo itself gets only gitignore
    entries and opencode config.

    Args:
        repo_root: Target repository path.
        run_external: Runner for external CLI tools (serena, gitnexus).
        on_step: Callback fired after each step with (name, status).
        on_hindsight_bank_failed: Async callback when bank creation fails.
    """
    from orch.state import resolve_state_dir

    def _emit(name: str, status: str) -> None:
        if on_step is not None:
            on_step(name, status)

    result = InitResult()

    cfg = Config.load(repo_root=repo_root)
    state_dir = resolve_state_dir(repo_root, base_dir=cfg.state.base_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    (state_dir / "worktrees").mkdir(exist_ok=True)

    # Ensure orch-managed files are gitignored in the target repo
    _ensure_gitignore(repo_root)

    # Config lives in state_dir (not inside the git repo)
    config_path = state_dir / "config.toml"
    if config_path.exists():
        result.config_status = "exists"
    else:
        config_path.write_text(_DEFAULT_CONFIG)
        result.config_status = "created"
    _emit("config", result.config_status)

    # Database lives in state_dir
    db_path = state_dir / "state.db"
    if db_path.exists():
        result.db_status = "exists"
        _alembic_upgrade(db_path)
    else:
        async with Database(db_path):
            pass
        _alembic_stamp(db_path)
        result.db_status = "created"
    _emit("db", result.db_status)

    # Compile agent configs into opencode.json
    try:
        from orch.agents.config import (
            AGENTS_SOURCE_DIR,
            KNOWN_AGENTS,
            compile_all_agents,
            copy_plugin_files,
            copy_prompt_files,
            copy_tool_files,
            merge_opencode_json,
            setup_opencode_deps,
        )
        from orch.backends import load_configured_backends

        agent_models = {
            name: getattr(cfg.agents, name).model
            for name in ("coder", "reviewer", "merger", "decomposer")
            if getattr(cfg.agents, name).model
        }
        _HIDDEN_HELPER_CONFIG_KEYS = {
            "patch_reviewer": "patch-reviewer",
            "codebase_scout": "codebase-scout",
        }
        for config_key, agent_name in _HIDDEN_HELPER_CONFIG_KEYS.items():
            helper_model = getattr(cfg.agents, config_key).model
            if helper_model:
                agent_models[agent_name] = helper_model

        if agent_models:
            configured_backends = load_configured_backends(repo_root, config=cfg)
            compiled = compile_all_agents(
                agent_models,
                configured_backends=configured_backends,
            )
            opencode_path = repo_root / "opencode.json"
            merge_result = merge_opencode_json(opencode_path, compiled)
            opencode_path.write_text(json.dumps(merge_result.config, indent=2) + "\n")
            copy_prompt_files(
                KNOWN_AGENTS,
                source_base=AGENTS_SOURCE_DIR,
                target_repo=repo_root,
            )
            copy_tool_files(repo_root)
            copy_plugin_files(repo_root)
            setup_opencode_deps(repo_root)
            result.agents_status = "created"
        else:
            result.agents_status = "skipped"
    except Exception:
        logger.exception("Failed to compile agent configs")
        result.agents_status = "failed"
    _emit("agents", result.agents_status)

    # Hindsight bank + mental model setup
    if cfg.hindsight.url and cfg.hindsight.bank_id:
        from orch.hindsight import setup_hindsight

        project_name = repo_root.name
        bank_id = f"{cfg.hindsight.bank_id}-{project_name}"
        hindsight_result = await setup_hindsight(
            url=cfg.hindsight.url,
            bank_id=bank_id,
            api_key=cfg.hindsight.api_key,
        )

        if hindsight_result.bank_status == "failed" and on_hindsight_bank_failed is not None:
            new_bank_id = await on_hindsight_bank_failed(bank_id)
            if new_bank_id:
                _update_config_bank_id(config_path, new_bank_id)
                hindsight_result = await setup_hindsight(
                    url=cfg.hindsight.url,
                    bank_id=new_bank_id,
                    api_key=cfg.hindsight.api_key,
                )

        result.hindsight_status = hindsight_result.status
    _emit("hindsight", result.hindsight_status)

    # External tools
    if run_external is not None:
        repo = str(repo_root)

        result.serena_status = "skipped"
        _emit(
            "serena", "run 'serena project create .' then 'serena project index' to set up Serena"
        )

        await run_external("gitnexus", ["setup"])
        if await run_external("gitnexus", ["analyze", repo]):
            result.gitnexus_status = "created"
        else:
            result.gitnexus_status = "failed"
        _emit("gitnexus", result.gitnexus_status)

    return result
