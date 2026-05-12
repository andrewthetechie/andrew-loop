"""Ticket CRUD operations."""

from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orch.db import VALID_STATES, Database, Event, Ticket, TicketComment, TicketDependency

# Fields that can appear in the ticket YAML
YAML_FIELDS = (
    "title",
    "description",
    "acceptance_criteria",
    "file_paths",
    "test_expectations",
    "risk_score",
    "issue_id",
)

REQUIRED_FIELDS = ("title", "description", "acceptance_criteria")

# Fields required before a ticket can move to "To Do"
TO_DO_REQUIRED = ("title", "description", "acceptance_criteria")


def _next_ticket_id(existing_ids: list[str]) -> str:
    """Generate the next ORCH-NNN ticket ID."""
    max_num = 0
    for tid in existing_ids:
        if tid.startswith("ORCH-"):
            try:
                num = int(tid.split("-", 1)[1])
                max_num = max(max_num, num)
            except ValueError:
                continue
    return f"ORCH-{max_num + 1:03d}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_ticket_yaml(content: str, *, strict: bool = True) -> dict:
    """Parse a YAML string into a ticket data dict.

    When strict=True (default), required fields must be present and non-empty.
    When strict=False, only 'title' is required (for quick Draft creation).
    """
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        msg = "Ticket YAML must be a mapping."
        raise TypeError(msg)

    if strict:
        for field in REQUIRED_FIELDS:
            if not data.get(field):
                msg = f"Required field '{field}' is missing or empty."
                raise ValueError(msg)
    elif not data.get("title"):
        msg = "Required field 'title' is missing or empty."
        raise ValueError(msg)

    # Validate risk_score if present
    risk = data.get("risk_score")
    if risk is not None and (not isinstance(risk, int) or not (1 <= risk <= 5)):
        msg = "risk_score must be an integer between 1 and 5."
        raise ValueError(msg)

    return data


def ticket_to_dict(ticket: Ticket, comments: list[TicketComment] | None = None) -> dict:
    """Convert a Ticket ORM object to a plain dict."""
    d = {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "acceptance_criteria": ticket.acceptance_criteria,
        "file_paths": ticket.file_paths,
        "test_expectations": ticket.test_expectations,
        "state": ticket.state,
        "assignee": ticket.assignee,
        "risk_score": ticket.risk_score,
        "rework_loop_count": ticket.rework_loop_count,
        "linked_pr": ticket.linked_pr,
        "worktree_path": ticket.worktree_path,
        "issue_id": ticket.issue_id,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }
    if comments is not None:
        d["comments"] = [
            {
                "id": c.id,
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at,
            }
            for c in comments
        ]
    return d


def ticket_to_yaml(ticket: Ticket) -> str:
    """Convert a Ticket to editable YAML (round-trippable with parse_ticket_yaml)."""
    data = {
        "title": ticket.title,
        "description": ticket.description,
        "acceptance_criteria": ticket.acceptance_criteria,
        "file_paths": ticket.file_paths,
        "test_expectations": ticket.test_expectations,
        "risk_score": ticket.risk_score,
    }
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


async def import_tickets(db: Database, tickets_data: list[dict]) -> list[Ticket]:
    """Import multiple tickets atomically. Validates all before creating any.

    Tickets can declare dependencies via a ``depends_on`` list of titles
    referencing other tickets in the same import batch.
    """
    # Validate all tickets first (fail-fast)
    for data in tickets_data:
        # Temporarily remove depends_on for YAML validation (not a ticket field)
        deps = data.pop("depends_on", None)
        parse_ticket_yaml(
            yaml.dump(data, default_flow_style=False),
            strict=True,
        )
        if deps is not None:
            data["depends_on"] = deps

    # Create all tickets
    created: list[Ticket] = []
    title_to_id: dict[str, str] = {}
    for data in tickets_data:
        deps = data.pop("depends_on", None)
        ticket = await create_ticket(db, data)
        created.append(ticket)
        title_to_id[ticket.title] = ticket.id
        if deps is not None:
            data["depends_on"] = deps

    # Wire up dependencies by title
    for data, ticket in zip(tickets_data, created, strict=True):
        dep_titles = data.get("depends_on", [])
        if dep_titles:
            dep_ids = []
            for title in dep_titles:
                dep_id = title_to_id.get(title)
                if dep_id is None:
                    msg = f"Dependency '{title}' not found in import batch."
                    raise ValueError(msg)
                dep_ids.append(dep_id)
            await add_dependencies(db, ticket.id, dep_ids)

    return created


