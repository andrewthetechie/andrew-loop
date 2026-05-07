"""Deterministic PR creation and update commands.

These wrap `gh` CLI operations so that agents call `orch pr create/update`
instead of reasoning about git and gh flags directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from orch.db import Database
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
    base_branch: str = "main",
    run_cmd: RunCmd | None = None,
    cwd: Path | None = None,
) -> str:
    """Create a PR for a ticket using gh CLI. Returns the PR URL."""
    run = run_cmd or _default_run_cmd
    ticket = await get_ticket(db, ticket_id)
    if ticket is None:
        msg = f"Ticket '{ticket_id}' not found."
        raise ValueError(msg)

    title = f"ticket/{ticket_id}: {ticket.title}"
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

    returncode, _stdout, stderr = await run(["git", "push"], cwd)
    if returncode != 0:
        msg = f"git push failed (exit {returncode}): {stderr}"
        raise RuntimeError(msg)
