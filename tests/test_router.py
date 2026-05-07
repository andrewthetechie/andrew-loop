"""Tests for the router: poll and dispatch."""

from pathlib import Path

from orch.db import Database
from orch.router import Router
from orch.tickets import create_ticket


async def _make_todo_ticket(
    db: Database, *, title: str = "Test ticket", **overrides: object
) -> str:
    """Create a ticket and move it to 'To Do' state, return its ID."""
    from orch.tickets import move_ticket

    data = {
        "title": title,
        "description": "A test ticket",
        "acceptance_criteria": "- [ ] It works",
        **overrides,
    }
    ticket = await create_ticket(db, data)
    await move_ticket(db, ticket.id, "To Do")
    return ticket.id


async def _noop_create_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
    """Fake worktree creation that does nothing."""


async def _noop_run_agent(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
    """Fake agent run that returns success."""
    return 0


async def test_poll_once_finds_todo_ticket(tmp_db_path: Path, tmp_path: Path) -> None:
    """poll_once picks up a 'To Do' ticket and returns its ID."""
    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_noop_run_agent,
        )
        result = await router.poll_once()

    assert result == ticket_id


async def test_poll_once_returns_none_when_no_todo(tmp_db_path: Path, tmp_path: Path) -> None:
    """poll_once returns None when no tickets are in 'To Do' state."""
    async with Database(tmp_db_path) as db:
        # Create a ticket but leave it in Draft
        await create_ticket(
            db,
            {
                "title": "Draft ticket",
                "description": "Not ready",
                "acceptance_criteria": "N/A",
            },
        )

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_noop_run_agent,
        )
        result = await router.poll_once()

    assert result is None


async def test_dispatch_moves_ticket_to_in_progress(tmp_db_path: Path, tmp_path: Path) -> None:
    """Dispatching a ticket moves it to 'In Progress' and records worktree_path."""
    from orch.tickets import get_ticket

    states_seen: list[str] = []

    async def _spy_run_agent(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
        """Capture ticket state at the moment the agent runs."""
        async with Database(tmp_db_path) as db2:
            ticket = await get_ticket(db2, ticket_id)
            states_seen.append(ticket.state)
        return 0

    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_spy_run_agent,
        )
        await router.poll_once()

        ticket = await get_ticket(db, ticket_id)

    assert states_seen == ["In Progress"]
    assert ticket.worktree_path is not None
    assert ticket_id in ticket.worktree_path


async def test_dispatch_creates_worktree_with_correct_args(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """Dispatch calls create_worktree with ticket ID, branch, and worktree path."""
    worktree_calls: list[tuple[str, str, Path]] = []

    async def _record_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
        worktree_calls.append((ticket_id, branch, worktree_dir))

    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_record_worktree,
            run_agent=_noop_run_agent,
        )
        await router.poll_once()

    assert len(worktree_calls) == 1
    tid, branch, wt_path = worktree_calls[0]
    assert tid == ticket_id
    assert branch == f"ticket/{ticket_id}"
    assert wt_path == tmp_path / ".orchestra" / "worktrees" / ticket_id


async def test_dispatch_launches_agent_with_correct_args(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """Dispatch calls run_agent with ticket ID and worktree directory."""
    agent_calls: list[tuple[str, Path]] = []

    async def _record_agent(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
        agent_calls.append((ticket_id, worktree_dir))
        return 0

    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_record_agent,
        )
        await router.poll_once()

    assert len(agent_calls) == 1
    tid, wt_dir = agent_calls[0]
    assert tid == ticket_id
    assert wt_dir == tmp_path / ".orchestra" / "worktrees" / ticket_id


async def test_agent_updates_state_router_leaves_it(tmp_db_path: Path, tmp_path: Path) -> None:
    """If the agent moves ticket out of 'In Progress', router doesn't override."""
    from orch.tickets import get_ticket, move_ticket

    async def _agent_moves_to_code_review(
        ticket_id: str, worktree_dir: Path, agent_type: str = "coder"
    ) -> int:
        async with Database(tmp_db_path) as db2:
            await move_ticket(db2, ticket_id, "Code Review")
        return 0

    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_agent_moves_to_code_review,
        )
        await router.poll_once()

        ticket = await get_ticket(db, ticket_id)

    assert ticket.state == "Code Review"


