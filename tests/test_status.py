"""Tests for orch status command."""

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from orch.cli import main


def _create_ticket(runner: CliRunner, db_path: Path, tmp_path: Path, **overrides: object) -> str:
    """Helper: create a ticket and return its ID."""
    data = {
        "title": "Test ticket",
        "description": "D",
        "acceptance_criteria": "AC",
        **overrides,
    }
    tf = tmp_path / "t.yaml"
    tf.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "create", "--from-file", str(tf)]
    )
    assert result.exit_code == 0, result.output
    return result.output.strip().split()[-1]


def test_status_empty(tmp_path: Path) -> None:
    """orch status with no tickets shows a helpful message."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    result = runner.invoke(main, ["--db", str(db_path), "status"])
    assert result.exit_code == 0, result.output
    assert "no tickets" in result.output.lower()


def test_status_shows_tickets_sorted_by_state(tmp_path: Path) -> None:
    """orch status displays tickets sorted by state with expected columns."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    t1 = _create_ticket(runner, db_path, tmp_path, title="First task", risk_score=3)
    _create_ticket(runner, db_path, tmp_path, title="Second task")

    # Move t1 to To Do so it appears in a different state group
    runner.invoke(main, ["--db", str(db_path), "tickets", "move", t1, "To Do"])

    result = runner.invoke(main, ["--db", str(db_path), "status"])
    assert result.exit_code == 0, result.output
    # Both tickets appear
    assert "First task" in result.output
    assert "Second task" in result.output
    # State column values
    assert "Draft" in result.output
    assert "To Do" in result.output


def test_status_json_output(tmp_path: Path) -> None:
    """orch status --json returns structured JSON array of tickets."""
    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    _create_ticket(runner, db_path, tmp_path, title="JSON ticket")
    _create_ticket(runner, db_path, tmp_path, title="Another")

    result = runner.invoke(main, ["--db", str(db_path), "status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2
    assert data[0]["title"] == "JSON ticket"
    assert "state" in data[0]
    assert "id" in data[0]
