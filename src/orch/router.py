"""Router: polls for routable tickets and dispatches agents per lifecycle state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from orch.db import Database
from orch.hindsight import retain_human_intervention, retain_ticket_outcome
from orch.tickets import add_comment, get_next_routable, get_ticket, update_ticket

logger = logging.getLogger(__name__)

CreateWorktree = Callable[[str, str, Path], Awaitable[None]]
RunAgent = Callable[[str, Path, str], Awaitable[int]]
WebhookPost = Callable[..., Awaitable[int]]

WEBHOOK_TRIGGER_STATES = {"Needs Human Review", "Human Merge"}

AGENT_FOR_STATE: dict[str, str] = {
    "To Do": "coder",
    "Rework": "coder",
    "Code Review": "reviewer",
    "Ready to Merge": "merger",
}

ROUTABLE_STATES = tuple(AGENT_FOR_STATE.keys())
MAX_REWORK_LOOPS = 3


class Router:
    """Polls SQLite for routable tickets and dispatches the correct agent."""

    def __init__(
        self,
        db: Database,
        repo_root: Path,
        *,
        poll_interval: float = 10.0,
        create_worktree: CreateWorktree | None = None,
        run_agent: RunAgent | None = None,
        webhook_post: WebhookPost | None = None,
        webhook_url: str = "",
        hindsight_client: object | None = None,
        hindsight_bank_id: str = "",
    ) -> None:
        self._db = db
        self._repo_root = repo_root
        self._poll_interval = poll_interval
        self._create_worktree = create_worktree or self._default_create_worktree
        self._run_agent = run_agent or self._default_run_agent
        self._webhook_post = webhook_post
        self._webhook_url = webhook_url
        self._hindsight_client = hindsight_client
        self._hindsight_bank_id = hindsight_bank_id
        self._stop = asyncio.Event()

    async def poll_once(self) -> str | None:
        """Find next routable ticket (dependency-aware), dispatch it, return ticket ID or None."""
        ticket_id = await get_next_routable(self._db, states=ROUTABLE_STATES)

        if ticket_id is None:
            return None

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return None

        # Rework loop escalation
        if ticket.state == "Rework" and ticket.rework_loop_count >= MAX_REWORK_LOOPS:
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                f"Reached {ticket.rework_loop_count} rework loops."
                " Escalating to Needs Human Review.",
                author="router",
            )
            await self._fire_webhook(ticket_id, "Rework", "Needs Human Review")
            return ticket_id

        # Increment rework loop count when dispatching from Rework
        if ticket.state == "Rework":
            await update_ticket(
                self._db,
                ticket_id,
                rework_loop_count=ticket.rework_loop_count + 1,
            )

        agent_type = AGENT_FOR_STATE.get(ticket.state, "coder")
        await self._dispatch(ticket_id, agent_type)
        return ticket_id

    async def _dispatch(self, ticket_id: str, agent_type: str) -> None:
        """Move ticket to In Progress, create worktree, launch agent, handle result."""
        worktree_dir = self._repo_root / ".orchestra" / "worktrees" / ticket_id
        branch = f"ticket/{ticket_id}"

        await update_ticket(
            self._db,
            ticket_id,
            state="In Progress",
            worktree_path=str(worktree_dir),
        )

        await self._create_worktree(ticket_id, branch, worktree_dir)

        exit_code = await self._run_agent(ticket_id, worktree_dir, agent_type)

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        if ticket.state == "Done":
            await self._retain_outcome(ticket_id)
        elif ticket.state == "In Progress":
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                f"Agent exited with code {exit_code} without completing."
                " Moved to Needs Human Review.",
                author="router",
            )
            await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")

    async def retain_human_resolve(self, ticket_id: str, comment_body: str) -> None:
        """Retain a human intervention to Hindsight if configured."""
        if not self._hindsight_client or not self._hindsight_bank_id:
            return
        await retain_human_intervention(
            self._hindsight_client,
            self._db,
            ticket_id,
            comment_body=comment_body,
            bank_id=self._hindsight_bank_id,
        )

    async def _retain_outcome(self, ticket_id: str) -> None:
        """Retain ticket outcome to Hindsight if configured."""
        if not self._hindsight_client or not self._hindsight_bank_id:
            return
        await retain_ticket_outcome(
            self._hindsight_client,
            self._db,
            ticket_id,
            bank_id=self._hindsight_bank_id,
        )

    async def _fire_webhook(self, ticket_id: str, old_state: str, new_state: str) -> None:
        """Fire webhook if configured and new_state is a trigger state."""
        if not self._webhook_post or not self._webhook_url:
            return
        if new_state not in WEBHOOK_TRIGGER_STATES:
            return

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        payload = {
            "ticket_id": ticket_id,
            "title": ticket.title,
            "old_state": old_state,
            "new_state": new_state,
            "risk_score": ticket.risk_score,
            "linked_pr": ticket.linked_pr,
        }
        try:
            await self._webhook_post(self._webhook_url, payload, timeout=10.0)
        except Exception:
            logger.exception("Webhook failed for %s", ticket_id)

    async def run(self) -> None:
        """Enter the polling loop until stopped."""
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Error during poll cycle")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                break
            except TimeoutError:
                continue

    def stop(self) -> None:
        """Signal the router to stop after the current dispatch completes."""
        self._stop.set()

    @staticmethod
    async def _default_create_worktree(ticket_id: str, branch: str, worktree_dir: Path) -> None:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "add",
            str(worktree_dir),
            "-b",
            branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    def _db_path_abs(self) -> str:
        """Return the absolute path to the SQLite database."""
        return str(self._db.path.resolve())

    async def _default_run_agent(self, ticket_id: str, worktree_dir: Path, agent_type: str) -> int:
        import os

        env = {**os.environ, "ORCH_DB_PATH": self._db_path_abs()}
        proc = await asyncio.create_subprocess_exec(
            "opencode",
            "run",
            "--agent",
            agent_type,
            "--format",
            "json",
            "--dir",
            str(worktree_dir),
            f"Implement ticket {ticket_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        log_dir = worktree_dir.parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{ticket_id}.log"
        log_file.write_bytes(b"=== STDOUT ===\n" + stdout + b"\n=== STDERR ===\n" + stderr)

        return proc.returncode or 0
