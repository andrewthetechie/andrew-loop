"""Comprehensive end-to-end tests for the full agent workflow pipeline (issue 27)."""

from pathlib import Path

from orch.db import Database
from orch.tickets import (
    add_dependencies,
    create_ticket,
    get_ticket,
    list_events,
    move_ticket,
    update_ticket,
)


async def _noop_create_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
    """No-op worktree creation for tests."""


# ---------------------------------------------------------------------------
# Test 1 — tracer bullet: full 3-agent pipeline
# ---------------------------------------------------------------------------


async def test_full_pipeline_coder_reviewer_merger_to_done(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """Coder → reviewer → merger: ticket reaches Done through all states."""
    from orch.router import Router

    async def _advancing_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        next_state = {"coder": "Code Review", "reviewer": "Ready to Merge", "merger": "Done"}
        async with Database(tmp_db_path) as db:
            await update_ticket(db, ticket_id, state=next_state[agent_type])
        return 0

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Add greeting endpoint",
                "description": "Add GET /hello that returns 200.",
                "acceptance_criteria": "- [ ] GET /hello returns 200",
            },
        )
        await move_ticket(db, ticket.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_advancing_agent,
        )

        # Coder pass: To Do → In Progress → Code Review
        await router.poll_once()
        assert (await get_ticket(db, ticket.id)).state == "Code Review"

        # Reviewer pass: Code Review → In Progress → Ready to Merge
        await router.poll_once()
        assert (await get_ticket(db, ticket.id)).state == "Ready to Merge"

        # Merger pass: Ready to Merge → In Progress → Done
        await router.poll_once()
        assert (await get_ticket(db, ticket.id)).state == "Done"


# ---------------------------------------------------------------------------
# Test 2 — dependency enforcement end-to-end
# ---------------------------------------------------------------------------


async def test_dependent_ticket_blocked_until_dependency_done(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    """TKT-B does not dispatch until TKT-A is Done."""
    from orch.router import Router

    dispatch_order: list[str] = []

    async def _complete_immediately(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        dispatch_order.append(ticket_id)
        async with Database(tmp_db_path) as db:
            await update_ticket(db, ticket_id, state="Done")
        return 0

    async with Database(tmp_db_path) as db:
        a = await create_ticket(
            db,
            {
                "title": "Foundation",
                "description": "Must be done first.",
                "acceptance_criteria": "- [ ] done",
            },
        )
        b = await create_ticket(
            db,
            {
                "title": "Dependent",
                "description": "Requires foundation.",
                "acceptance_criteria": "- [ ] done",
            },
        )
        await add_dependencies(db, b.id, [a.id])
        await move_ticket(db, a.id, "To Do")
        await move_ticket(db, b.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_complete_immediately,
        )

        # First poll: only A is routable; B is blocked by A
        first = await router.poll_once()
        assert first == a.id

        # Second poll: A is Done, B is now routable
        second = await router.poll_once()
        assert second == b.id

    assert dispatch_order == [a.id, b.id]


# ---------------------------------------------------------------------------
# Test 3 — event log completeness
# ---------------------------------------------------------------------------


async def test_event_log_records_complete_chain(tmp_db_path: Path, tmp_path: Path) -> None:
    """Event log contains all state transitions from Draft to Done in order."""
    from orch.router import Router

    async def _advancing_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        next_state = {"coder": "Code Review", "reviewer": "Ready to Merge", "merger": "Done"}
        async with Database(tmp_db_path) as db:
            await update_ticket(db, ticket_id, state=next_state[agent_type])
        return 0

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Traced ticket",
                "description": "For event log verification.",
                "acceptance_criteria": "- [ ] all events recorded",
            },
        )
        await move_ticket(db, ticket.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_advancing_agent,
        )
        await router.poll_once()  # coder
        await router.poll_once()  # reviewer
        await router.poll_once()  # merger

        events = await list_events(db)
        ticket_events = [e for e in events if e.ticket_id == ticket.id]
        state_pairs = [(e.old_state, e.new_state) for e in ticket_events]

    expected = [
        ("Draft", "To Do"),
        ("To Do", "In Progress"),
        ("In Progress", "Code Review"),
        ("Code Review", "In Progress"),
        ("In Progress", "Ready to Merge"),
        ("Ready to Merge", "In Progress"),
        ("In Progress", "Done"),
    ]
    for pair in expected:
        assert pair in state_pairs, f"Missing transition {pair[0]} → {pair[1]}"


