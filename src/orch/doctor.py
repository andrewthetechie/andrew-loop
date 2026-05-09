"""Preflight checks for orch environment."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

ToolRunner = Callable[[str, list[str]], Awaitable[bool]]


@dataclass
class CheckResult:
    """Result of a single preflight check."""

    name: str
    passed: bool
    message: str


@dataclass
class DoctorResult:
    """Aggregate result of all doctor checks."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(c.passed for c in self.checks)


_REQUIRED_MCPS = {"context7", "firecrawl", "gitnexus", "serena", "hindsight"}

_REQUIRED_TABLES = {
    "tickets",
    "ticket_comments",
    "events",
    "ticket_dependencies",
    "subtask_context",
}


async def _check_db_schema(db_path: Path) -> CheckResult:
    """Verify database has the expected tables without auto-creating them."""
    import aiosqlite

    try:
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            rows = await cursor.fetchall()
            tables = {row[0] for row in rows}
    except Exception:
        return CheckResult("database", False, "state.db is not a valid database")

    missing = _REQUIRED_TABLES - tables
    if missing:
        return CheckResult("database", False, f"missing tables: {', '.join(sorted(missing))}")
    return CheckResult("database", True, "database schema valid")


def _check_opencode_mcps(repo_root: Path) -> CheckResult:
    """Verify all required MCPs are configured in opencode.json.

    opencode merges a global config (~/.config/opencode/opencode.json) with a
    repo-level config (opencode.json). MCPs may be defined in either location.
    """
    configured: set[str] = set()
    sources: list[str] = []

    # Global opencode config
    global_config = Path.home() / ".config" / "opencode" / "opencode.json"
    if global_config.is_file():
        try:
            data = json.loads(global_config.read_text())
            keys = set(data.get("mcp", {}).keys())
            if keys:
                configured |= keys
                sources.append(f"global ({len(keys)})")
        except json.JSONDecodeError:
            pass

    # Repo-level config
    repo_config = repo_root / "opencode.json"
    if repo_config.is_file():
        try:
            data = json.loads(repo_config.read_text())
            keys = set(data.get("mcp", {}).keys())
            if keys:
                configured |= keys
                sources.append(f"repo ({len(keys)})")
        except json.JSONDecodeError:
            return CheckResult("mcp", False, "opencode.json is invalid JSON")

    if not configured:
        return CheckResult("mcp", False, "no MCP servers found in global or repo opencode.json")

    missing = _REQUIRED_MCPS - configured
    if missing:
        return CheckResult(
            "mcp",
            False,
            f"missing MCP servers: {', '.join(sorted(missing))}"
            f" (checked: {', '.join(sources) or 'none'})",
        )

    return CheckResult(
        "mcp",
        True,
        f"all required MCPs configured ({', '.join(sources) or 'repo only'})",
    )


async def run_doctor(
    repo_root: Path,
    *,
    tool_runner: ToolRunner | None = None,
) -> DoctorResult:
    """Run all preflight checks and return results."""
    result = DoctorResult()

    from orch.config import Config
    from orch.state import resolve_state_dir, repo_id_from_remote
    cfg = Config.load(repo_root=repo_root)
    state_dir = resolve_state_dir(repo_root, base_dir=cfg.state.base_dir)
    repo_id = repo_id_from_remote(repo_root)

    result.checks.append(CheckResult("state_dir", True, f"{state_dir}  (repo_id: {repo_id})"))

    # Config check
    config_path = state_dir / "config.toml"
    if config_path.is_file():
        result.checks.append(CheckResult("config", True, "config.toml found"))
    else:
        result.checks.append(CheckResult("config", False, f"config.toml not found in {state_dir}"))

    # Database check — verify schema, not just file existence
    db_path = state_dir / "state.db"
    if not db_path.is_file():
        result.checks.append(CheckResult("database", False, ".orchestra/state.db not found"))
    else:
        result.checks.append(await _check_db_schema(db_path))

    # GitNexus index check
    gitnexus_dir = repo_root / ".gitnexus"
    if gitnexus_dir.is_dir():
        result.checks.append(CheckResult("gitnexus", True, ".gitnexus/ index found"))
    else:
        result.checks.append(CheckResult("gitnexus", False, ".gitnexus/ not found"))

    # opencode MCP configuration check
    result.checks.append(_check_opencode_mcps(repo_root))

    # CLI tool checks
    if tool_runner is not None:
        for tool in ("rtk", "opencode", "gh"):
            if await tool_runner(tool, ["--version"]):
                result.checks.append(CheckResult(tool, True, f"{tool} available"))
            else:
                result.checks.append(CheckResult(tool, False, f"{tool} not found"))

    return result
