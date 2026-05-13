"""Deterministic PR creation and update commands.

These wrap `gh` CLI operations so that agents call `orch pr create/update`
instead of reasoning about git and gh flags directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from orch.db import Database
from orch.router import feature_branch_name
from orch.tickets import get_ticket, update_ticket

RunCmd = Callable[[list[str], Path | None], Awaitable[tuple[int, str, str]]]


async def _default_run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return (proc.returncode or 0, stdout.decode(), stderr.decode())


async def create_pr(
    db: Database,
    ticket_id: str,
    *,
    base_branch: str | None = None,
    run_cmd: RunCmd | None = None,
    cwd: Path | None = None,
) -> str:
    """Create a PR for a ticket using gh CLI. Returns the PR URL.

    When base_branch is not specified, it is derived from the ticket's issue_id:
    issue-{N} if set, 'main' otherwise.
    """
    run = run_cmd or _default_run_cmd
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        msg = f"Ticket '{ticket_id}' not found."
        raise ValueError(msg)

    if base_branch is None:
        base_branch = feature_branch_name(ticket.issue_id)

    # Derive the head branch name from the ticket's worktree branch convention.
    issue_slug = f"issue-{ticket.issue_id}" if ticket.issue_id is not None else "no-issue"
    head_branch = f"ticket/{issue_slug}/{ticket_id}"

    # Push the ticket branch to origin so it exists on remote before PR creation.
    push_rc, _push_out, push_err = await run(
        ["git", "push", "-u", "origin", f"HEAD:{head_branch}"], cwd
    )
    if push_rc != 0:
        msg = f"git push failed (exit {push_rc}): {push_err}"
        raise RuntimeError(msg)

    title = f"feat({ticket_id}): {ticket.title}"
    body = (
        f"## {ticket.title}\n\n"
        f"{ticket.description}\n\n"
        f"### Acceptance Criteria\n\n{ticket.acceptance_criteria}\n\n"
        f"---\nTicket: `{ticket_id}`"
    )

    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--base",
        base_branch,
        "--head",
        head_branch,
    ]

    returncode, stdout, stderr = await run(cmd, cwd)
    if returncode != 0:
        msg = f"gh pr create failed (exit {returncode}): {stderr}"
        raise RuntimeError(msg)

    pr_url = stdout.strip()
    await update_ticket(db, ticket_id, linked_pr=pr_url)
    return pr_url


async def update_pr(
    db: Database,
    ticket_id: str,
    *,
    run_cmd: RunCmd | None = None,
    cwd: Path | None = None,
) -> None:
    """Push current branch to update existing PR for a ticket."""
    run = run_cmd or _default_run_cmd
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        msg = f"Ticket '{ticket_id}' not found."
        raise ValueError(msg)

    if not ticket.linked_pr:
        msg = f"Ticket '{ticket_id}' has no linked PR."
        raise ValueError(msg)

    # Push explicitly to the ticket branch to avoid upstream mismatches.
    issue_slug = f"issue-{ticket.issue_id}" if ticket.issue_id is not None else "no-issue"
    head_branch = f"ticket/{issue_slug}/{ticket_id}"

    returncode, _stdout, stderr = await run(["git", "push", "origin", f"HEAD:{head_branch}"], cwd)
    if returncode != 0:
        msg = f"git push failed (exit {returncode}): {stderr}"
        raise RuntimeError(msg)