async def test_agent_fails_ticket_moves_to_needs_human_review(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """If agent exits without updating state, router moves to 'Needs Human Review'."""
    from orch.tickets import get_ticket, get_ticket_comments

    async def _agent_fails(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
        return 1  # non-zero exit, doesn't update ticket

    async with Database(tmp_db_path) as db:
        ticket_id = await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_agent_fails,
        )
        await router.poll_once()

        ticket = await get_ticket(db, ticket_id)
        comments = await get_ticket_comments(db, ticket_id)

    assert ticket.state == "Needs Human Review"
    assert len(comments) == 1
    assert "exit" in comments[0].body.lower()
    assert comments[0].author == "router"


async def test_poll_once_picks_fifo_by_created_at(tmp_db_path: Path, tmp_path: Path) -> None:
    """When multiple 'To Do' tickets exist, poll_once picks the oldest."""
    async with Database(tmp_db_path) as db:
        first_id = await _make_todo_ticket(db, title="First")
        await _make_todo_ticket(db, title="Second")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_noop_run_agent,
        )
        result = await router.poll_once()

    assert result == first_id


async def test_run_loop_stops_on_stop_signal(tmp_db_path: Path, tmp_path: Path) -> None:
    """run() polls repeatedly and stops when stop() is called."""
    import asyncio

    poll_count = 0

    async def _counting_agent(
        ticket_id: str, worktree_dir: Path, agent_type: str = "coder"
    ) -> int:
        nonlocal poll_count
        poll_count += 1
        return 0

    async with Database(tmp_db_path) as db:
        await _make_todo_ticket(db, title="Ticket A")
        await _make_todo_ticket(db, title="Ticket B")

        router = Router(
            db,
            tmp_path,
            poll_interval=0.05,
            create_worktree=_noop_create_worktree,
            run_agent=_counting_agent,
        )

        async def _stop_after_delay() -> None:
            await asyncio.sleep(0.3)
            router.stop()

        await asyncio.gather(router.run(), _stop_after_delay())

    # Should have dispatched at least one ticket
    assert poll_count >= 1


def test_cli_router_start_registers_command(tmp_path: Path) -> None:
    """'orch router start' is a valid CLI command that starts and can be stopped."""
    from unittest.mock import AsyncMock, patch

    from click.testing import CliRunner

    from orch.cli import main

    mock_router = AsyncMock()
    mock_router.run = AsyncMock(return_value=None)
    mock_router.stop = lambda: None  # sync, like the real Router.stop()

    db_path = tmp_path / ".orchestra" / "state.db"

    with patch("orch.cli.Router", return_value=mock_router) as mock_cls:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--db", str(db_path), "router", "start", "--interval", "0.01"],
        )

    assert result.exit_code == 0, result.output
    assert "Router started" in result.output
    mock_cls.assert_called_once()
    mock_router.run.assert_awaited_once()


async def test_graceful_shutdown_waits_for_dispatch(tmp_db_path: Path, tmp_path: Path) -> None:
    """Stopping the router mid-dispatch waits for the current agent to finish."""
    import asyncio

    dispatch_completed = False

    async def _slow_agent(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
        nonlocal dispatch_completed
        await asyncio.sleep(0.2)
        dispatch_completed = True
        return 0

    async with Database(tmp_db_path) as db:
        await _make_todo_ticket(db)

        router = Router(
            db,
            tmp_path,
            poll_interval=0.05,
            create_worktree=_noop_create_worktree,
            run_agent=_slow_agent,
        )

        async def _stop_during_dispatch() -> None:
            await asyncio.sleep(0.05)  # stop while agent is still running
            router.stop()

        await asyncio.gather(router.run(), _stop_during_dispatch())

    # The dispatch should have completed even though we called stop()
    assert dispatch_completed is True
