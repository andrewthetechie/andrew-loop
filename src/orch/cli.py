"""CLI entrypoint for the orch orchestrator."""

import asyncio
import json
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from orch.db import VALID_STATES, Database
from orch.doctor import run_doctor
from orch.init import default_runner, init_project
from orch.pr import create_pr, update_pr
from orch.router import Router
from orch.tickets import (
    add_comment,
    add_dependencies,
    create_ticket,
    edit_ticket_from_yaml,
    get_dependencies,
    get_dependents,
    get_ticket,
    get_ticket_comments,
    import_tickets,
    list_events,
    list_tickets,
    move_ticket,
    parse_ticket_yaml,
    promote_tickets,
    ticket_to_dict,
    ticket_to_yaml,
    update_ticket,
)

console = Console()

DEFAULT_DB_PATH = ".orchestra/state.db"

TICKET_TEMPLATE = """\
# Fill in the ticket fields below. Lines starting with # are ignored.
# Required fields: title, description, acceptance_criteria

title: ""
description: |
  Describe what needs to be done.
acceptance_criteria: |
  - [ ] First criterion
file_paths: null
test_expectations: null
risk_score: null
"""


def _run(coro: object) -> object:
    """Run an async coroutine from sync Click context."""
    return asyncio.run(coro)


def _get_db(ctx: click.Context) -> Database:
    """Get the Database instance from Click context."""
    return ctx.obj["db"]


def _strip_yaml_comments(content: str) -> str:
    """Remove lines starting with # from YAML content."""
    lines = content.splitlines()
    filtered = [line for line in lines if not line.lstrip().startswith("#")]
    return "\n".join(filtered)


