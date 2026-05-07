"""Tests for the append-only event log."""

from pathlib import Path

from orch.db import Database
from orch.tickets import create_ticket, list_events, move_ticket, update_ticket


async def test_move_ticket_writes_event(tmp_db_path: Path) -> None:
    """Moving a ticket produces an event row with correct old/new state and actor."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {"title": "Test", "description": "Desc", "acceptance_criteria": "AC"},
        )
        await move_ticket(db, ticket.id, "To Do", actor="human")

        events = await list_events(db)

    assert len(events) == 1
    evt = events[0]
    assert evt.ticket_id == ticket.id
    assert evt.old_state == "Draft"
    assert evt.new_state == "To Do"
    assert evt.actor == "human"
    assert evt.timestamp is not None


async def test_multiple_transitions_produce_event_chain(tmp_db_path: Path) -> None:
    """Each state transition appends a new event; chain is chronologically ordered."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {"title": "Chain", "description": "D", "acceptance_criteria": "AC"},
        )
        await move_ticket(db, ticket.id, "To Do", actor="human")
        await move_ticket(db, ticket.id, "In Progress", actor="router")
        await move_ticket(db, ticket.id, "Code Review", actor="coder")

        events = await list_events(db)

    assert len(events) == 3
    assert [(e.old_state, e.new_state, e.actor) for e in events] == [
        ("Draft", "To Do", "human"),
        ("To Do", "In Progress", "router"),
        ("In Progress", "Code Review", "coder"),
    ]


async def test_list_events_with_limit(tmp_db_path: Path) -> None:
    """list_events with limit returns the last N events."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {"title": "Limit", "description": "D", "acceptance_criteria": "AC"},
        )
        await move_ticket(db, ticket.id, "To Do", actor="human")
        await move_ticket(db, ticket.id, "In Progress", actor="router")
        await move_ticket(db, ticket.id, "Code Review", actor="coder")

        events = await list_events(db, limit=2)

    assert len(events) == 2
    assert events[0].old_state == "To Do"
    assert events[1].old_state == "In Progress"


async def test_update_ticket_state_change_writes_event(tmp_db_path: Path) -> None:
    """update_ticket with a state change also writes an event row."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {"title": "Update", "description": "D", "acceptance_criteria": "AC"},
        )
        await update_ticket(db, ticket.id, state="To Do", actor="router")

        events = await list_events(db)

    assert len(events) == 1
    assert events[0].old_state == "Draft"
    assert events[0].new_state == "To Do"
    assert events[0].actor == "router"


async def test_update_ticket_without_state_change_no_event(tmp_db_path: Path) -> None:
    """update_ticket that doesn't change state writes no event."""
    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {"title": "NoEvent", "description": "D", "acceptance_criteria": "AC"},
        )
        await update_ticket(db, ticket.id, assignee="coder")

        events = await list_events(db)

    assert len(events) == 0


def test_orch_log_displays_events(tmp_path: Path) -> None:
    """orch log shows events in chronological order."""
    import json

    from click.testing import CliRunner

    from orch.cli import main

    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Create a ticket and move it through states
    import yaml

    ticket_file = tmp_path / "t.yaml"
    ticket_file.write_text(
        yaml.dump(
            {"title": "Log test", "description": "D", "acceptance_criteria": "AC"},
        )
    )
    result = runner.invoke(
        main, ["--db", str(db_path), "tickets", "create", "--from-file", str(ticket_file)]
    )
    assert result.exit_code == 0, result.output
    ticket_id = result.output.strip().split()[-1]

    runner.invoke(main, ["--db", str(db_path), "tickets", "move", ticket_id, "To Do"])
    runner.invoke(main, ["--db", str(db_path), "tickets", "move", ticket_id, "In Progress"])

    # orch log
    result = runner.invoke(main, ["--db", str(db_path), "log"])
    assert result.exit_code == 0, result.output
    assert "Draft" in result.output
    assert "To Do" in result.output
    assert "In Progress" in result.output

    # orch log -n 1
    result = runner.invoke(main, ["--db", str(db_path), "log", "-n", "1"])
    assert result.exit_code == 0, result.output
    # Should only show the last event (To Do -> In Progress)
    lines = [ln for ln in result.output.strip().splitlines() if ticket_id in ln]
    assert len(lines) == 1
    assert "In Progress" in lines[0]

    # orch log --json
    result = runner.invoke(main, ["--db", str(db_path), "log", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2
    assert data[0]["old_state"] == "Draft"
