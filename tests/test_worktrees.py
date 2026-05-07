"""Tests for orch worktrees list and prune."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from orch.cli import main
from orch.db import Database
from orch.tickets import create_ticket, move_ticket
from orch.worktrees import PruneResult, list_worktrees, prune_worktrees


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "state.db"
    async with Database(db_path) as database:
        yield database


def _make_worktree_dir(repo_root: Path, ticket_id: str) -> Path:
    """Create a fake worktree directory."""
    wt = repo_root / ".orchestra" / "worktrees" / ticket_id
    wt.mkdir(parents=True)
    return wt


async def test_list_worktrees_returns_ticket_states(db: Database, tmp_path: Path) -> None:
    """list_worktrees scans .orchestra/worktrees/ and reports ticket states."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Create two tickets, move one to Done
    t1 = await create_ticket(
        db,
        {
            "title": "T1",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    t2 = await create_ticket(
        db,
        {
            "title": "T2",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    await move_ticket(db, t2.id, "Done")

    # Create worktree dirs for both
    _make_worktree_dir(repo_root, t1.id)
    _make_worktree_dir(repo_root, t2.id)

    infos = await list_worktrees(db, repo_root)
    assert len(infos) == 2

    by_id = {i.ticket_id: i for i in infos}
    assert by_id[t1.id].state == "Draft"
    assert by_id[t2.id].state == "Done"


async def test_prune_removes_done_skips_non_done(db: Database, tmp_path: Path) -> None:
    """Prune removes worktrees for Done tickets, skips non-Done."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    t1 = await create_ticket(
        db,
        {
            "title": "T1",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    t2 = await create_ticket(
        db,
        {
            "title": "T2",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    await move_ticket(db, t2.id, "Done")

    _make_worktree_dir(repo_root, t1.id)
    _make_worktree_dir(repo_root, t2.id)

    removed: list[Path] = []

    async def mock_git_check_dirty(_path: Path) -> bool:
        return False

    async def mock_git_check_unpushed(_path: Path) -> bool:
        return False

    async def mock_git_remove(path: Path) -> None:
        removed.append(path)
        # Simulate removal
        if path.exists():
            path.rmdir()

    result = await prune_worktrees(
        db,
        repo_root,
        check_dirty=mock_git_check_dirty,
        check_unpushed=mock_git_check_unpushed,
        remove_worktree=mock_git_remove,
    )

    assert isinstance(result, PruneResult)
    assert result.pruned == 1
    assert result.skipped == 1
    assert len(removed) == 1
    assert removed[0].name == t2.id


async def test_prune_skips_dirty_worktree(db: Database, tmp_path: Path) -> None:
    """Prune refuses to remove a Done worktree with uncommitted changes."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    t1 = await create_ticket(
        db,
        {
            "title": "T1",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    await move_ticket(db, t1.id, "Done")
    _make_worktree_dir(repo_root, t1.id)

    removed: list[Path] = []

    async def mock_dirty(_path: Path) -> bool:
        return True  # dirty

    async def mock_unpushed(_path: Path) -> bool:
        return False

    async def mock_remove(path: Path) -> None:
        removed.append(path)

    result = await prune_worktrees(
        db,
        repo_root,
        check_dirty=mock_dirty,
        check_unpushed=mock_unpushed,
        remove_worktree=mock_remove,
    )
    assert result.pruned == 0
    assert result.skipped == 1
    assert len(removed) == 0


async def test_prune_skips_unpushed_worktree(db: Database, tmp_path: Path) -> None:
    """Prune refuses to remove a Done worktree with unpushed commits."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    t1 = await create_ticket(
        db,
        {
            "title": "T1",
            "description": "D",
            "acceptance_criteria": "AC",
        },
    )
    await move_ticket(db, t1.id, "Done")
    _make_worktree_dir(repo_root, t1.id)

    removed: list[Path] = []

    async def mock_dirty(_path: Path) -> bool:
        return False

    async def mock_unpushed(_path: Path) -> bool:
        return True  # unpushed

    async def mock_remove(path: Path) -> None:
        removed.append(path)

    result = await prune_worktrees(
        db,
        repo_root,
        check_dirty=mock_dirty,
        check_unpushed=mock_unpushed,
        remove_worktree=mock_remove,
    )
    assert result.pruned == 0
    assert result.skipped == 1
    assert len(removed) == 0


# ── CLI integration ─────────────────────────────────────────────────


def _create_and_invoke(
    runner: CliRunner,
    db_path: Path,
    ticket_file: Path,
    tmp_path: Path,
) -> None:
    ticket_file.write_text(
        yaml.dump(
            {
                "title": "T",
                "description": "D",
                "acceptance_criteria": "AC",
            }
        )
    )
    runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)],
    )


def test_cli_worktrees_prune_with_yes(tmp_path: Path) -> None:
    """orch worktrees prune --yes prunes Done worktrees without prompting."""
    db_path = tmp_path / "state.db"
    repo_root = tmp_path / "repo"
    (repo_root / ".orchestra" / "worktrees" / "ORCH-001").mkdir(parents=True)

    runner = CliRunner()
    ticket_file = tmp_path / "t.yaml"
    _create_and_invoke(runner, db_path, ticket_file, tmp_path)
    runner.invoke(
        main,
        ["--db", str(db_path), "tickets", "move", "ORCH-001", "Done"],
    )

    dirty = "orch.worktrees._default_check_dirty"
    unpushed = "orch.worktrees._default_check_unpushed"
    remove = "orch.worktrees._default_remove_worktree"
    with (
        patch(dirty, new_callable=AsyncMock, return_value=False),
        patch(unpushed, new_callable=AsyncMock, return_value=False),
        patch(remove, new_callable=AsyncMock),
    ):
        result = runner.invoke(
            main,
            ["--db", str(db_path), "worktrees", "prune", "--yes", "--dir", str(repo_root)],
        )
    assert result.exit_code == 0, result.output
    assert "Pruned 1" in result.output


def test_cli_worktrees_prune_reports_skipped(tmp_path: Path) -> None:
    """orch worktrees prune reports skipped count for non-Done tickets."""
    db_path = tmp_path / "state.db"
    repo_root = tmp_path / "repo"
    (repo_root / ".orchestra" / "worktrees" / "ORCH-001").mkdir(parents=True)

    runner = CliRunner()
    ticket_file = tmp_path / "t.yaml"
    _create_and_invoke(runner, db_path, ticket_file, tmp_path)
    # Leave in Draft — should be skipped

    dirty = "orch.worktrees._default_check_dirty"
    unpushed = "orch.worktrees._default_check_unpushed"
    remove = "orch.worktrees._default_remove_worktree"
    with (
        patch(dirty, new_callable=AsyncMock, return_value=False),
        patch(unpushed, new_callable=AsyncMock, return_value=False),
        patch(remove, new_callable=AsyncMock),
    ):
        result = runner.invoke(
            main,
            ["--db", str(db_path), "worktrees", "prune", "--yes", "--dir", str(repo_root)],
        )
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output.lower()
    assert "1 skipped" in result.output