@click.group()
@click.option(
    "--db",
    "db_path",
    default=DEFAULT_DB_PATH,
    envvar="ORCH_DB_PATH",
    help="Path to the SQLite database.",
)
@click.pass_context
def main(ctx: click.Context, db_path: str) -> None:
    """orch — deterministic orchestrator for the agent developer workflow."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@main.command()
@click.option("--dir", "target_dir", default=".", help="Target repository directory.")
@click.option("--no-externals", is_flag=True, help="Skip external tool setup.")
def init(target_dir: str, no_externals: bool) -> None:
    """Initialize a repository for the agent workflow."""
    repo_root = Path(target_dir).resolve()

    async def _init() -> None:
        runner = None if no_externals else default_runner
        result = await init_project(repo_root, run_external=runner)
        for field in ("config", "db", "agents", "hindsight", "serena", "gitnexus"):
            status = getattr(result, f"{field}_status")
            console.print(f"  {field}: {status}")

    _run(_init())


@main.command()
@click.option("--dir", "target_dir", default=".", help="Target repository directory.")
@click.option("--no-tools", is_flag=True, help="Skip CLI tool checks.")
def doctor(target_dir: str, no_tools: bool) -> None:
    """Check that the environment is ready for the agent workflow."""
    repo_root = Path(target_dir).resolve()

    async def _doctor() -> None:
        runner = None if no_tools else default_runner
        result = await run_doctor(repo_root, tool_runner=runner)
        for check in result.checks:
            mark = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
            console.print(f"  {mark}  {check.name}: {check.message}")
        if not result.healthy:
            sys.exit(1)

    _run(_doctor())


@main.group()
@click.pass_context
def tickets(ctx: click.Context) -> None:
    """Manage tickets."""


@tickets.command()
@click.option("--from-file", "from_file", type=click.Path(exists=True), help="YAML ticket file.")
@click.option("--title", help="Ticket title (for quick creation).")
@click.option("--depends-on", "depends_on", multiple=True, help="Ticket ID this depends on.")
@click.pass_context
def create(
    ctx: click.Context, from_file: str | None, title: str | None, depends_on: tuple[str, ...]
) -> None:
    """Create a new ticket."""
    strict = True
    if from_file:
        content = Path(from_file).read_text()
    elif title:
        # Quick mode: minimal ticket from flags, relaxed validation
        content = f"title: {title}\ndescription: ''\nacceptance_criteria: ''"
        strict = False
    else:
        # Editor mode
        content = click.edit(TICKET_TEMPLATE)
        if content is None:
            click.echo("Aborted — no content saved.", err=True)
            sys.exit(1)
        content = _strip_yaml_comments(content)

    try:
        data = parse_ticket_yaml(content, strict=strict)
    except (ValueError, TypeError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    async def _create() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            ticket = await create_ticket(db, data)
            if depends_on:
                try:
                    await add_dependencies(db, ticket.id, list(depends_on))
                except ValueError as e:
                    click.echo(f"Error: {e}", err=True)
                    sys.exit(1)
            click.echo(f"Created {ticket.id}")

    _run(_create())


@tickets.command("list")
@click.option("--state", "state_filter", help="Filter by ticket state.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def list_cmd(ctx: click.Context, state_filter: str | None, as_json: bool) -> None:
    """List tickets."""

    async def _list() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            items = await list_tickets(db, state_filter=state_filter)

            if as_json:
                click.echo(json.dumps([ticket_to_dict(t) for t in items], indent=2))
                return

            if not items:
                click.echo("No tickets found.")
                return

            table = Table(show_header=True, header_style="bold")
            table.add_column("ID")
            table.add_column("Title")
            table.add_column("State")
            table.add_column("Risk")
            table.add_column("PR")
            for t in items:
                table.add_row(
                    t.id,
                    t.title,
                    t.state,
                    str(t.risk_score) if t.risk_score else "-",
                    t.linked_pr or "-",
                )
            console.print(table)

    _run(_list())


@tickets.command()
@click.argument("ticket_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--yaml", "as_yaml", is_flag=True, help="Output as round-trippable YAML.")
@click.pass_context
def show(ctx: click.Context, ticket_id: str, as_json: bool, as_yaml: bool) -> None:
    """Show ticket details."""

    async def _show() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            ticket = await get_ticket(db, ticket_id)
            if ticket is None:
                click.echo(f"Error: Ticket '{ticket_id}' not found.", err=True)
                sys.exit(1)

            if as_yaml:
                click.echo(ticket_to_yaml(ticket))
                return

            comments = await get_ticket_comments(db, ticket_id)
            deps = await get_dependencies(db, ticket_id)
            blocks = await get_dependents(db, ticket_id)
            data = ticket_to_dict(ticket, comments=comments)
            data["depends_on"] = deps
            data["blocks"] = blocks

            if as_json:
                click.echo(json.dumps(data, indent=2))
                return

            # Human-readable output
            console.print(f"[bold]{data['id']}[/bold]: {data['title']}")
            console.print(f"State: {data['state']}")
            console.print(f"Risk: {data['risk_score'] or '-'}")
            console.print(f"PR: {data['linked_pr'] or '-'}")
            if deps:
                console.print(f"Depends on: {', '.join(deps)}")
            if blocks:
                console.print(f"Blocks: {', '.join(blocks)}")
            console.print(f"\n[bold]Description[/bold]\n{data['description']}")
            console.print(f"\n[bold]Acceptance Criteria[/bold]\n{data['acceptance_criteria']}")
            if data["file_paths"]:
                console.print(f"\n[bold]File Paths[/bold]\n{data['file_paths']}")
            if data["test_expectations"]:
                console.print(f"\n[bold]Test Expectations[/bold]\n{data['test_expectations']}")
            if comments:
                console.print("\n[bold]Comments[/bold]")
                for c in data["comments"]:
                    console.print(f"  [{c['created_at']}] {c['author']}: {c['body']}")

    _run(_show())


@tickets.command()
@click.argument("ticket_id")
@click.argument("new_state")
@click.pass_context
def move(ctx: click.Context, ticket_id: str, new_state: str) -> None:
    """Move a ticket to a new state."""

    async def _move() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                ticket = await move_ticket(db, ticket_id, new_state)
                click.echo(f"{ticket.id} → {ticket.state}")
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_move())


@tickets.command()
@click.argument("ticket_id")
@click.option("--state", help="New state.")
@click.option("--linked-pr", help="PR URL.")
@click.option("--assignee", help="Assignee.")
@click.pass_context
def update(
    ctx: click.Context,
    ticket_id: str,
    state: str | None,
    linked_pr: str | None,
    assignee: str | None,
) -> None:
    """Update specific fields on a ticket."""

    async def _update() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                fields = {}
                if state is not None:
                    fields["state"] = state
                if linked_pr is not None:
                    fields["linked_pr"] = linked_pr
                if assignee is not None:
                    fields["assignee"] = assignee
                ticket = await update_ticket(db, ticket_id, **fields)
                click.echo(f"Updated {ticket.id}")
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_update())


@tickets.command()
@click.argument("ticket_id")
@click.option("--from-file", "from_file", type=click.Path(exists=True), help="YAML file.")
@click.pass_context
def edit(ctx: click.Context, ticket_id: str, from_file: str | None) -> None:
    """Edit a ticket's content."""
    file_content = Path(from_file).read_text() if from_file else None

    async def _edit() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            ticket = await get_ticket(db, ticket_id)
            if ticket is None:
                click.echo(f"Error: Ticket '{ticket_id}' not found.", err=True)
                sys.exit(1)

            if file_content is not None:
                content = file_content
            else:
                current_yaml = ticket_to_yaml(ticket)
                content = click.edit(current_yaml)
                if content is None:
                    click.echo("Aborted — no changes saved.", err=True)
                    sys.exit(1)

            try:
                updated = await edit_ticket_from_yaml(db, ticket_id, content)
                click.echo(f"Updated {updated.id}")
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_edit())


