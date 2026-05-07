"""End-to-end smoke tests for the spike tracer bullet (issue 06).

Validates the full flow: create ticket → move to To Do → router dispatches →
agent runs → ticket ends in expected state.
"""

from pathlib import Path

from orch.db import Database
from orch.tickets import (
    create_ticket,
    get_ticket,
    get_ticket_comments,
    move_ticket,
    update_ticket,
)


async def _noop_create_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
    """Fake worktree creation."""


async def test_happy_path_ticket_to_code_review(tmp_db_path: Path, tmp_path: Path) -> None:
    """Full flow: create → To Do → router dispatches → agent moves to Code Review."""
    from orch.router import Router

    async def _successful_agent(
        ticket_id: str, worktree_dir: Path, agent_type: str = "coder"
    ) -> int:
        """Simulates what a successful coder agent does: updates ticket state and links PR."""
        async with Database(tmp_db_path) as agent_db:
            await update_ticket(
                agent_db,
                ticket_id,
                state="Code Review",
                linked_pr="https://github.com/org/repo/pull/42",
            )
        return 0

    async with Database(tmp_db_path) as db:
        # 1. Create a well-specified ticket (lands in Draft)
        ticket = await create_ticket(
            db,
            {
                "title": "Add greeting endpoint",
                "description": "Add GET /hello that returns JSON greeting",
                "acceptance_criteria": '- [ ] GET /hello returns {"message": "hello"}',
                "file_paths": "src/app.py",
                "test_expectations": "test_hello_endpoint passes",
            },
        )
        assert ticket.state == "Draft"

        # 2. Human promotes ticket to To Do
        ticket = await move_ticket(db, ticket.id, "To Do")
        assert ticket.state == "To Do"

        # 3. Router picks it up and dispatches
        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_successful_agent,
        )
        dispatched_id = await router.poll_once()
        assert dispatched_id == ticket.id

        # 4. Verify final state: Code Review with linked PR
        final = await get_ticket(db, ticket.id)
        assert final.state == "Code Review"
        assert final.linked_pr == "https://github.com/org/repo/pull/42"
        assert final.worktree_path is not None


async def test_agent_failure_lands_in_needs_human_review(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """Full flow: agent crashes → ticket moves to Needs Human Review with explanatory comment."""
    from orch.router import Router

    async def _crashing_agent(
        ticket_id: str, worktree_dir: Path, agent_type: str = "coder"
    ) -> int:
        return 1  # simulate crash, doesn't update ticket

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Failing task",
                "description": "This will fail",
                "acceptance_criteria": "- [ ] Won't happen",
            },
        )
        await move_ticket(db, ticket.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_crashing_agent,
        )
        await router.poll_once()

        final = await get_ticket(db, ticket.id)
        comments = await get_ticket_comments(db, ticket.id)

    assert final.state == "Needs Human Review"
    assert len(comments) == 1
    assert comments[0].author == "router"
    assert "exit" in comments[0].body.lower()


def test_cli_round_trip_create_show_move(tmp_path: Path) -> None:
    """CLI commands create, show, and move a ticket consistently."""
    import json

    from click.testing import CliRunner

    from orch.cli import main

    db_path = str(tmp_path / "state.db")
    runner = CliRunner()

    # Create a ticket from file
    ticket_file = tmp_path / "ticket.yaml"
    ticket_file.write_text(
        "title: CLI round trip\n"
        "description: Testing the CLI flow\n"
        "acceptance_criteria: '- [ ] It works'\n"
    )
    result = runner.invoke(
        main,
        ["--db", db_path, "tickets", "create", "--from-file", str(ticket_file)],
    )
    assert result.exit_code == 0, result.output
    ticket_id = result.output.strip().split()[-1]  # "Created ORCH-001"

    # Show ticket as JSON
    result = runner.invoke(main, ["--db", db_path, "tickets", "show", ticket_id, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["title"] == "CLI round trip"
    assert data["state"] == "Draft"

    # Move to To Do
    result = runner.invoke(main, ["--db", db_path, "tickets", "move", ticket_id, "To Do"])
    assert result.exit_code == 0, result.output
    assert "To Do" in result.output

    # Verify state changed
    result = runner.invoke(main, ["--db", db_path, "tickets", "show", ticket_id, "--json"])
    data = json.loads(result.output)
    assert data["state"] == "To Do"

    # List filtered by state
    result = runner.invoke(
        main,
        ["--db", db_path, "tickets", "list", "--state", "To Do", "--json"],
    )
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    assert len(items) == 1
    assert items[0]["id"] == ticket_id


def test_cli_comment_with_author(tmp_path: Path) -> None:
    """CLI comment --author sets the comment author correctly."""
    import json

    from click.testing import CliRunner

    from orch.cli import main

    db_path = str(tmp_path / "state.db")
    runner = CliRunner()

    # Create a ticket
    result = runner.invoke(
        main,
        ["--db", db_path, "tickets", "create", "--title", "Author test"],
    )
    ticket_id = result.output.strip().split()[-1]

    # Add comment with custom author
    result = runner.invoke(
        main,
        ["--db", db_path, "tickets", "comment", ticket_id, "Agent note", "--author", "coder"],
    )
    assert result.exit_code == 0, result.output

    # Verify author
    result = runner.invoke(main, ["--db", db_path, "tickets", "show", ticket_id, "--json"])
    data = json.loads(result.output)
    assert data["comments"][0]["author"] == "coder"


def test_database_path_property(tmp_db_path: Path) -> None:
    """Database.path exposes the database file path."""
    db = Database(tmp_db_path)
    assert db.path == tmp_db_path


async def test_router_db_path_abs(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router._db_path_abs returns the absolute path to the database."""
    from orch.router import Router

    async with Database(tmp_db_path) as db:
        router = Router(db, tmp_path)
        abs_path = router._db_path_abs()
        assert Path(abs_path).is_absolute()
        assert abs_path == str(tmp_db_path.resolve())
