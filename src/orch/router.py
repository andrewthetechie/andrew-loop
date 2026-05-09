"""Router: polls for routable tickets and dispatches agents per lifecycle state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from rich.console import Console

from orch.db import Database
from orch.tui import RouterTUI
from orch.hindsight import retain_human_intervention, retain_ticket_outcome
from orch.prompt import build_dispatch_prompt
from orch.tickets import (
    add_comment,
    get_next_routable,
    get_ticket,
    get_ticket_comments,
    ticket_to_dict,
    update_ticket,
)

logger = logging.getLogger(__name__)

CreateWorktree = Callable[[str, str, Path], Awaitable[None]]
RunAgent = Callable[[str, Path, str, str], Awaitable[int]]  # ticket_id, dir, agent_type, payload
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
_REVIEW_DECISION_PREFIX = "## REVIEW_DECISION:"


def _extract_review_decision(
    comments: list[object],
) -> tuple[str, str]:
    """Scan ticket comments (newest first) for a REVIEW_DECISION marker.

    Returns (decision, full_comment_body). Decision is one of:
    APPROVED, CHANGES_REQUESTED, NEEDS_HUMAN_REVIEW, or 'Not available'.
    """
    for comment in reversed(comments):
        body: str = getattr(comment, "body", "") or ""
        if body.startswith(_REVIEW_DECISION_PREFIX):
            first_line = body.splitlines()[0]
            decision = first_line.removeprefix(_REVIEW_DECISION_PREFIX).strip()
            return decision, body
    return "Not available", ""



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
        verbose: bool = False,
        manual_approval: bool = False,
        console: Console | None = None,
        tui: RouterTUI | None = None,
    ) -> None:
        self._db = db
        self._repo_root = repo_root
        self._poll_interval = poll_interval
        self._create_worktree: CreateWorktree = create_worktree or self._default_create_worktree
        self._run_agent = run_agent or self._default_run_agent
        self._webhook_post = webhook_post
        self._webhook_url = webhook_url
        self._hindsight_client = hindsight_client
        self._hindsight_bank_id = hindsight_bank_id
        self._verbose = verbose
        self._manual_approval = manual_approval
        self._console = console or Console()
        self._tui = tui
        self._stop = asyncio.Event()

    def _log(self, msg: str) -> None:
        if self._tui:
            self._tui.log(msg)
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self._console.print(f"[dim]{ts}[/dim] {msg}")

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
            self._log(
                f"[yellow]↑[/yellow] [bold]{ticket_id}[/bold] rework limit reached"
                f" ({ticket.rework_loop_count}/{MAX_REWORK_LOOPS}) → Needs Human Review"
            )
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

        if self._manual_approval and not await self._prompt_approval(ticket_id, agent_type, ticket):
            self.stop()
            return None

        await self._dispatch(ticket_id, agent_type)
        return ticket_id

    async def _dispatch(self, ticket_id: str, agent_type: str) -> None:
        """Move ticket to In Progress, create worktree, launch agent, handle result."""
        from orch.config import Config as _Config
        from orch.state import resolve_state_dir as _resolve_state_dir
        _cfg = _Config.load(repo_root=self._repo_root)
        _state_dir = _resolve_state_dir(self._repo_root, base_dir=_cfg.state.base_dir)
        worktree_dir = _state_dir / "worktrees" / ticket_id
        branch = f"ticket/{ticket_id}"

        await update_ticket(
            self._db,
            ticket_id,
            state="In Progress",
            worktree_path=str(worktree_dir),
        )

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        await self._create_worktree(ticket_id, branch, worktree_dir)

        if not worktree_dir.is_dir():
            logger.error("Worktree directory missing after creation attempt: %s", worktree_dir)
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db, ticket_id,
                f"Router could not create worktree at {worktree_dir}. Check git worktree state.",
                author="router",
            )
            return

        self._sync_opencode_to_worktree(worktree_dir)

        comments = await get_ticket_comments(self._db, ticket_id)
        comments_data = [{"author": c.author, "body": c.body} for c in comments]

        from orch.config import Config
        cfg = Config.load(repo_root=self._repo_root)

        import json as _json
        opencode_json = self._repo_root / "opencode.json"
        step_budget = 50
        model_name = ""
        if opencode_json.is_file():
            try:
                oc = _json.loads(opencode_json.read_text())
                agent_cfg = oc.get("agent", {}).get(agent_type, {})
                step_budget = agent_cfg.get("steps", 50)
                model_name = agent_cfg.get("model", "")
            except Exception:
                pass

        dispatch_config: dict[str, object] = {
            "validation_commands": cfg.validation.commands,
            "worktree_dir": str(worktree_dir),
            "step_budget": step_budget,
            "model": model_name,
        }

        # Pre-flight validation gate: run validators before dispatching a coder on new
        # (To Do) work. If the baseline is broken the coder can't distinguish its own
        # failures from pre-existing ones. Prompt user to fix before proceeding.
        if agent_type == "coder" and getattr(ticket, "state", "") == "In Progress":
            # ticket was "To Do" before we set it to In Progress above — check original
            pass  # state already flipped; check handled below via pre_state tracking

        # We need the original state before it was set to In Progress, so we track it
        # via the ticket's previous state. Since we just moved it, check the event log
        # instead — simpler: re-read from the dispatch payload context (ticket.state was
        # captured before update_ticket above). Actually we captured it in poll_once.
        # Simplest reliable approach: check if this is a To Do dispatch by looking at
        # whether the worktree had any prior commits (fresh = To Do, has commits = Rework).
        if agent_type == "coder" and cfg.validation.commands:
            pre_existing_commits = await self._worktree_has_commits(worktree_dir)
            if not pre_existing_commits:
                # Fresh worktree = To Do dispatch — validate the baseline
                val_ok = await self._check_baseline_validation(
                    ticket_id, worktree_dir, cfg.validation.commands
                )
                if not val_ok:
                    # Move back to To Do — the user needs to fix things first
                    await update_ticket(self._db, ticket_id, state="To Do")
                    await add_comment(
                        self._db, ticket_id,
                        "Router halted dispatch: pre-existing validation failures detected. "
                        "Fix the failing validators and re-queue the ticket.",
                        author="router",
                    )
                    return

        if agent_type == "merger":
            review_decision, review_comment = _extract_review_decision(comments)
            dispatch_config["review_decision"] = review_decision
            dispatch_config["review_comment"] = review_comment

        # Pre-run validation for reviewer and merger — deterministic, no LLM needed.
        if agent_type in ("reviewer", "merger") and cfg.validation.commands:
            val_results = await self._run_validation(worktree_dir, cfg.validation.commands)
            dispatch_config["validation_results"] = val_results
            if self._hindsight_client and self._hindsight_bank_id:
                from orch.hindsight import retain_validation_results
                await retain_validation_results(
                    self._hindsight_client,
                    ticket_id,
                    getattr(ticket, "title", ""),
                    validation_results=val_results,
                    bank_id=self._hindsight_bank_id,
                )

        if self._tui:
            total = await self._db.count_tickets()
            self._tui.set_dispatching(
                ticket_id, ticket.title, ticket.state, agent_type,
                total_tickets=total,
                model=str(model_name),
                step_budget=int(step_budget),
            )

        self._log(
            f"[cyan]→[/cyan] [bold]{ticket_id}[/bold] dispatching [cyan]{agent_type}[/cyan]"
            f"  [dim]{worktree_dir}[/dim]"
        )

        payload = build_dispatch_prompt(
            ticket_to_dict(ticket),
            agent_type,
            comments=comments_data,
            config=dispatch_config,
        )
        dispatch_file = worktree_dir / f"ORCH_DISPATCH_{ticket_id}.md"
        dispatch_file.write_text(payload)

        prompt = f"Read ORCH_DISPATCH_{ticket_id}.md in the current directory for your full dispatch payload, then begin."

        exit_code = await self._run_agent(ticket_id, worktree_dir, agent_type, prompt)

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        if self._tui:
            self._tui.set_ticket_state(ticket.state)

        if ticket.state == "Done":
            self._log(f"[green]✓[/green] [bold]{ticket_id}[/bold] done (exit {exit_code})")
            await self._retain_outcome(ticket_id)
        elif ticket.state == "In Progress":
            if exit_code == 0 and agent_type == "coder":
                next_state = "Rework"
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold] coder exited 0 without completing"
                    " → Rework (likely hit step limit)"
                )
            else:
                next_state = "Needs Human Review"
                self._log(
                    f"[red]✗[/red] [bold]{ticket_id}[/bold] agent exited {exit_code} without completing"
                    f" → {next_state}"
                )
            if self._tui:
                self._tui.set_ticket_state(next_state)
            await update_ticket(self._db, ticket_id, state=next_state)
            escalation_body = await self._build_escalation_comment(ticket_id, exit_code, agent_type)
            await add_comment(self._db, ticket_id, escalation_body, author="router")
            if next_state == "Needs Human Review":
                await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")
        else:
            self._log(
                f"[green]✓[/green] [bold]{ticket_id}[/bold] exit {exit_code} → [bold]{ticket.state}[/bold]"
            )

        if self._tui:
            self._tui.on_agent_done()

    async def _worktree_has_commits(self, worktree_dir: Path) -> bool:
        """Return True if the worktree branch has any commits beyond its base."""
        if not worktree_dir.is_dir():
            return False
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "origin/main..HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_dir),
        )
        stdout, _ = await proc.communicate()
        return bool(stdout.strip())

    async def _check_baseline_validation(
        self,
        ticket_id: str,
        worktree_dir: Path,
        commands: list[str],
    ) -> bool:
        """Run validation on a fresh To Do worktree before the coder starts.

        Returns True if all validators pass (safe to proceed).
        If any fail, pauses and prompts the user to fix the baseline.
        Returns False if the user decides not to proceed (ticket reverts to To Do).
        """
        import sys

        self._log(
            f"  [dim]baseline validation for {ticket_id} before coder dispatch…[/dim]"
        )
        results = await self._run_validation(worktree_dir, commands)
        failures = [r for r in results if not r.get("passed")]

        if not failures:
            self._log(f"  [green]✓[/green] baseline clean — proceeding with {ticket_id}")
            return True

        # Failures found — pause and prompt
        if self._tui:
            self._tui.pause()

        try:
            self._console.print()
            self._console.print(f"[red bold]Baseline validation failed for {ticket_id}[/red bold]")
            self._console.print(
                "The following validators are failing BEFORE the coder starts.\n"
                "Fix them so the coder has a clean baseline to work from.\n"
            )
            for r in failures:
                self._console.print(f"  [red]✗[/red] `{r['command']}`  exit={r.get('exit_code')}")
                output = (r.get("stdout", "") + "\n" + r.get("stderr", "")).strip()
                if output:
                    # Show last 10 lines of combined output
                    lines = output.splitlines()
                    for line in lines[-10:]:
                        self._console.print(f"    [dim]{line}[/dim]")
                self._console.print()

            while True:
                self._console.print(
                    "Fix the failures, then press [bold]Enter[/bold] to retry validation,\n"
                    "or type [bold]skip[/bold] to move the ticket to To Do and continue: ",
                    end="",
                )
                sys.stdout.flush()
                try:
                    answer = sys.stdin.readline().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return False

                if answer == "skip":
                    self._log(
                        f"[yellow]⚠[/yellow] {ticket_id} skipped — fix baseline validation and re-queue"
                    )
                    return False

                # Retry validation
                self._console.print("  Re-running validators…")
                results = await self._run_validation(worktree_dir, commands)
                failures = [r for r in results if not r.get("passed")]
                if not failures:
                    self._console.print(f"  [green]✓ All validators passing — dispatching {ticket_id}[/green]\n")
                    return True

                self._console.print(
                    f"  [red]Still {len(failures)} failing.[/red] Fix and press Enter again, or type 'skip':\n"
                )
        finally:
            if self._tui:
                self._tui.resume()

    async def _run_validation(
        self,
        worktree_dir: Path,
        commands: list[str],
    ) -> list[dict[str, object]]:
        """Run validation commands in the worktree and return structured results.

        stdout and stderr are captured separately so the dispatch payload can
        present them clearly to the agent. Plain-text format is used throughout
        to maximise readability for cost-optimised (Qwen-class) models.
        """
        import os
        import shlex

        results: list[dict[str, object]] = []
        if not commands:
            return results

        self._log(f"  [dim]running {len(commands)} validation command(s)…[/dim]")
        env = {**os.environ, "ORCH_DB_PATH": self._db_path_abs()}

        def _truncate(text: str, max_lines: int = 60) -> str:
            lines = text.splitlines()
            if len(lines) > max_lines:
                return f"... ({len(lines) - max_lines} lines truncated)\n" + "\n".join(lines[-max_lines:])
            return text

        for cmd in commands:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_dir),
                env=env,
            )
            raw_stdout, raw_stderr = await proc.communicate()
            exit_code = proc.returncode or 0
            passed = exit_code == 0

            results.append({
                "command": cmd,
                "exit_code": exit_code,
                "passed": passed,
                "stdout": _truncate(raw_stdout.decode(errors="replace").strip()),
                "stderr": _truncate(raw_stderr.decode(errors="replace").strip()),
            })
            status = "[green]✓[/green]" if passed else "[red]✗[/red]"
            self._log(f"  {status} [dim]{cmd}[/dim]  exit={exit_code}")

        return results

    async def _build_escalation_comment(self, ticket_id: str, exit_code: int, agent_type: str) -> str:
        """Build an informative NHR escalation comment summarising what happened."""
        comments = await get_ticket_comments(self._db, ticket_id)
        ticket = await get_ticket(self._db, ticket_id)
        rework_count = getattr(ticket, "rework_loop_count", 0) if ticket else 0

        lines = [
            f"## Needs Human Review — escalated by router",
            f"",
            f"**Agent:** {agent_type}  |  **Exit code:** {exit_code}  |  **Rework loops:** {rework_count}",
            f"",
            f"**Reason:** The agent exited without moving this ticket to the expected next state.",
        ]

        # Find the most recent non-router comment (reviewer/coder findings)
        review_comment = next(
            (c for c in reversed(comments) if c.author not in ("router",)), None
        )
        if review_comment:
            preview = review_comment.body[:600]
            if len(review_comment.body) > 600:
                preview += "\n... *(truncated — see full comment above)*"
            lines += [
                f"",
                f"**Last agent comment** (from {review_comment.author}):",
                f"",
                preview,
            ]

        lines += [
            f"",
            f"**To resume:** move this ticket to `Rework` once the issues above are addressed.",
        ]
        return "\n".join(lines)

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

    async def _prompt_approval(self, ticket_id: str, agent_type: str, ticket: object) -> bool:
        """Print a dispatch summary and ask the user to approve. Returns False → stop router."""
        import sys

        title = getattr(ticket, "title", "")
        state = getattr(ticket, "state", "")

        # Pause TUI before ANY output so console.print isn't swallowed by the Live display
        if self._tui:
            self._tui.pause()

        try:
            self._console.print()
            self._console.print("[dim]" + "─" * 60 + "[/dim]")
            self._console.print(f"  [bold]ticket[/bold]  {ticket_id}: {title}")
            self._console.print(f"  [bold]state[/bold]   {state}")
            self._console.print(f"  [bold]agent[/bold]   [cyan]{agent_type}[/cyan]")
            self._console.print("[dim]" + "─" * 60 + "[/dim]")
            self._console.print("  Proceed? [Y/n] ", end="")
            # Use sys.stdin directly so the read happens after Rich has flushed output
            sys.stdout.flush()
            try:
                answer = sys.stdin.readline().strip().lower()
                return answer in ("", "y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False
        finally:
            if self._tui:
                self._tui.resume()

    async def run(self) -> None:
        """Enter the polling loop until stopped."""
        idle_cycles = 0
        while not self._stop.is_set():
            try:
                dispatched = await self.poll_once()
                if dispatched:
                    # Something was dispatched — re-poll immediately so the next
                    # stage (e.g. merger after reviewer approves) starts without
                    # waiting a full poll interval.
                    idle_cycles = 0
                    continue
                else:
                    idle_cycles += 1
                    # Print a heartbeat every 6 idle cycles (~1 min at default interval)
                    if idle_cycles % 6 == 1:
                        self._log("[dim]polling — no routable tickets[/dim]")
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

    def _sync_opencode_to_worktree(self, worktree_dir: Path) -> None:
        """Copy opencode.json, prompts, and tools fresh into the worktree.

        Called on every dispatch so agents always run with the latest config.
        Avoids symlinks, which opencode's --dir sandbox rejects as external_directory.
        No-op if the worktree directory doesn't exist (git worktree add may have failed).
        """
        import shutil

        if not worktree_dir.is_dir():
            logger.debug("Skipping opencode sync — worktree dir missing: %s", worktree_dir)
            return

        src_opencode = self._repo_root / ".opencode"
        dst_opencode = worktree_dir / ".opencode"

        # opencode.json
        src_json = self._repo_root / "opencode.json"
        if src_json.is_file():
            dst_json = worktree_dir / "opencode.json"
            if dst_json.is_symlink():
                dst_json.unlink()
            shutil.copy2(src_json, dst_json)

        if not src_opencode.is_dir():
            return

        # Remove old symlink if present (legacy)
        if dst_opencode.is_symlink():
            dst_opencode.unlink()
        dst_opencode.mkdir(exist_ok=True)

        # Prompts — copy fresh every dispatch
        src_prompts = src_opencode / "prompts"
        if src_prompts.is_dir():
            dst_prompts = dst_opencode / "prompts"
            dst_prompts.mkdir(exist_ok=True)
            for f in src_prompts.glob("*.md"):
                shutil.copy2(f, dst_prompts / f.name)

        # Tools — copy fresh every dispatch
        src_tools = src_opencode / "tools"
        if src_tools.is_dir():
            dst_tools = dst_opencode / "tools"
            dst_tools.mkdir(exist_ok=True)
            for f in src_tools.glob("*.ts"):
                shutil.copy2(f, dst_tools / f.name)

        # node_modules — symlink once (large, doesn't change per dispatch)
        src_nm = src_opencode / "node_modules"
        dst_nm = dst_opencode / "node_modules"
        if src_nm.is_dir() and not dst_nm.exists():
            dst_nm.symlink_to(src_nm)

        # package.json — copy once if missing
        src_pkg = src_opencode / "package.json"
        dst_pkg = dst_opencode / "package.json"
        if src_pkg.is_file() and not dst_pkg.exists():
            shutil.copy2(src_pkg, dst_pkg)

    async def _default_create_worktree(self, ticket_id: str, branch: str, worktree_dir: Path) -> None:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        if worktree_dir.is_dir():
            # Worktree already exists (re-dispatch) — nothing to do
            return

        # Fetch origin so we always branch from the latest merged state.
        # This prevents merge conflicts caused by PRs merged since the last fetch.
        fetch = await asyncio.create_subprocess_exec(
            "git", "fetch", "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        await fetch.communicate()

        # Create the worktree branching from origin/main (not local HEAD)
        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", str(worktree_dir), "-b", branch, "origin/main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Branch already exists — check it out instead
            logger.debug(
                "git worktree add -b failed (%s), retrying with existing branch: %s",
                stderr.decode(errors="replace").strip(),
                branch,
            )
            proc2 = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", str(worktree_dir), branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_root),
            )
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0:
                logger.error(
                    "git worktree add failed for %s: %s",
                    branch,
                    stderr2.decode(errors="replace").strip(),
                )
        # _sync_opencode_to_worktree is called by _dispatch after this returns
        # _sync_opencode_to_worktree is called by _dispatch after this returns
        # _sync_opencode_to_worktree is called by _dispatch after this returns

    def _db_path_abs(self) -> str:
        """Return the absolute path to the SQLite database."""
        return str(self._db.path.resolve())

    async def _default_run_agent(self, ticket_id: str, worktree_dir: Path, agent_type: str, payload: str) -> int:
        import os

        log_dir = worktree_dir.parent.parent / "logs"  # state_dir/logs/
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{ticket_id}.log"

        env = {**os.environ, "ORCH_DB_PATH": self._db_path_abs()}
        # limit=8MB: opencode emits single-line JSON that can exceed the 64KB default
        proc = await asyncio.create_subprocess_exec(
            "opencode",
            "run",
            "--agent",
            agent_type,
            "--format",
            "json",
            "--dir",
            str(worktree_dir),
            payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=2**23,  # 8 MB per line
        )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _read_stream(stream: asyncio.StreamReader, buf: list[bytes], label: str) -> None:
            while True:
                try:
                    line = await stream.readline()
                except asyncio.LimitOverrunError:
                    # Line exceeds buffer — read the rest and treat as one chunk
                    line = await stream.read(2**23)
                if not line:
                    break
                buf.append(line)
                if self._tui:
                    if label == "out":
                        self._tui.parse_agent_line(ticket_id, line)
                    elif line.strip():
                        self._tui.log(f"  [dim]{line.decode(errors='replace').rstrip()}[/dim]")
                elif self._verbose:
                    self._console.print(
                        f"  [dim]{ticket_id}[/dim] [dim]{label}[/dim] {line.decode(errors='replace').rstrip()}"
                    )

        await asyncio.gather(
            _read_stream(proc.stdout, stdout_chunks, "out"),  # type: ignore[arg-type]
            _read_stream(proc.stderr, stderr_chunks, "err"),  # type: ignore[arg-type]
        )
        await proc.wait()

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)
        log_file.write_bytes(b"=== STDOUT ===\n" + stdout + b"\n=== STDERR ===\n" + stderr)
        self._log(f"  [dim]{ticket_id} log → {log_file}[/dim]")

        return proc.returncode or 0
