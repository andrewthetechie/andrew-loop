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


async def _check_hindsight(repo_root: Path, cfg: object) -> CheckResult:
    """Verify configured Hindsight bank is reachable and useful for agent memory."""
    hindsight_cfg = getattr(cfg, "hindsight", None)
    base_bank_id = str(getattr(hindsight_cfg, "bank_id", "") or "")
    url = str(getattr(hindsight_cfg, "url", "") or "")
    api_key = str(getattr(hindsight_cfg, "api_key", "") or "")
    if not base_bank_id:
        return CheckResult("hindsight", True, "optional: not configured")

    bank_id = f"{base_bank_id}-{repo_root.name}"
    client = None
    try:
        from hindsight_client import Hindsight

        client = Hindsight(base_url=url, api_key=api_key or None, timeout=15.0)
        await client.banks.get_bank_profile(bank_id)
        config_response = await _aget_bank_config(client, bank_id)
        bank_config = _extract_bank_config(config_response)
        missing = _missing_hindsight_memory_config(bank_config)
        stats = await _aget_bank_stats(client, bank_id)
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        return CheckResult("hindsight", False, f"{bank_id} unreachable or invalid: {detail}")
    finally:
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    if missing:
        return CheckResult(
            "hindsight",
            False,
            f"{bank_id} missing coding-agent config: {', '.join(missing)}",
        )

    return CheckResult(
        "hindsight",
        True,
        (
            f"{bank_id} reachable; memory units: {_stat_value(stats, 'total_nodes')}; "
            f"documents: {_stat_value(stats, 'total_documents')}; "
            f"observations: {_observation_count(stats)}"
        ),
    )


async def _aget_bank_config(client: object, bank_id: str) -> object:
    """Read resolved Hindsight bank config across SDK versions."""
    method = getattr(client, "aget_bank_config", None)
    if method is not None:
        return await method(bank_id)

    method = getattr(client, "_aget_bank_config", None)
    if method is not None:
        return await method(bank_id)

    method = getattr(client, "get_bank_config", None)
    if method is not None:
        result = method(bank_id)
        if hasattr(result, "__await__"):
            return await result
        return result

    banks = getattr(client, "banks", None)
    method = getattr(banks, "get_bank_config", None)
    if method is not None:
        return await method(bank_id)

    return {}


async def _aget_bank_stats(client: object, bank_id: str) -> object:
    """Read Hindsight bank stats across SDK versions."""
    banks = getattr(client, "banks", None)
    for owner in (banks, client):
        if owner is None:
            continue
        for name in ("get_agent_stats", "get_bank_stats"):
            method = getattr(owner, name, None)
            if method is None:
                continue
            result = method(bank_id)
            if hasattr(result, "__await__"):
                return await result
            return result
    return {}


def _extract_bank_config(response: object) -> dict[str, object]:
    """Return resolved config from a Hindsight config response."""
    if isinstance(response, dict):
        value = response.get("config", response)
        return dict(value) if isinstance(value, dict) else {}

    value = getattr(response, "config", response)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _missing_hindsight_memory_config(config: dict[str, object]) -> list[str]:
    missing: list[str] = []
    if not str(config.get("retain_mission") or "").strip():
        missing.append("retain_mission")
    if not config.get("enable_observations"):
        missing.append("enable_observations")
    if not str(config.get("observations_mission") or "").strip():
        missing.append("observations_mission")
    return missing


def _stat_value(stats: object, name: str) -> int:
    if isinstance(stats, dict):
        return int(stats.get(name, 0) or 0)
    return int(getattr(stats, name, 0) or 0)


def _observation_count(stats: object) -> int:
    explicit = _stat_value(stats, "total_observations")
    if explicit:
        return explicit
    if isinstance(stats, dict):
        nodes_by_type = stats.get("nodes_by_fact_type", {})
    else:
        nodes_by_type = getattr(stats, "nodes_by_fact_type", {})
    if isinstance(nodes_by_type, dict):
        return int(nodes_by_type.get("observation", 0) or 0)
    return 0


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
    from orch.state import repo_id_from_remote, resolve_state_dir

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
        result.checks.append(CheckResult("database", False, f"state.db not found in {state_dir}"))
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

    # Hindsight bank quality check
    result.checks.append(await _check_hindsight(repo_root, cfg))

    # CLI tool checks
    if tool_runner is not None:
        for tool in ("rtk", "opencode", "gh"):
            if await tool_runner(tool, ["--version"]):
                result.checks.append(CheckResult(tool, True, f"{tool} available"))
            else:
                result.checks.append(CheckResult(tool, False, f"{tool} not found"))

    return result