PROMOTE_REQUIRED = ("title", "description", "acceptance_criteria", "risk_score")


async def promote_tickets(db: Database, *, ticket_id: str | None = None) -> list[Ticket]:
    """Move Draft tickets to To Do. Validates required fields before promoting.

    If ticket_id is given, promotes only that ticket. Otherwise promotes all Drafts.
    """
    drafts = await list_tickets(db, state_filter="Draft")

    if ticket_id is not None:
        drafts = [t for t in drafts if t.id == ticket_id]
        if not drafts:
            msg = f"Ticket '{ticket_id}' not found or not in Draft state."
            raise ValueError(msg)

    promoted: list[Ticket] = []
    for ticket in drafts:
        for field in PROMOTE_REQUIRED:
            if not getattr(ticket, field, None):
                msg = f"Cannot promote '{ticket.id}': field '{field}' is required."
                raise ValueError(msg)
        moved = await move_ticket(db, ticket.id, "To Do")
        promoted.append(moved)

    return promoted


async def create_ticket(db: Database, data: dict) -> Ticket:
    """Create a new ticket in Draft state."""
    issue_id = data.get("issue_id")
    if issue_id is not None and not isinstance(issue_id, int):
        msg = "issue_id must be an integer when provided."
        raise ValueError(msg)

    async with db.session() as session:
        existing = await _get_all_ticket_ids(session)
        ticket_id = _next_ticket_id(existing)
        now = _now_iso()

        ticket = Ticket(
            id=ticket_id,
            title=data["title"],
            description=data["description"],
            acceptance_criteria=data["acceptance_criteria"],
            file_paths=data.get("file_paths"),
            test_expectations=data.get("test_expectations"),
            state="Draft",
            risk_score=data.get("risk_score"),
            rework_loop_count=0,
            issue_id=issue_id,
            created_at=now,
            updated_at=now,
        )
        session.add(ticket)
        await session.commit()
        return ticket


async def list_tickets(db: Database, *, state_filter: str | None = None) -> list[Ticket]:
    """List tickets, optionally filtered by state."""
    async with db.session() as session:
        stmt = select(Ticket).order_by(Ticket.created_at)
        if state_filter:
            stmt = stmt.where(Ticket.state == state_filter)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_ticket(db: Database, ticket_id: str) -> Ticket | None:
    """Get a single ticket by ID."""
    async with db.session() as session:
        result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        return result.scalar_one_or_none()


async def get_ticket_comments(db: Database, ticket_id: str) -> list[TicketComment]:
    """Get all comments for a ticket."""
    async with db.session() as session:
        result = await session.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at)
        )
        return list(result.scalars().all())


async def move_ticket(
    db: Database, ticket_id: str, new_state: str, *, actor: str = "human"
) -> Ticket:
    """Move a ticket to a new state with validation."""
    if new_state not in VALID_STATES:
        msg = f"Invalid state '{new_state}'. Valid states: {', '.join(VALID_STATES)}"
        raise ValueError(msg)

    async with db.session() as session:
        result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            msg = f"Ticket '{ticket_id}' not found."
            raise ValueError(msg)

        # Validate required fields for To Do
        if new_state == "To Do":
            for field in TO_DO_REQUIRED:
                if not getattr(ticket, field):
                    msg = f"Cannot move to 'To Do': field '{field}' is required."
                    raise ValueError(msg)

        old_state = ticket.state
        ticket.state = new_state
        ticket.updated_at = _now_iso()

        event = Event(
            ticket_id=ticket_id,
            timestamp=_now_iso(),
            actor=actor,
            old_state=old_state,
            new_state=new_state,
        )
        session.add(event)

        await session.commit()
        await session.refresh(ticket)
        return ticket


async def update_ticket(
    db: Database, ticket_id: str, *, actor: str = "human", **fields: object
) -> Ticket:
    """Update specific fields on a ticket."""
    async with db.session() as session:
        result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            msg = f"Ticket '{ticket_id}' not found."
            raise ValueError(msg)

        old_state = ticket.state
        for key, value in fields.items():
            if value is not None and hasattr(ticket, key):
                setattr(ticket, key, value)

        # Write event if state changed
        if ticket.state != old_state:
            event = Event(
                ticket_id=ticket_id,
                timestamp=_now_iso(),
                actor=actor,
                old_state=old_state,
                new_state=ticket.state,
            )
            session.add(event)

        ticket.updated_at = _now_iso()
        await session.commit()
        await session.refresh(ticket)
        return ticket


