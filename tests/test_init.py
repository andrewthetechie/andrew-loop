"""Tests for orch init command."""

import pytest

from orch.init import init_project


@pytest.mark.asyncio
async def test_creates_orchestra_directory_structure(tmp_path: pytest.TempPathFactory) -> None:
    """orch init creates .orchestra/ with config.toml, state.db, logs/, worktrees/."""
    result = await init_project(tmp_path)

    assert (tmp_path / ".orchestra").is_dir()
    assert (tmp_path / ".orchestra" / "config.toml").is_file()
    assert (tmp_path / ".orchestra" / "state.db").is_file()
    assert (tmp_path / ".orchestra" / "logs").is_dir()
    assert (tmp_path / ".orchestra" / "worktrees").is_dir()
    assert result.config_status == "created"
    assert result.db_status == "created"


@pytest.mark.asyncio
async def test_idempotent_skips_completed_steps(tmp_path: pytest.TempPathFactory) -> None:
    """Running init twice reports 'exists' for already-completed steps."""
    await init_project(tmp_path)
    result = await init_project(tmp_path)

    assert result.config_status == "exists"
    assert result.db_status == "exists"
    # Directories still intact
    assert (tmp_path / ".orchestra" / "logs").is_dir()
    assert (tmp_path / ".orchestra" / "worktrees").is_dir()


@pytest.mark.asyncio
async def test_database_has_full_schema(tmp_path: pytest.TempPathFactory) -> None:
    """init creates a database with all required tables."""
    from orch.db import Database

    await init_project(tmp_path)

    async with Database(tmp_path / ".orchestra" / "state.db") as db:
        tables = await db.list_tables()

    assert "tickets" in tables
    assert "ticket_comments" in tables
    assert "events" in tables
    assert "ticket_dependencies" in tables
    assert "subtask_context" in tables


@pytest.mark.asyncio
async def test_config_has_valid_toml_defaults(tmp_path: pytest.TempPathFactory) -> None:
    """Generated config.toml is valid TOML and loadable by Config."""
    import tomllib

    from orch.config import Config

    await init_project(tmp_path)

    config_path = tmp_path / ".orchestra" / "config.toml"
    raw = config_path.read_text()
    parsed = tomllib.loads(raw)
    # Should at minimum have a router section with poll_interval
    assert "router" in parsed
    # Config.load should work with the generated file
    cfg = Config.load(repo_root=tmp_path)
    assert cfg.router.poll_interval == 10.0


@pytest.mark.asyncio
async def test_external_tools_called_with_correct_args(tmp_path: pytest.TempPathFactory) -> None:
    """init invokes external tools (hindsight, serena, gitnexus) with repo root."""
    calls: list[tuple[str, list[str]]] = []

    async def fake_runner(name: str, args: list[str]) -> bool:
        calls.append((name, args))
        return True

    result = await init_project(tmp_path, run_external=fake_runner)

    names = [c[0] for c in calls]
    assert "hindsight" in names
    assert "serena" in names
    assert "gitnexus" in names
    assert result.hindsight_status == "created"
    assert result.serena_status == "created"
    assert result.gitnexus_status == "created"


@pytest.mark.asyncio
async def test_external_tool_failure_reports_failed(tmp_path: pytest.TempPathFactory) -> None:
    """If an external tool returns False, status is 'failed'."""

    async def failing_runner(name: str, args: list[str]) -> bool:
        return name != "serena"  # serena fails, others succeed

    result = await init_project(tmp_path, run_external=failing_runner)

    assert result.hindsight_status == "created"
    assert result.serena_status == "failed"
    assert result.gitnexus_status == "created"


@pytest.mark.asyncio
async def test_idempotent_does_not_corrupt_existing_config(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Running init on a repo with custom config preserves the custom config."""
    import tomllib

    await init_project(tmp_path)

    # Simulate user editing the config
    config_path = tmp_path / ".orchestra" / "config.toml"
    custom = config_path.read_text() + '\n[custom]\nkey = "value"\n'
    config_path.write_text(custom)

    # Re-run init
    result = await init_project(tmp_path)

    assert result.config_status == "exists"
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["custom"]["key"] == "value"
    assert parsed["router"]["poll_interval"] == 10.0


def test_cli_init_creates_project(tmp_path: pytest.TempPathFactory) -> None:
    """orch init CLI command creates .orchestra/ and prints step statuses."""
    from click.testing import CliRunner

    from orch.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-externals"])

    assert result.exit_code == 0, result.output
    assert "config" in result.output.lower()
    assert "created" in result.output.lower()
    assert (tmp_path / ".orchestra" / "config.toml").is_file()
    assert (tmp_path / ".orchestra" / "state.db").is_file()


def test_cli_init_idempotent(tmp_path: pytest.TempPathFactory) -> None:
    """Running orch init twice shows 'exists' on second run."""
    from click.testing import CliRunner

    from orch.cli import main

    runner = CliRunner()
    runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-externals"])
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-externals"])

    assert result.exit_code == 0, result.output
    assert "exists" in result.output.lower()