# ---------------------------------------------------------------------------
# Test 4 — agent log file written to .orchestra/logs/
# ---------------------------------------------------------------------------


async def test_agent_log_file_written_to_orchestra_logs(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router._default_run_agent writes stdout+stderr to .orchestra/logs/<ticket-id>.log."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from orch.router import Router

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"agent output here", b"some stderr"))

    ticket_id = "ORCH-0042"
    worktree_dir = tmp_path / ".orchestra" / "worktrees" / ticket_id

    async with Database(tmp_db_path) as db:
        router = Router(db, tmp_path)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await router._default_run_agent(ticket_id, worktree_dir, "coder")

    log_file = tmp_path / ".orchestra" / "logs" / f"{ticket_id}.log"
    assert log_file.exists(), f"Log file not found: {log_file}"
    content = log_file.read_bytes()
    assert b"agent output here" in content
    assert b"some stderr" in content


# ---------------------------------------------------------------------------
# Test 5 — Hindsight retain called on ticket Done
# ---------------------------------------------------------------------------


async def test_hindsight_retain_called_on_ticket_done(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router calls hindsight_client.aretain with correct args when ticket reaches Done."""
    from unittest.mock import AsyncMock

    from orch.router import Router

    mock_client = AsyncMock()

    async def _completes_to_done(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        async with Database(tmp_db_path) as db:
            await update_ticket(db, ticket_id, state="Done")
        return 0

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Hindsight test",
                "description": "Should retain on completion.",
                "acceptance_criteria": "- [ ] hindsight called",
            },
        )
        await move_ticket(db, ticket.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_completes_to_done,
            hindsight_client=mock_client,
            hindsight_bank_id="test-bank",
        )
        await router.poll_once()

    mock_client.aretain.assert_called_once()
    call_kwargs = mock_client.aretain.call_args.kwargs
    assert call_kwargs["bank_id"] == "test-bank"
    assert call_kwargs["context"] == "ticket-outcome"
    assert ticket.id in call_kwargs["document_id"]


# ---------------------------------------------------------------------------
# Test 6 — rework escalation webhook payload fields
# ---------------------------------------------------------------------------


async def test_rework_escalation_webhook_payload_fields(tmp_db_path: Path, tmp_path: Path) -> None:
    """Webhook fires once with correct payload fields after 3 rework loops."""
    from orch.router import MAX_REWORK_LOOPS, Router

    webhook_calls: list[dict] = []

    async def _fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        webhook_calls.append(payload)
        return 200

    async def _coder_sends_to_rework(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        async with Database(tmp_db_path) as db:
            await update_ticket(db, ticket_id, state="Rework")
        return 0

    async with Database(tmp_db_path) as db:
        ticket = await create_ticket(
            db,
            {
                "title": "Stubborn ticket",
                "description": "Will keep failing review.",
                "acceptance_criteria": "- [ ] passes review",
                "risk_score": 4,
            },
        )
        await move_ticket(db, ticket.id, "To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_create_worktree,
            run_agent=_coder_sends_to_rework,
            webhook_post=_fake_post,
            webhook_url="https://hooks.example.com/test",
        )

        # Drive through all rework cycles until escalation fires.
        # Cycle 0: To Do → In Progress → Rework (rework_loop_count stays 0, was not Rework)
        # Cycles 1..MAX_REWORK_LOOPS: each increments count then dispatches → Rework again
        # Final call: count == MAX_REWORK_LOOPS → escalate → Needs Human Review + webhook
        for _ in range(MAX_REWORK_LOOPS + 2):
            await router.poll_once()

        final = await get_ticket(db, ticket.id)

    assert final.state == "Needs Human Review"
    assert len(webhook_calls) == 1, f"Expected 1 webhook call, got {len(webhook_calls)}"

    payload = webhook_calls[0]
    assert payload["ticket_id"] == ticket.id
    assert payload["title"] == "Stubborn ticket"
    assert payload["new_state"] == "Needs Human Review"
    assert "old_state" in payload
    assert "risk_score" in payload
