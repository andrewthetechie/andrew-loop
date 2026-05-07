"""Tests for orch doctor command."""

import pytest

from orch.doctor import run_doctor


@pytest.mark.asyncio
async def test_all_checks_pass(tmp_path: pytest.TempPathFactory) -> None:
    """When all checks pass, result has all passing and is healthy."""
    from orch.init import init_project

    await init_project(tmp_path)
    # Create .gitnexus dir to satisfy that check
    (tmp_path / ".gitnexus").mkdir()

    async def all_pass(name: str, args: list[str]) -> bool:
        return True

    result = await run_doctor(tmp_path, tool_runner=all_pass)

    assert result.healthy
    assert all(c.passed for c in result.checks)
    assert len(result.checks) > 0


@pytest.mark.asyncio
async def test_missing_config_fails(tmp_path: pytest.TempPathFactory) -> None:
    """When .orchestra/config.toml is missing, doctor reports failure."""
    result = await run_doctor(tmp_path)

    assert not result.healthy
    config_check = next(c for c in result.checks if c.name == "config")
    assert not config_check.passed
    assert "config.toml" in config_check.message


@pytest.mark.asyncio
async def test_tool_check_failure(tmp_path: pytest.TempPathFactory) -> None:
    """When a CLI tool is missing, doctor reports it as failed."""
    from orch.init import init_project

    await init_project(tmp_path)
    (tmp_path / ".gitnexus").mkdir()

    async def gh_fails(name: str, args: list[str]) -> bool:
        return name != "gh"

    result = await run_doctor(tmp_path, tool_runner=gh_fails)

    gh_check = next(c for c in result.checks if c.name == "gh")
    assert not gh_check.passed
    assert not result.healthy
    # Other tools should pass
    rtk_check = next(c for c in result.checks if c.name == "rtk")
    assert rtk_check.passed


@pytest.mark.asyncio
async def test_database_schema_validated(tmp_path: pytest.TempPathFactory) -> None:
    """Doctor verifies the database has the correct schema, not just existence."""
    # Create a state.db that's just an empty file (no schema)
    orchestra_dir = tmp_path / ".orchestra"
    orchestra_dir.mkdir()
    (orchestra_dir / "config.toml").write_text("[router]\npoll_interval = 10.0\n")
    (orchestra_dir / "state.db").write_text("")  # empty, invalid sqlite
    (tmp_path / ".gitnexus").mkdir()

    result = await run_doctor(tmp_path)

    db_check = next(c for c in result.checks if c.name == "database")
    assert not db_check.passed
    assert "schema" in db_check.message.lower() or "table" in db_check.message.lower()


def test_cli_doctor_healthy(tmp_path: pytest.TempPathFactory) -> None:
    """orch doctor CLI exits 0 when healthy, prints pass for each check."""
    from click.testing import CliRunner

    from orch.cli import main

    runner = CliRunner()
    # First init the project
    runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-externals"])
    (tmp_path / ".gitnexus").mkdir()

    result = runner.invoke(main, ["doctor", "--dir", str(tmp_path), "--no-tools"])

    assert result.exit_code == 0, result.output
    assert "pass" in result.output.lower() or "✓" in result.output


def test_cli_doctor_unhealthy(tmp_path: pytest.TempPathFactory) -> None:
    """orch doctor CLI exits 1 when checks fail."""
    from click.testing import CliRunner

    from orch.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--dir", str(tmp_path), "--no-tools"])

    assert result.exit_code == 1
