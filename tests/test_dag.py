"""Tests for ticket dependency DAG."""

from pathlib import Path

from orch.db import Database
from orch.tickets import (
    add_dependencies,
    create_ticket,
    get_dependencies,
    get_dependents,
    get_next_routable,
    move_ticket,
)


async def _make_ticket(db: Database, title: str = "T") -> str:
    """Create a Draft ticket and return its ID."""
    t = await create_ticket(db, {"title": title, "description": "D", "acceptance_criteria": "AC"})
    return t.id


async def test_add_and_query_dependencies(tmp_db_path: Path) -> None:
    """Adding dependencies records them; they can be queried in both directions."""
    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        c = await _make_ticket(db, "C")

        # B depends on A; C depends on A
        await add_dependencies(db, b, [a])
        await add_dependencies(db, c, [a])

        b_deps = await get_dependencies(db, b)
        c_deps = await get_dependencies(db, c)
        a_dependents = await get_dependents(db, a)

    assert b_deps == [a]
    assert c_deps == [a]
    assert sorted(a_dependents) == sorted([b, c])


async def test_circular_dependency_rejected(tmp_db_path: Path) -> None:
    """Adding a dependency that creates a cycle raises ValueError."""
    import pytest

    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        c = await _make_ticket(db, "C")

        await add_dependencies(db, b, [a])  # B depends on A
        await add_dependencies(db, c, [b])  # C depends on B

        with pytest.raises(ValueError, match="cycle"):
            await add_dependencies(db, a, [c])  # A depends on C — cycle!


async def test_ticket_with_unfinished_deps_not_routable(tmp_db_path: Path) -> None:
    """A To Do ticket whose deps aren't all Done is not routable."""
    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        await add_dependencies(db, b, [a])

        # Move both to To Do
        await move_ticket(db, a, "To Do")
        await move_ticket(db, b, "To Do")

        # B depends on A which is still To Do — only A is routable
        routable = await get_next_routable(db)

    assert routable is not None
    assert routable == a


async def test_ticket_with_all_deps_done_is_routable(tmp_db_path: Path) -> None:
    """A To Do ticket whose deps are all Done becomes routable."""
    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        await add_dependencies(db, b, [a])

        await move_ticket(db, a, "To Do")
        await move_ticket(db, b, "To Do")

        # Complete A
        await move_ticket(db, a, "In Progress")
        await move_ticket(db, a, "Done")

        # Now B should be routable
        routable = await get_next_routable(db)

    assert routable == b


async def test_priority_most_transitive_dependents_first(tmp_db_path: Path) -> None:
    """Ticket that unblocks the most downstream work is dispatched first."""
    async with Database(tmp_db_path) as db:
        # Create chain: D->C->A and E->A (A has 3 transitive dependents)
        # Also: F->B (B has 1 transitive dependent)
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        c = await _make_ticket(db, "C")
        d = await _make_ticket(db, "D")
        e = await _make_ticket(db, "E")
        f = await _make_ticket(db, "F")

        await add_dependencies(db, c, [a])  # C depends on A
        await add_dependencies(db, d, [c])  # D depends on C
        await add_dependencies(db, e, [a])  # E depends on A
        await add_dependencies(db, f, [b])  # F depends on B

        # Move A and B to To Do — both routable (no deps)
        await move_ticket(db, a, "To Do")
        await move_ticket(db, b, "To Do")

        # A has 3 transitive dependents (C, D, E), B has 1 (F)
        routable = await get_next_routable(db)

    assert routable == a  # A dispatched first — unblocks more work


def test_cli_create_with_depends_on_and_show(tmp_path: Path) -> None:
    """Create with --depends-on records deps; show displays them."""
    import json

    import yaml
    from click.testing import CliRunner

    from orch.cli import main

    db_path = tmp_path / ".orchestra" / "state.db"
    runner = CliRunner()

    # Create ticket A
    tf = tmp_path / "t.yaml"
    tf.write_text(yaml.dump({"title": "A", "description": "D", "acceptance_criteria": "AC"}))
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "create", "--from-file", str(tf)])
    assert r.exit_code == 0, r.output
    a_id = r.output.strip().split()[-1]

    # Create ticket B depending on A
    r = runner.invoke(
        main,
        [
            "--db",
            str(db_path),
            "tickets",
            "create",
            "--from-file",
            str(tf),
            "--depends-on",
            a_id,
        ],
    )
    assert r.exit_code == 0, r.output
    b_id = r.output.strip().split()[-1]

    # Show B — should display dependency on A
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "show", b_id, "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert a_id in data.get("depends_on", [])

    # Show A — should display that it blocks B
    r = runner.invoke(main, ["--db", str(db_path), "tickets", "show", a_id, "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert b_id in data.get("blocks", [])


async def test_router_respects_dependencies(tmp_db_path: Path, tmp_path: Path) -> None:
    """Router dispatches only tickets whose deps are satisfied."""
    from orch.router import Router

    dispatched: list[str] = []

    async def _noop_wt(ticket_id: str, branch: str, worktree_dir: Path) -> None:
        pass

    async def _record_agent(ticket_id: str, worktree_dir: Path, agent_type: str = "coder") -> int:
        dispatched.append(ticket_id)
        # Move to Done so next ticket becomes routable
        await move_ticket(db, ticket_id, "Code Review")
        await move_ticket(db, ticket_id, "Done")
        return 0

    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        await add_dependencies(db, b, [a])

        await move_ticket(db, a, "To Do")
        await move_ticket(db, b, "To Do")

        router = Router(db, tmp_path, create_worktree=_noop_wt, run_agent=_record_agent)

        # First poll should dispatch A (B's dep not met)
        await router.poll_once()
        # Second poll should dispatch B (A is now Done)
        await router.poll_once()

    assert dispatched == [a, b]


async def test_dispatch_chain_a_b_c(tmp_db_path: Path, tmp_path: Path) -> None:
    """Integration: chain A -> B -> C dispatches in correct order."""
    from orch.router import Router

    dispatched: list[str] = []

    async def _noop_wt(ticket_id: str, branch: str, worktree_dir: Path) -> None:
        pass

    async def _record_and_complete(
        ticket_id: str, worktree_dir: Path, agent_type: str = "coder"
    ) -> int:
        dispatched.append(ticket_id)
        await move_ticket(db, ticket_id, "Code Review")
        await move_ticket(db, ticket_id, "Done")
        return 0

    async with Database(tmp_db_path) as db:
        a = await _make_ticket(db, "A")
        b = await _make_ticket(db, "B")
        c = await _make_ticket(db, "C")
        await add_dependencies(db, b, [a])
        await add_dependencies(db, c, [b])

        await move_ticket(db, a, "To Do")
        await move_ticket(db, b, "To Do")
        await move_ticket(db, c, "To Do")

        router = Router(db, tmp_path, create_worktree=_noop_wt, run_agent=_record_and_complete)

        await router.poll_once()  # A
        await router.poll_once()  # B
        await router.poll_once()  # C

    assert dispatched == [a, b, c]
