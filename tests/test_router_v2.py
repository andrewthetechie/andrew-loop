"""Tests for router v2: full lifecycle dispatch."""

from pathlib import Path

from orch.db import Database
from orch.router import Router
from orch.tickets import create_ticket, move_ticket


async def _make_ticket(db: Database, *, state: str = "To Do", **overrides: object) -> str:
    """Create a ticket and move it to the given state."""
    data = {
        "title": overrides.pop("title", "Test ticket"),
        "description": "Test",
        "acceptance_criteria": "- [ ] works",
        **overrides,
    }
    ticket = await create_ticket(db, data)
    if state != "Draft":
        await move_ticket(db, ticket.id, state)
    return ticket.id


async def _noop_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
    pass


async def test_dispatches_correct_agent_per_state(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router dispatches coder for To Do, reviewer for Code Review, merger for Ready to Merge."""
    dispatches: list[tuple[str, str]] = []

    async def _spy_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        dispatches.append((ticket_id, agent_type))
        # Simulate agent advancing state
        async with Database(tmp_db_path) as db2:
            if agent_type == "coder":
                await move_ticket(db2, ticket_id, "Code Review")
            elif agent_type == "reviewer":
                await move_ticket(db2, ticket_id, "Ready to Merge")
            elif agent_type == "merger":
                await move_ticket(db2, ticket_id, "Done")
        return 0

    async with Database(tmp_db_path) as db:
        await _make_ticket(db, state="To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_spy_agent,
        )

        # Poll three times to walk the ticket through the lifecycle
        await router.poll_once()
        await router.poll_once()
        await router.poll_once()

    agents = [d[1] for d in dispatches]
    assert agents == ["coder", "reviewer", "merger"]


async def test_rework_loop_escalation(tmp_db_path: Path, tmp_path: Path) -> None:
    """After 3 rework loops the router escalates to Needs Human Review."""
    from orch.tickets import get_ticket, get_ticket_comments

    async def _coder_sends_to_review(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        async with Database(tmp_db_path) as db2:
            await move_ticket(db2, ticket_id, "Code Review")
        return 0

    async def _reviewer_sends_to_rework(
        ticket_id: str, worktree_dir: Path, agent_type: str
    ) -> int:
        async with Database(tmp_db_path) as db2:
            await move_ticket(db2, ticket_id, "Rework")
        return 0

    async with Database(tmp_db_path) as db:
        tid = await _make_ticket(db, state="To Do")

        poll_count = 0

        async def _agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
            nonlocal poll_count
            poll_count += 1
            async with Database(tmp_db_path) as db2:
                if agent_type == "coder":
                    await move_ticket(db2, ticket_id, "Code Review")
                elif agent_type == "reviewer":
                    await move_ticket(db2, ticket_id, "Rework")
            return 0

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_agent,
        )

        # Run enough polls to hit 3 rework loops
        # Each loop: coder (To Do/Rework -> Code Review), reviewer (Code Review -> Rework)
        for _ in range(20):
            result = await router.poll_once()
            if result is None:
                break

        ticket = await get_ticket(db, tid)
        comments = await get_ticket_comments(db, tid)

    assert ticket.state == "Needs Human Review"
    assert ticket.rework_loop_count >= 3
    escalation_comments = [c for c in comments if "rework" in c.body.lower()]
    assert len(escalation_comments) > 0


async def test_blocked_ticket_skipped(tmp_db_path: Path, tmp_path: Path) -> None:
    """Ticket with unfinished dependency is not dispatched."""
    from orch.tickets import add_dependencies

    dispatched: list[str] = []

    async def _record_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        dispatched.append(ticket_id)
        return 0

    async with Database(tmp_db_path) as db:
        blocker = await _make_ticket(db, state="To Do", title="Blocker")
        blocked = await _make_ticket(db, state="To Do", title="Blocked")
        await add_dependencies(db, blocked, [blocker])

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_record_agent,
        )
        await router.poll_once()

    # Only the blocker should have been dispatched
    assert dispatched == [blocker]


async def test_priority_most_dependents_first(tmp_db_path: Path, tmp_path: Path) -> None:
    """Ticket that unblocks more work is dispatched first."""
    from orch.tickets import add_dependencies

    dispatched: list[str] = []

    async def _record_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        dispatched.append(ticket_id)
        return 0

    async with Database(tmp_db_path) as db:
        # low_priority has no dependents
        await _make_ticket(db, state="To Do", title="Low priority")
        # high_priority blocks two other tickets
        high = await _make_ticket(db, state="To Do", title="High priority")
        child1 = await _make_ticket(db, state="Draft", title="Child 1")
        child2 = await _make_ticket(db, state="Draft", title="Child 2")
        await add_dependencies(db, child1, [high])
        await add_dependencies(db, child2, [high])

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_record_agent,
        )
        await router.poll_once()

    # High priority (2 dependents) dispatched before low (0 dependents)
    assert dispatched == [high]


async def test_webhook_fires_on_escalation(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router fires webhook when ticket escalates to Needs Human Review."""
    webhook_payloads: list[dict] = []

    async def _fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
        webhook_payloads.append(payload)
        return 200

    async def _crashing_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        return 1  # crash without updating state

    async with Database(tmp_db_path) as db:
        await _make_ticket(db, state="To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_crashing_agent,
            webhook_post=_fake_post,
            webhook_url="https://example.com/hook",
        )
        await router.poll_once()

    assert len(webhook_payloads) == 1
    assert webhook_payloads[0]["new_state"] == "Needs Human Review"


async def test_full_lifecycle_to_done(tmp_db_path: Path, tmp_path: Path) -> None:
    """Integration: ticket walks To Do -> Code Review -> Ready to Merge -> Done."""
    from orch.tickets import get_ticket

    states_seen: list[str] = []

    async def _advancing_agent(ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        async with Database(tmp_db_path) as db2:
            if agent_type == "coder":
                await move_ticket(db2, ticket_id, "Code Review")
            elif agent_type == "reviewer":
                await move_ticket(db2, ticket_id, "Ready to Merge")
            elif agent_type == "merger":
                await move_ticket(db2, ticket_id, "Done")
            t = await get_ticket(db2, ticket_id)
            states_seen.append(t.state)
        return 0

    async with Database(tmp_db_path) as db:
        tid = await _make_ticket(db, state="To Do")

        router = Router(
            db,
            tmp_path,
            create_worktree=_noop_worktree,
            run_agent=_advancing_agent,
        )

        # Keep polling until no more routable tickets
        for _ in range(10):
            result = await router.poll_once()
            if result is None:
                break

        ticket = await get_ticket(db, tid)

    assert ticket.state == "Done"
    assert states_seen == ["Code Review", "Ready to Merge", "Done"]
