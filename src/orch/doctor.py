"""Preflight checks for orch environment."""

from __future__ import annotations

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


async def run_doctor(
    repo_root: Path,
    *,
    tool_runner: ToolRunner | None = None,
) -> DoctorResult:
    """Run all preflight checks and return results."""
    result = DoctorResult()

    # Config check
    config_path = repo_root / ".orchestra" / "config.toml"
    if config_path.is_file():
        result.checks.append(CheckResult("config", True, "config.toml found"))
    else:
        result.checks.append(CheckResult("config", False, ".orchestra/config.toml not found"))

    # Database check — verify schema, not just file existence
    db_path = repo_root / ".orchestra" / "state.db"
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

    # CLI tool checks
    if tool_runner is not None:
        for tool in ("rtk", "opencode", "gh"):
            if await tool_runner(tool, ["--version"]):
                result.checks.append(CheckResult(tool, True, f"{tool} available"))
            else:
                result.checks.append(CheckResult(tool, False, f"{tool} not found"))

    return result