async def edit_ticket_from_yaml(db: Database, ticket_id: str, content: str) -> Ticket:
    """Update a ticket's editable fields from YAML content."""
    data = parse_ticket_yaml(content)

    async with db.session() as session:
        result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            msg = f"Ticket '{ticket_id}' not found."
            raise ValueError(msg)

        for field in YAML_FIELDS:
            if field in data:
                setattr(ticket, field, data[field])

        ticket.updated_at = _now_iso()
        await session.commit()
        await session.refresh(ticket)
        return ticket


async def add_comment(
    db: Database, ticket_id: str, body: str, author: str = "human"
) -> TicketComment:
    """Add a comment to a ticket."""
    # Verify ticket exists
    async with db.session() as session:
        result = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            msg = f"Ticket '{ticket_id}' not found."
            raise ValueError(msg)

        comment = TicketComment(
            ticket_id=ticket_id,
            author=author,
            body=body,
            created_at=_now_iso(),
        )
        session.add(comment)
        await session.commit()
        return comment


async def _get_all_ticket_ids(session: AsyncSession) -> list[str]:
    result = await session.execute(select(Ticket.id))
    return [row[0] for row in result.all()]


async def list_events(
    db: Database, *, ticket_id: str | None = None, limit: int | None = None
) -> list[Event]:
    """List events from the append-only event log, newest last."""
    async with db.session() as session:
        stmt = select(Event).order_by(Event.id)
        if ticket_id:
            stmt = stmt.where(Event.ticket_id == ticket_id)
        if limit:
            # Get the last N events: subquery to get count, then offset
            count_result = await session.execute(select(func.count()).select_from(stmt.subquery()))
            total = count_result.scalar() or 0
            if total > limit:
                stmt = stmt.offset(total - limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# --- Dependency DAG ---
# Uses graphlib.TopologicalSorter (stdlib since Python 3.9) for cycle detection.
# graphlib was chosen over networkx because it's stdlib, zero-dependency, and provides
# exactly the two operations we need: topological sort and cycle detection.


async def add_dependencies(db: Database, ticket_id: str, depends_on_ids: list[str]) -> None:
    """Add dependency edges. Rejects cycles using graphlib.TopologicalSorter."""
    async with db.session() as session:
        # Verify all tickets exist
        for tid in [ticket_id, *depends_on_ids]:
            result = await session.execute(select(Ticket).where(Ticket.id == tid))
            if result.scalar_one_or_none() is None:
                msg = f"Ticket '{tid}' not found."
                raise ValueError(msg)

        # Load existing graph
        graph = await _load_dependency_graph(session)

        # Add proposed edges and check for cycles
        if ticket_id not in graph:
            graph[ticket_id] = set()
        graph[ticket_id].update(depends_on_ids)
        for dep_id in depends_on_ids:
            if dep_id not in graph:
                graph[dep_id] = set()

        try:
            TopologicalSorter(graph).prepare()
        except CycleError as e:
            msg = f"Adding dependencies would create a cycle: {e}"
            raise ValueError(msg) from e

        # Persist
        for dep_id in depends_on_ids:
            dep = TicketDependency(ticket_id=ticket_id, depends_on_ticket_id=dep_id)
            session.add(dep)
        await session.commit()


async def remove_dependencies(db: Database, ticket_id: str, depends_on_ids: list[str]) -> None:
    """Remove dependency edges from a ticket."""
    async with db.session() as session:
        for tid in [ticket_id, *depends_on_ids]:
            result = await session.execute(select(Ticket).where(Ticket.id == tid))
            if result.scalar_one_or_none() is None:
                msg = f"Ticket '{tid}' not found."
                raise ValueError(msg)

        if depends_on_ids:
            await session.execute(
                delete(TicketDependency).where(
                    TicketDependency.ticket_id == ticket_id,
                    TicketDependency.depends_on_ticket_id.in_(depends_on_ids),
                )
            )
        await session.commit()


async def get_dependencies(db: Database, ticket_id: str) -> list[str]:
    """Get the ticket IDs that this ticket depends on."""
    async with db.session() as session:
        result = await session.execute(
            select(TicketDependency.depends_on_ticket_id).where(
                TicketDependency.ticket_id == ticket_id
            )
        )
        return [row[0] for row in result.all()]


async def get_dependents(db: Database, ticket_id: str) -> list[str]:
    """Get the ticket IDs that depend on this ticket (i.e. this ticket blocks them)."""
    async with db.session() as session:
        result = await session.execute(
            select(TicketDependency.ticket_id).where(
                TicketDependency.depends_on_ticket_id == ticket_id
            )
        )
        return [row[0] for row in result.all()]


async def _load_dependency_graph(session: object) -> dict[str, set[str]]:
    """Load the full dependency graph from the DB as {ticket: {deps...}}."""
    result = await session.execute(select(TicketDependency))
    rows = result.all()
    graph: dict[str, set[str]] = {}
    for row in rows:
        dep = row[0]
        if dep.ticket_id not in graph:
            graph[dep.ticket_id] = set()
        graph[dep.ticket_id].add(dep.depends_on_ticket_id)
        if dep.depends_on_ticket_id not in graph:
            graph[dep.depends_on_ticket_id] = set()
    return graph


async def get_next_routable(
    db: Database,
    *,
    states: tuple[str, ...] = ("To Do",),
    issue_id: int | None = None,
) -> str | None:
    """Find the next routable ticket, respecting dependencies and priority.

    A ticket is routable when it's in one of the given states and all its
    dependencies are 'Done'.
    When *issue_id* is set, only tickets with that issue_id are considered.
    Priority: most transitive dependents first (unblocks the most work), FIFO tiebreaker.
    """
    async with db.session() as session:
        # Get tickets in routable states
        query = select(Ticket).where(Ticket.state.in_(states))
        if issue_id is not None:
            query = query.where(Ticket.issue_id == issue_id)
        result = await session.execute(query.order_by(Ticket.created_at))
        routable_candidates = list(result.scalars().all())

        if not routable_candidates:
            return None

        # Load all dependencies
        dep_result = await session.execute(select(TicketDependency))
        all_deps = dep_result.all()

        # Build dependency map: ticket -> set of deps
        deps_map: dict[str, set[str]] = {}
        # Build reverse map: ticket -> set of dependents
        dependents_map: dict[str, set[str]] = {}
        for row in all_deps:
            dep = row[0]
            deps_map.setdefault(dep.ticket_id, set()).add(dep.depends_on_ticket_id)
            dependents_map.setdefault(dep.depends_on_ticket_id, set()).add(dep.ticket_id)

        # Get all ticket states for dependency checks
        all_tickets_result = await session.execute(select(Ticket.id, Ticket.state))
        state_map = {row[0]: row[1] for row in all_tickets_result.all()}

        # Filter to routable tickets (all deps Done)
        routable: list[Ticket] = []
        for ticket in routable_candidates:
            ticket_deps = deps_map.get(ticket.id, set())
            if all(state_map.get(dep_id) == "Done" for dep_id in ticket_deps):
                routable.append(ticket)

        if not routable:
            return None

        # Score by transitive dependents count (most dependents first)
        def _transitive_dependents_count(ticket_id: str) -> int:
            visited: set[str] = set()
            stack = [ticket_id]
            while stack:
                current = stack.pop()
                for dep in dependents_map.get(current, set()):
                    if dep not in visited:
                        visited.add(dep)
                        stack.append(dep)
            return len(visited)

        # Sort: most transitive dependents first, FIFO tiebreaker (already ordered by created_at)
        routable.sort(key=lambda t: _transitive_dependents_count(t.id), reverse=True)

        return routable[0].id


async def get_routable_issue_ids(
    db: Database, *, states: tuple[str, ...] = ("To Do",)
) -> list[int]:
    """Return distinct issue_id values from tickets in routable states, sorted ascending.

    Tickets with NULL issue_id are excluded.
    """
    async with db.session() as session:
        result = await session.execute(
            select(Ticket.issue_id)
            .where(Ticket.state.in_(states), Ticket.issue_id.isnot(None))
            .distinct()
            .order_by(Ticket.issue_id)
        )
        return [row[0] for row in result.all()]
