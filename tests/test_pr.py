"""Tests for PR create and update commands."""

from pathlib import Path

import pytest

from orch.db import Database
from orch.pr import create_pr, update_pr
from orch.tickets import create_ticket, get_ticket, update_ticket


async def test_create_pr_constructs_correct_gh_command(tmp_db_path: Path) -> None:
    """create_pr calls gh with correct title, body, and base branch; links PR to ticket."""
    captured_cmds: list[list[str]] = []

    async def fake_run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        captured_cmds.append(cmd)
        return (0, "https://github.com/org/repo/pull/42\n", "")

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Add slugify",
                "description": "Add a slugify function.",
                "acceptance_criteria": "- [ ] Function exists\n- [ ] Tests pass",
            },
        )

        url = await create_pr(db, ticket.id, base_branch="main", run_cmd=fake_run)

        # Verify the PR URL was returned and linked
        assert url == "https://github.com/org/repo/pull/42"
        updated = await get_ticket(db, ticket.id)
        assert updated.linked_pr == "https://github.com/org/repo/pull/42"

    # Verify gh command construction
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[0] == "gh"
    assert "pr" in cmd
    assert "create" in cmd

    # Title format: ticket/<id>: <title>
    title_idx = cmd.index("--title") + 1
    assert cmd[title_idx] == f"ticket/{ticket.id}: Add slugify"

    # Base branch
    base_idx = cmd.index("--base") + 1
    assert cmd[base_idx] == "main"

    # Body contains ticket info
    body_idx = cmd.index("--body") + 1
    body = cmd[body_idx]
    assert "Add a slugify function." in body
    assert "Function exists" in body


async def test_create_pr_fails_on_nonexistent_ticket(tmp_db_path: Path) -> None:
    """create_pr raises ValueError for a ticket that doesn't exist."""
    async with Database(tmp_db_path) as db:
        with pytest.raises(ValueError, match="not found"):
            await create_pr(db, "ORCH-999", base_branch="main")


async def test_create_pr_fails_on_gh_error(tmp_db_path: Path) -> None:
    """create_pr raises RuntimeError when gh command fails."""

    async def failing_gh(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        return (1, "", "not authenticated")

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        with pytest.raises(RuntimeError, match="not authenticated"):
            await create_pr(db, ticket.id, base_branch="main", run_cmd=failing_gh)


async def test_update_pr_pushes_branch(tmp_db_path: Path) -> None:
    """update_pr runs git push for a ticket with a linked PR."""
    captured: list[list[str]] = []

    async def fake_run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        await update_ticket(db, ticket.id, linked_pr="https://github.com/org/repo/pull/42")

        await update_pr(db, ticket.id, run_cmd=fake_run)

    assert captured == [["git", "push"]]


async def test_update_pr_fails_without_linked_pr(tmp_db_path: Path) -> None:
    """update_pr raises ValueError when ticket has no linked PR."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db, {"title": "T", "description": "D", "acceptance_criteria": "AC"}
        )
        with pytest.raises(ValueError, match="no linked PR"):
            await update_pr(db, ticket.id)


def test_cli_pr_create(tmp_path: Path) -> None:
    """orch pr create creates a PR and links it to the ticket."""
    import json
    from unittest.mock import AsyncMock, patch

    import yaml
    from click.testing import CliRunner

    from orch.cli import main

    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Create a ticket
    tf = tmp_path / "t.yaml"
    tf.write_text(yaml.dump({"title": "CLI PR", "description": "D", "acceptance_criteria": "AC"}))
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "create", "--from-file", str(tf)])
    assert r.exit_code == 0, r.output
    ticket_id = r.output.strip().split()[-1]

    # Mock the subprocess runner in pr module
    mock_run = AsyncMock(return_value=(0, "https://github.com/org/repo/pull/99\n", ""))
    with patch("orch.pr._default_run_cmd", mock_run):
        r = runner.invoke(
            main, ["--db", str(db_path), "pr", "create", ticket_id, "--base", "main"]
        )

    assert r.exit_code == 0, r.output
    assert "https://github.com/org/repo/pull/99" in r.output

    # Verify ticket has linked PR
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "show", ticket_id, "--json"])
    data = json.loads(r.output)
    assert data["linked_pr"] == "https://github.com/org/repo/pull/99"


def test_cli_pr_update(tmp_path: Path) -> None:
    """orch pr update pushes the branch for a ticket with a linked PR."""
    from unittest.mock import AsyncMock, patch

    import yaml
    from click.testing import CliRunner

    from orch.cli import main

    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Create and link a ticket
    tf = tmp_path / "t.yaml"
    tf.write_text(
        yaml.dump({"title": "CLI update", "description": "D", "acceptance_criteria": "AC"})
    )
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "create", "--from-file", str(tf)])
    ticket_id = r.output.strip().split()[-1]
    runner.invoke(
        main,
        [
            "--db",
            str(db_path),
            "tickets",
            "update",
            ticket_id,
            "--linked-pr",
            "https://github.com/org/repo/pull/99",
        ],
    )

    mock_run = AsyncMock(return_value=(0, "", ""))
    with patch("orch.pr._default_run_cmd", mock_run):
        r = runner.invoke(main, ["--db", str(db_path), "pr", "update", ticket_id])

    assert r.exit_code == 0, r.output
    mock_run.assert_called_once()
