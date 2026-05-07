"""Tests for orch tickets import and orch tickets promote."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from orch.cli import main
from orch.db import Database
from orch.tickets import get_dependencies, import_tickets, list_tickets, promote_tickets


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "state.db"
    async with Database(db_path) as database:
        yield database


async def test_import_creates_tickets_in_draft(db: Database) -> None:
    """Import multiple tickets from a list of dicts, all created in Draft."""
    tickets_data = [
        {
            "title": "First ticket",
            "description": "Do the first thing",
            "acceptance_criteria": "- [ ] Done",
        },
        {
            "title": "Second ticket",
            "description": "Do the second thing",
            "acceptance_criteria": "- [ ] Also done",
        },
    ]
    created = await import_tickets(db, tickets_data)
    assert len(created) == 2
    assert all(t.state == "Draft" for t in created)
    assert created[0].title == "First ticket"
    assert created[1].title == "Second ticket"
    # IDs should be sequential
    assert created[0].id == "ORCH-001"
    assert created[1].id == "ORCH-002"


async def test_import_with_dependencies_by_title(db: Database) -> None:
    """Import tickets that reference each other by title for dependencies."""
    tickets_data = [
        {
            "title": "Setup database",
            "description": "Create the schema",
            "acceptance_criteria": "- [ ] Schema exists",
        },
        {
            "title": "Build API",
            "description": "REST endpoints",
            "acceptance_criteria": "- [ ] Endpoints work",
            "depends_on": ["Setup database"],
        },
        {
            "title": "Build UI",
            "description": "Frontend",
            "acceptance_criteria": "- [ ] UI renders",
            "depends_on": ["Build API"],
        },
    ]
    created = await import_tickets(db, tickets_data)
    assert len(created) == 3

    # ORCH-002 depends on ORCH-001
    deps_002 = await get_dependencies(db, created[1].id)
    assert deps_002 == [created[0].id]

    # ORCH-003 depends on ORCH-002
    deps_003 = await get_dependencies(db, created[2].id)
    assert deps_003 == [created[1].id]


async def test_import_fails_fast_on_invalid_ticket(db: Database) -> None:
    """If any ticket is invalid, none are created."""
    tickets_data = [
        {
            "title": "Good ticket",
            "description": "Fine",
            "acceptance_criteria": "- [ ] OK",
        },
        {
            "title": "",  # invalid: empty title
            "description": "Bad",
            "acceptance_criteria": "- [ ] Nope",
        },
    ]
    with pytest.raises(ValueError, match="title"):
        await import_tickets(db, tickets_data)

    # Nothing should have been created
    all_tickets = await list_tickets(db)
    assert len(all_tickets) == 0


async def test_promote_moves_all_drafts_to_todo(db: Database) -> None:
    """Promote without ticket_id moves all Draft tickets to To Do."""
    tickets_data = [
        {
            "title": "Ticket A",
            "description": "Desc A",
            "acceptance_criteria": "- [ ] AC A",
            "risk_score": 2,
        },
        {
            "title": "Ticket B",
            "description": "Desc B",
            "acceptance_criteria": "- [ ] AC B",
            "risk_score": 3,
        },
    ]
    await import_tickets(db, tickets_data)
    promoted = await promote_tickets(db)
    assert len(promoted) == 2
    assert all(t.state == "To Do" for t in promoted)


async def test_promote_specific_ticket(db: Database) -> None:
    """Promote with ticket_id moves only that ticket."""
    tickets_data = [
        {
            "title": "Ticket A",
            "description": "Desc",
            "acceptance_criteria": "- [ ] AC",
            "risk_score": 1,
        },
        {
            "title": "Ticket B",
            "description": "Desc",
            "acceptance_criteria": "- [ ] AC",
            "risk_score": 2,
        },
    ]
    created = await import_tickets(db, tickets_data)
    promoted = await promote_tickets(db, ticket_id=created[0].id)
    assert len(promoted) == 1
    assert promoted[0].id == created[0].id
    assert promoted[0].state == "To Do"

    # Other ticket still Draft
    all_tickets = await list_tickets(db, state_filter="Draft")
    assert len(all_tickets) == 1
    assert all_tickets[0].id == created[1].id


async def test_promote_rejects_missing_risk_score(db: Database) -> None:
    """Promote refuses tickets missing risk_score."""
    tickets_data = [
        {
            "title": "No risk",
            "description": "Missing risk_score",
            "acceptance_criteria": "- [ ] AC",
            # No risk_score
        },
    ]
    await import_tickets(db, tickets_data)
    with pytest.raises(ValueError, match="risk_score"):
        await promote_tickets(db)


# ── CLI integration ─────────────────────────────────────────────────


def test_cli_import_creates_tickets(tmp_path: Path) -> None:
    """orch tickets import <file> creates tickets from YAML."""
    db_path = tmp_path / "state.db"
    import_file = tmp_path / "tickets.yaml"
    import_file.write_text(
        "- title: CLI ticket A\n"
        "  description: Desc A\n"
        "  acceptance_criteria: '- [ ] AC'\n"
        "- title: CLI ticket B\n"
        "  description: Desc B\n"
        "  acceptance_criteria: '- [ ] AC'\n"
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "import", str(import_file)])
    assert result.exit_code == 0, result.output
    assert "Created 2 tickets" in result.output
    assert "ORCH-001" in result.output
    assert "ORCH-002" in result.output


def test_cli_promote_moves_drafts(tmp_path: Path) -> None:
    """orch tickets promote moves Draft tickets to To Do."""
    db_path = tmp_path / "state.db"
    import_file = tmp_path / "tickets.yaml"
    import_file.write_text(
        "- title: Promotable\n"
        "  description: Desc\n"
        "  acceptance_criteria: '- [ ] AC'\n"
        "  risk_score: 3\n"
    )
    runner = CliRunner()
    # Import first
    runner.invoke(main, ["--db", str(db_path), "tickets", "import", str(import_file)])
    # Then promote
    result = runner.invoke(main, ["--db", str(db_path), "tickets", "promote"])
    assert result.exit_code == 0, result.output
    assert "Promoted 1 ticket" in result.output


def test_cli_promote_specific_ticket(tmp_path: Path) -> None:
    """orch tickets promote --ticket-id promotes only that ticket."""
    db_path = tmp_path / "state.db"
    import_file = tmp_path / "tickets.yaml"
    import_file.write_text(
        "- title: A\n"
        "  description: D\n"
        "  acceptance_criteria: AC\n"
        "  risk_score: 1\n"
        "- title: B\n"
        "  description: D\n"
        "  acceptance_criteria: AC\n"
        "  risk_score: 2\n"
    )
    runner = CliRunner()
    runner.invoke(main, ["--db", str(db_path), "tickets", "import", str(import_file)])
    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "promote", "--ticket-id", "ORCH-001"]
    )
    assert result.exit_code == 0, result.output
    assert "ORCH-001" in result.output