@tickets.command()
@click.argument("ticket_id")
@click.argument("body")
@click.option("--author", default="human", help="Comment author (default: human).")
@click.pass_context
def comment(ctx: click.Context, ticket_id: str, body: str, author: str) -> None:
    """Add a comment to a ticket."""

    async def _comment() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                await add_comment(db, ticket_id, body, author=author)
                click.echo(f"Comment added to {ticket_id}")
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_comment())


@tickets.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.pass_context
def import_cmd(ctx: click.Context, file: str) -> None:
    """Import tickets from a YAML file containing a list of ticket definitions."""
    content = Path(file).read_text()
    data = yaml.safe_load(content)
    if not isinstance(data, list):
        click.echo("Error: Import file must contain a YAML list of tickets.", err=True)
        sys.exit(1)

    async def _import() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                created = await import_tickets(db, data)
                ids = ", ".join(t.id for t in created)
                count = len(created)
                click.echo(f"Created {count} ticket{'s' if count != 1 else ''}: {ids}")
            except (ValueError, TypeError) as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_import())


@tickets.command()
@click.option("--ticket-id", "ticket_id", help="Promote a specific ticket.")
@click.pass_context
def promote(ctx: click.Context, ticket_id: str | None) -> None:
    """Promote Draft tickets to To Do (validates required fields)."""

    async def _promote() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                promoted = await promote_tickets(db, ticket_id=ticket_id)
                if not promoted:
                    click.echo("No Draft tickets to promote.")
                    return
                ids = ", ".join(t.id for t in promoted)
                count = len(promoted)
                click.echo(f"Promoted {count} ticket{'s' if count != 1 else ''} to To Do: {ids}")
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_promote())


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show workflow status dashboard."""

    async def _status() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            items = await list_tickets(db)

            if as_json:
                click.echo(json.dumps([ticket_to_dict(t) for t in items], indent=2))
                return

            if not items:
                click.echo("No tickets found.")
                return

            # Sort by state for grouping
            state_order = {s: i for i, s in enumerate(VALID_STATES)}
            items.sort(key=lambda t: (state_order.get(t.state, 999), t.created_at))

            table = Table(show_header=True, header_style="bold", title="Workflow Status")
            table.add_column("ID")
            table.add_column("Title")
            table.add_column("State")
            table.add_column("Risk")
            table.add_column("PR")
            table.add_column("Assignee")
            for t in items:
                table.add_row(
                    t.id,
                    t.title,
                    t.state,
                    str(t.risk_score) if t.risk_score else "-",
                    t.linked_pr or "-",
                    t.assignee or "-",
                )
            console.print(table)

    _run(_status())


@main.command("log")
@click.option("-n", "count", type=int, default=None, help="Show last N events.")
@click.option("-f", "follow", is_flag=True, help="Follow the event log in real time.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--interval", default=2.0, type=float, help="Poll interval for -f mode.")
@click.pass_context
def log_cmd(
    ctx: click.Context, count: int | None, follow: bool, as_json: bool, interval: float
) -> None:
    """Display the event log."""

    async def _log() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            if follow:
                last_id = 0
                import asyncio

                try:
                    while True:
                        events = await list_events(db)
                        new_events = [e for e in events if e.id > last_id]
                        if new_events:
                            _print_events(new_events, as_json=as_json)
                            last_id = new_events[-1].id
                        await asyncio.sleep(interval)
                except KeyboardInterrupt:
                    pass
            else:
                events = await list_events(db, limit=count)
                _print_events(events, as_json=as_json)

    _run(_log())


def _print_events(events: list, *, as_json: bool = False) -> None:
    """Print event rows to stdout."""
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "id": e.id,
                        "ticket_id": e.ticket_id,
                        "timestamp": e.timestamp,
                        "actor": e.actor,
                        "old_state": e.old_state,
                        "new_state": e.new_state,
                    }
                    for e in events
                ],
                indent=2,
            )
        )
        return
    for e in events:
        console.print(f"{e.timestamp}  {e.ticket_id}  {e.actor}: {e.old_state} → {e.new_state}")


@main.group()
@click.pass_context
def pr(ctx: click.Context) -> None:
    """PR commands."""


@pr.command("create")
@click.argument("ticket_id")
@click.option("--base", "base_branch", default="main", help="Base branch for the PR.")
@click.pass_context
def pr_create(ctx: click.Context, ticket_id: str, base_branch: str) -> None:
    """Create a PR for a ticket."""

    async def _pr_create() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                url = await create_pr(db, ticket_id, base_branch=base_branch)
                click.echo(url)
            except (ValueError, RuntimeError) as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_pr_create())


@pr.command("update")
@click.argument("ticket_id")
@click.pass_context
def pr_update(ctx: click.Context, ticket_id: str) -> None:
    """Push updates for an existing PR."""

    async def _pr_update() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            try:
                await update_pr(db, ticket_id)
                click.echo(f"Pushed updates for {ticket_id}")
            except (ValueError, RuntimeError) as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)

    _run(_pr_update())


@main.group()
@click.pass_context
def router(ctx: click.Context) -> None:
    """Router commands."""


@router.command()
@click.option("--interval", default=10.0, type=float, help="Poll interval in seconds.")
@click.pass_context
def start(ctx: click.Context, interval: float) -> None:
    """Start the router polling loop."""
    import signal

    async def _start() -> None:
        db_path = ctx.obj["db_path"]
        repo_root = Path.cwd()
        async with Database(Path(db_path)) as db:
            r = Router(db, repo_root, poll_interval=interval)

            try:
                loop = asyncio.get_event_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, r.stop)
            except NotImplementedError:
                pass

            console.print(f"[bold]Router started[/bold] (poll interval: {interval}s)")
            await r.run()
            console.print("[bold]Router stopped.[/bold]")

    _run(_start())


# ── Worktrees ───────────────────────────────────────────────────────


@main.group()
@click.pass_context
def worktrees(ctx: click.Context) -> None:
    """Worktree management commands."""


@worktrees.command()
@click.option("--yes", "skip_confirm", is_flag=True, help="Skip confirmation prompt.")
@click.option("--dir", "repo_dir", default=".", help="Repository root directory.")
@click.pass_context
def prune(ctx: click.Context, skip_confirm: bool, repo_dir: str) -> None:
    """Prune worktrees for completed (Done) tickets."""
    from orch.worktrees import prune_worktrees as _prune_worktrees

    repo_root = Path(repo_dir).resolve()

    async def _prune() -> None:
        db_path = ctx.obj["db_path"]
        async with Database(Path(db_path)) as db:
            from orch.worktrees import list_worktrees as _list_worktrees

            infos = await _list_worktrees(db, repo_root)
            if not infos:
                click.echo("No worktrees found.")
                return

            done_count = sum(1 for i in infos if i.state == "Done")
            if done_count == 0:
                click.echo(f"No Done worktrees to prune ({len(infos)} skipped).")
                return

            if not skip_confirm:
                click.confirm(f"Prune {done_count} Done worktree(s)?", abort=True)

            result = await _prune_worktrees(db, repo_root)
            click.echo(f"Pruned {result.pruned}, {result.skipped} skipped")

    _run(_prune())
