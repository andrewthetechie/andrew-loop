"""Project initialization for orch."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from orch.db import Database

logger = logging.getLogger(__name__)

ExternalRunner = Callable[[str, list[str]], Awaitable[bool]]

_DEFAULT_CONFIG = """\
[router]
poll_interval = 10.0

[webhook]
url = ""
triggers = ["Needs Human Review", "Human Merge"]

[harness]
command = "opencode run --agent {agent} --format json --dir {worktree_dir}"

[validation]
commands = []

[hindsight]
url = "http://localhost:8888"
bank_id = ""
api_key = ""
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
    import asyncio

    cmd_map = {
        "hindsight": ["hindsight", *args],
        "serena": ["serena", *args],
        "gitnexus": ["npx", "gitnexus", *args],
    }
    cmd = cmd_map.get(name)
    if cmd is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.wait()
    return proc.returncode == 0


async def init_project(
    repo_root: Path,
    *,
    run_external: ExternalRunner | None = None,
) -> InitResult:
    """Initialize .orchestra/ directory structure in a repo."""
    result = InitResult()
    orchestra_dir = repo_root / ".orchestra"

    # Create directory structure
    orchestra_dir.mkdir(exist_ok=True)
    (orchestra_dir / "logs").mkdir(exist_ok=True)
    (orchestra_dir / "worktrees").mkdir(exist_ok=True)

    # Generate default config.toml
    config_path = orchestra_dir / "config.toml"
    if config_path.exists():
        result.config_status = "exists"
    else:
        config_path.write_text(_DEFAULT_CONFIG)
        result.config_status = "created"

    # Initialize database
    db_path = orchestra_dir / "state.db"
    if db_path.exists():
        result.db_status = "exists"
    else:
        async with Database(db_path):
            pass
        result.db_status = "created"

    # Compile agent configs into opencode.json
    try:
        from orch.agents.config import (
            AGENTS_SOURCE_DIR,
            KNOWN_AGENTS,
            compile_all_agents,
            copy_prompt_files,
            merge_opencode_json,
        )
        from orch.config import Config

        cfg = Config.load(repo_root=repo_root)
        agent_models = {
            name: getattr(cfg.agents, name).model
            for name in ("coder", "reviewer", "merger", "decomposer")
            if getattr(cfg.agents, name).model
        }

        if agent_models:
            compiled = compile_all_agents(agent_models)
            opencode_path = repo_root / "opencode.json"
            merge_result = merge_opencode_json(opencode_path, compiled)
            opencode_path.write_text(json.dumps(merge_result.config, indent=2) + "\n")
            copy_prompt_files(
                KNOWN_AGENTS,
                source_base=AGENTS_SOURCE_DIR,
                target_repo=repo_root,
            )
            result.agents_status = "created"
        else:
            result.agents_status = "skipped"
    except Exception:
        logger.exception("Failed to compile agent configs")
        result.agents_status = "failed"

    # External tools — only run when a runner is provided
    if run_external is not None:
        repo = str(repo_root)

        if await run_external("hindsight", ["bank", "create", "--dir", repo]):
            result.hindsight_status = "created"
        else:
            result.hindsight_status = "failed"

        if await run_external("serena", ["project", "create", "--index", "--dir", repo]):
            result.serena_status = "created"
        else:
            result.serena_status = "failed"

        if await run_external("gitnexus", ["analyze", repo]):
            result.gitnexus_status = "created"
        else:
            result.gitnexus_status = "failed"

    return result
