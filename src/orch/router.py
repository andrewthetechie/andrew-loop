"""Router: polls for routable tickets and dispatches agents per lifecycle state."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from rich.console import Console

from orch.config import Config
from orch.db import Database, Event
from orch.hindsight import retain_human_intervention, retain_ticket_outcome
from orch.prompt import build_dispatch_prompt
from orch.state import resolve_state_dir
from orch.tickets import (
    add_comment,
    get_dependencies,
    get_next_routable,
    get_routable_issue_ids,
    get_ticket,
    get_ticket_comments,
    list_tickets,
    ticket_to_dict,
    update_ticket,
)
from orch.tui import RouterTUI
from orch.webhook import fire_webhook

logger = logging.getLogger(__name__)

CreateWorktree = Callable[[str, str, Path, str], Awaitable[None]]
RunAgent = Callable[[str, Path, str, str], Awaitable[int]]  # ticket_id, dir, agent_type, payload
WebhookPost = Callable[..., Awaitable[int]]

AGENT_FOR_STATE: dict[str, str] = {
    "To Do": "coder",
    "Rework": "coder",
    "Code Review": "reviewer",
    "Ready to Merge": "merger",
}

ROUTABLE_STATES = tuple(AGENT_FOR_STATE.keys())


def feature_branch_name(issue_id: int | None) -> str:
    """Derive the feature branch name from an issue ID.

    Returns 'issue-N' when issue_id is set, 'main' otherwise.
    """
    if issue_id is not None:
        return f"issue-{issue_id}"
    return "main"


MAX_REWORK_LOOPS = 3
_REVIEW_DECISION_PREFIX = "## REVIEW_DECISION:"
_PENDING_CHECK_STATES = {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
_FAILING_CHECK_STATES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}


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


def _extract_current_review_decision(
    comments: list[object],
) -> tuple[str, str]:
    """Like _extract_review_decision but ignores decisions from prior review cycles.

    A REVIEW_DECISION comment is considered stale if a coder comment was written
    after it — meaning the coder worked since that review, so the decision no longer
    reflects the current state of the PR.

    When the latest REVIEW_DECISION predates the latest coder comment, the reviewer
    moved the ticket to Ready to Merge without writing a new REVIEW_DECISION comment
    (implicit approval). In that case the gate should allow through rather than block
    on the old decision.

    Returns (decision, full_comment_body) where decision is one of:
    APPROVED, CHANGES_REQUESTED, NEEDS_HUMAN_REVIEW, or 'Not available'.
    """
    last_coder_idx = -1
    last_rd_idx = -1
    review_decision = "Not available"
    review_body = ""

    for i, comment in enumerate(comments):
        author: str = getattr(comment, "author", "") or ""
        body: str = getattr(comment, "body", "") or ""
        if author == "coder":
            last_coder_idx = i
        if body.startswith(_REVIEW_DECISION_PREFIX):
            last_rd_idx = i
            first_line = body.splitlines()[0]
            review_decision = first_line.removeprefix(_REVIEW_DECISION_PREFIX).strip()
            review_body = body

    # If the last REVIEW_DECISION predates the last coder comment, it is stale.
    # The reviewer approved implicitly by moving the ticket to Ready to Merge.
    if last_rd_idx >= 0 and last_coder_idx > last_rd_idx:
        return "APPROVED", ""

    return review_decision, review_body


def _classify_status_check_rollup(rollup: list[object]) -> str:
    """Classify GitHub statusCheckRollup data as pending, failed, or passed."""
    latest_by_check: dict[object, dict[str, object]] = {}

    for index, item in enumerate(rollup):
        if not isinstance(item, dict):
            continue
        key = (
            item.get("name")
            or item.get("context")
            or item.get("checkName")
            or item.get("displayName")
            or ("__unknown__", index)
        )
        latest_by_check[key] = item

    saw_failure = False

    for item in latest_by_check.values():
        values = {
            str(value).upper()
            for key in ("conclusion", "state", "status")
            if (value := item.get(key)) is not None
        }
        if values & _PENDING_CHECK_STATES:
            return "pending"
        if values & _FAILING_CHECK_STATES:
            saw_failure = True

    if saw_failure:
        return "failed"
    return "passed"


def _sum_step_finish_total_tokens(output: bytes) -> int:
    """Sum `tokens.total` across all step_finish events in opencode stdout."""
    total = 0
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "step_finish":
            continue
        part = data.get("part", {})
        tokens = part.get("tokens", {})
        value = tokens.get("total", 0)
        if isinstance(value, int):
            total += value
    return total


class Router:
    """Polls SQLite for routable tickets and dispatches the correct agent."""

    def __init__(
        self,
        db: Database,
        repo_root: Path,
        *,
        poll_interval: float = 10.0,
        issue_id: int | None = None,
        create_worktree: CreateWorktree | None = None,
        run_agent: RunAgent | None = None,
        webhook_post: WebhookPost | None = None,
        webhook_url: str = "",
        hindsight_client: object | None = None,
        hindsight_bank_id: str = "",
        all_issues: bool = False,
        verbose: bool = False,
        manual_approval: bool = False,
        console: Console | None = None,
        tui: RouterTUI | None = None,
        config: Config | None = None,
    ) -> None:
        self._db = db
        self._repo_root = repo_root
        self._config = config or Config.load(repo_root=repo_root)
        self._state_dir = resolve_state_dir(repo_root, base_dir=self._config.state.base_dir)
        self._poll_interval = poll_interval
        self._issue_id = issue_id
        self._all_issues = all_issues
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
        self._last_dispatch_tokens = 0

    async def validate_feature_branch(self) -> None:
        """Verify origin/issue-{N} exists when issue_id is set. Raises SystemExit if missing."""
        if self._issue_id is None:
            return

        branch = feature_branch_name(self._issue_id)
        exists = await self._check_remote_branch(branch)
        if not exists:
            self._log(
                f"[red bold]Feature branch 'origin/{branch}' not found.[/red bold]\n"
                f"  Create it with: git push origin <local-branch>:{branch}"
            )
            raise SystemExit(1)

    async def _check_remote_branch(self, branch: str) -> bool:
        """Check whether origin/<branch> exists after fetching."""
        fetch = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        await fetch.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        await proc.communicate()
        return proc.returncode == 0

    def _log(self, msg: str) -> None:
        if self._tui:
            self._tui.log(msg)
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self._console.print(f"[dim]{ts}[/dim] {msg}")

    async def poll_once(self) -> str | None:
        """Find next routable ticket (dependency-aware), dispatch it, return ticket ID or None."""
        # All-issues mode: pick the first issue on initial poll
        if self._all_issues and self._issue_id is None:
            await self._advance_to_next_issue()

        ticket_id = await get_next_routable(
            self._db, states=ROUTABLE_STATES, issue_id=self._issue_id
        )

        # All-issues mode: advance to next issue when current is exhausted
        if ticket_id is None and self._all_issues:
            next_issue = await self._advance_to_next_issue()
            if next_issue is not None:
                ticket_id = await get_next_routable(
                    self._db, states=ROUTABLE_STATES, issue_id=self._issue_id
                )

        if ticket_id is None:
            return None

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return None

        if ticket.state == "Code Review":
            ci_gate = await self._apply_code_review_ci_gate(ticket)
            if ci_gate == "skip":
                return None
            if ci_gate == "rerouted":
                return ticket_id
        if ticket.state == "Ready to Merge":
            ready_gate = await self._apply_ready_to_merge_gate(ticket)
            if ready_gate == "skip":
                return None
            if ready_gate == "rerouted":
                return ticket_id
            risk_score = getattr(ticket, "risk_score", None)
            if isinstance(risk_score, int) and risk_score >= 5:
                await update_ticket(self._db, ticket_id, state="Human Merge")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(
                        [
                            "## ROUTER_GATE: RISK_REQUIRES_HUMAN_MERGE",
                            "",
                            f"**Result:** Ticket risk score {risk_score} requires human merge.",
                            "**Action:** moved to `Human Merge` without invoking merger.",
                        ]
                    ),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Human Merge")
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                    " merger gate routed highest-risk ticket → Human Merge"
                )
                await self._fire_webhook(ticket_id, "Ready to Merge", "Human Merge")
                return ticket_id
            if isinstance(risk_score, int) and risk_score <= 2:
                await self._fast_merge(ticket)
                return ticket_id

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

        if self._manual_approval and not await self._prompt_approval(
            ticket_id, agent_type, ticket
        ):
            self.stop()
            return None

        await self._dispatch(ticket_id, agent_type)
        return ticket_id

    async def _advance_to_next_issue(self) -> int | None:
        """Find the next issue with routable tickets, update _issue_id, return it or None."""
        issue_ids = await get_routable_issue_ids(self._db, states=ROUTABLE_STATES)
        # Find the first issue_id greater than the current one
        for iid in issue_ids:
            if self._issue_id is None or iid > self._issue_id:
                self._log(f"[cyan]⟫[/cyan] Switching to issue [bold]#{iid}[/bold]")
                self._issue_id = iid
                return iid
        return None

    async def _dispatch(self, ticket_id: str, agent_type: str) -> None:
        """Move ticket to In Progress, create worktree, launch agent, handle result."""
        # Fetch ticket first so the issue_id and pre-dispatch state are available.
        # Scoping under the issue prevents branch collisions when a new issue reuses
        # the same ticket sequence numbers (ORCH-001, ORCH-002, ...).
        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        pre_dispatch_state: str = str(getattr(ticket, "state", "To Do"))
        issue_slug = f"issue-{ticket.issue_id}" if ticket.issue_id is not None else "no-issue"
        worktree_dir = self._state_dir / "worktrees" / issue_slug / ticket_id
        branch = f"ticket/{issue_slug}/{ticket_id}"

        await update_ticket(
            self._db,
            ticket_id,
            state="In Progress",
            worktree_path=str(worktree_dir),
        )

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        base_ref = f"origin/{feature_branch_name(ticket.issue_id)}"
        await self._create_worktree(ticket_id, branch, worktree_dir, base_ref)

        if not worktree_dir.is_dir():
            logger.error("Worktree directory missing after creation attempt: %s", worktree_dir)
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                f"Router could not create worktree at {worktree_dir}. Check git worktree state.",
                author="router",
            )
            return

        # Worktree setup (e.g. rebase on re-dispatch) may have gated the ticket to a
        # terminal state — abort dispatch without invoking the agent.
        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None or ticket.state != "In Progress":
            return

        self._sync_opencode_to_worktree(worktree_dir)

        # Run setup commands (e.g. uv sync, npm install) before any agent.
        # On failure the ticket is reverted to its pre-dispatch state and dispatch aborts.
        if self._config.setup.commands:
            setup_ok = await self._run_setup(
                ticket_id, worktree_dir, self._config.setup.commands, pre_dispatch_state
            )
            if not setup_ok:
                return

        comments = await get_ticket_comments(self._db, ticket_id)
        comments_data = [{"author": c.author, "body": c.body} for c in comments]

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
            "validation_commands": self._config.validation.commands,
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

        # Run baseline validation before every coder dispatch (fresh or rework).
        # If validators fail, the coder can't distinguish its own breakage from
        # pre-existing failures, so halt and prompt the operator.
        if agent_type == "coder" and self._config.validation.commands:
            val_ok = await self._check_baseline_validation(
                ticket_id, worktree_dir, self._config.validation.commands
            )
            if not val_ok:
                await update_ticket(self._db, ticket_id, state=pre_dispatch_state)
                await add_comment(
                    self._db,
                    ticket_id,
                    "Router halted dispatch: baseline validation failed. "
                    "Fix the failing validators and re-queue the ticket.",
                    author="router",
                )
                return

        if agent_type == "merger":
            review_decision, review_comment = _extract_current_review_decision(comments)
            dispatch_config["review_decision"] = review_decision
            dispatch_config["review_comment"] = review_comment

        # Pre-run validation for reviewer and merger — deterministic, no LLM needed.
        if agent_type in ("reviewer", "merger") and self._config.validation.commands:
            val_results = await self._run_validation(
                worktree_dir, self._config.validation.commands
            )
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
            if agent_type == "reviewer":
                failed_results = [result for result in val_results if not result.get("passed")]
                if failed_results:
                    failed_commands = ", ".join(
                        (
                            f"{result.get('command', '<unknown>')} "
                            f"(exit {result.get('exit_code', '?')})"
                        )
                        for result in failed_results
                    )
                    await update_ticket(self._db, ticket_id, state="Rework")
                    await add_comment(
                        self._db,
                        ticket_id,
                        "\n".join(
                            [
                                "## ROUTER_GATE: VALIDATION_FAILED",
                                "",
                                (
                                    "**Result:** Validation failed before reviewer dispatch: "
                                    f"{failed_commands}"
                                ),
                                "**Action:** moved to `Rework` without invoking reviewer.",
                            ]
                        ),
                        author="router",
                    )
                    if self._tui:
                        self._tui.set_ticket_state("Rework")
                    self._log(
                        f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                        " reviewer gate failed → Rework"
                    )
                    return

        # Mergeability gate for reviewer — short-circuit if PR has conflicts.
        if agent_type == "reviewer" and ticket.linked_pr:
            mergeable = await self._check_pr_mergeability(ticket.linked_pr)
            if mergeable != "MERGEABLE":
                await update_ticket(self._db, ticket_id, state="Rework")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(
                        [
                            "## ROUTER_GATE: MERGE_CONFLICT",
                            "",
                            f"**Result:** PR mergeability is `{mergeable}`",
                            "**Action:** moved to `Rework` without invoking reviewer.",
                        ]
                    ),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Rework")
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                    " reviewer mergeability gate failed → Rework"
                )
                return

        if self._tui:
            issue_id = getattr(ticket, "issue_id", None)
            issue_label = f"Issue-{issue_id}" if issue_id is not None else "—"
            issue_tickets = [
                (t.id, t.state)
                for t in await list_tickets(self._db)
                if getattr(t, "issue_id", None) == issue_id
            ]
            dependencies = await get_dependencies(self._db, ticket_id)
            self._tui.set_dispatching(
                ticket_id,
                ticket.title,
                ticket.state,
                agent_type,
                total_tickets=len(issue_tickets),
                issue_label=issue_label,
                issue_tickets=issue_tickets,
                risk_score=getattr(ticket, "risk_score", None),
                rework_count=getattr(ticket, "rework_loop_count", 0),
                dependencies=dependencies,
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

        prompt = (
            f"Read ORCH_DISPATCH_{ticket_id}.md in the current"
            " directory for your full dispatch payload, then begin."
        )

        self._last_dispatch_tokens = 0
        exit_code = await self._run_agent(ticket_id, worktree_dir, agent_type, prompt)
        if exit_code == 0:
            from orch.metrics import record_ticket_metric

            await record_ticket_metric(
                self._db,
                ticket_id=ticket_id,
                agent_type=agent_type,
                model=str(model_name),
                total_tokens=int(self._last_dispatch_tokens),
            )

        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        if self._tui:
            self._tui.set_ticket_state(ticket.state)

        if ticket.state == "Done":
            self._log(f"[green]✓[/green] [bold]{ticket_id}[/bold] done (exit {exit_code})")
            await self._retain_outcome(ticket_id)
        elif ticket.state == "In Progress":
            # Agent killed by signal (e.g. Ctrl+C / SIGINT) — prompt operator.
            if exit_code < 0:
                await self._handle_sigint(ticket_id, pre_dispatch_state, worktree_dir, agent_type)
            elif exit_code == 0 and agent_type == "coder":
                next_state = "Rework"
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                    " coder exited 0 without completing"
                    " → Rework (likely hit step limit)"
                )
                if self._tui:
                    self._tui.set_ticket_state(next_state)
                await update_ticket(self._db, ticket_id, state=next_state)
                comment_body = await self._build_step_limit_handoff(ticket_id)
                await add_comment(self._db, ticket_id, comment_body, author="router")
            else:
                next_state = "Needs Human Review"
                self._log(
                    f"[red]✗[/red] [bold]{ticket_id}[/bold]"
                    f" agent exited {exit_code} without"
                    f" completing → {next_state}"
                )
                if self._tui:
                    self._tui.set_ticket_state(next_state)
                await update_ticket(self._db, ticket_id, state=next_state)
                comment_body = await self._build_escalation_comment(
                    ticket_id, exit_code, agent_type
                )
                await add_comment(self._db, ticket_id, comment_body, author="router")
                await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")
        else:
            self._log(
                f"[green]✓[/green] [bold]{ticket_id}[/bold]"
                f" exit {exit_code} → [bold]{ticket.state}[/bold]"
            )

        if self._tui:
            self._tui.on_agent_done()

    async def _fast_merge(self, ticket: object) -> None:
        """Merge a low-risk approved PR directly from the router."""
        ticket_id = str(getattr(ticket, "id", ""))
        pr_url = str(getattr(ticket, "linked_pr", "") or "")

        if not pr_url:
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        "## ROUTER_FAST_MERGE_FAILED",
                        "",
                        "**Result:** No linked PR was available for fast merge.",
                        "**Action:** moved to `Needs Human Review`.",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state("Needs Human Review")
            return

        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "merge",
            pr_url,
            "--squash",
            "--auto",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (
                stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            )
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        "## ROUTER_FAST_MERGE_FAILED",
                        "",
                        f"**Result:** `gh pr merge` failed for {pr_url}.",
                        f"**Detail:** {detail or 'No additional output.'}",
                        "**Action:** moved to `Needs Human Review`.",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state("Needs Human Review")
            self._log(
                f"[red]✗[/red] [bold]{ticket_id}[/bold] router fast merge failed"
                " → Needs Human Review"
            )
            await self._fire_webhook(ticket_id, "Ready to Merge", "Needs Human Review")
            return

        await update_ticket(self._db, ticket_id, state="Done")
        await add_comment(
            self._db,
            ticket_id,
            "\n".join(
                [
                    "## ROUTER_FAST_MERGE",
                    "",
                    f"**Result:** Merged linked PR {pr_url} via router fast merge.",
                    "**Action:** moved to `Done` without invoking merger agent.",
                ]
            ),
            author="router",
        )
        if self._tui:
            self._tui.set_ticket_state("Done")
        self._log(
            f"[green]✓[/green] [bold]{ticket_id}[/bold] router fast-merged [dim]{pr_url}[/dim]"
        )
        await self._retain_outcome(ticket_id)


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

        self._log(f"  [dim]baseline validation for {ticket_id} before coder dispatch…[/dim]")
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
                except EOFError, KeyboardInterrupt:
                    return False

                if answer == "skip":
                    self._log(
                        f"[yellow]⚠[/yellow] {ticket_id} skipped"
                        " — fix baseline validation and re-queue"
                    )
                    return False

                # Retry validation
                self._console.print("  Re-running validators…")
                results = await self._run_validation(worktree_dir, commands)
                failures = [r for r in results if not r.get("passed")]
                if not failures:
                    self._console.print(
                        f"  [green]✓ All validators passing — dispatching {ticket_id}[/green]\n"
                    )
                    return True

                self._console.print(
                    f"  [red]Still {len(failures)} failing.[/red]"
                    " Fix and press Enter again, or type 'skip':\n"
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
                return f"... ({len(lines) - max_lines} lines truncated)\n" + "\n".join(
                    lines[-max_lines:]
                )
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

            results.append(
                {
                    "command": cmd,
                    "exit_code": exit_code,
                    "passed": passed,
                    "stdout": _truncate(raw_stdout.decode(errors="replace").strip()),
                    "stderr": _truncate(raw_stderr.decode(errors="replace").strip()),
                }
            )
            status = "[green]✓[/green]" if passed else "[red]✗[/red]"
            self._log(f"  {status} [dim]{cmd}[/dim]  exit={exit_code}")

        return results

    async def _run_setup(
        self,
        ticket_id: str,
        worktree_dir: Path,
        commands: list[str],
        pre_dispatch_state: str,
    ) -> bool:
        """Run worktree setup commands (e.g. uv sync, npm install) before agent dispatch.

        Returns True if all commands pass (proceed with dispatch).
        On failure: reverts the ticket to pre_dispatch_state, adds a structured
        ROUTER_GATE: SETUP_FAILED comment, logs the error, and returns False.
        """
        import os
        import shlex

        self._log(f"  [dim]running {len(commands)} setup command(s) in worktree…[/dim]")
        env = {**os.environ, "ORCH_DB_PATH": self._db_path_abs()}

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

            if exit_code == 0:
                self._log(f"  [green]✓[/green] setup [dim]{cmd}[/dim]")
                continue

            # Setup command failed — revert ticket and alert.
            stdout_text = raw_stdout.decode(errors="replace").strip()
            stderr_text = raw_stderr.decode(errors="replace").strip()
            combined = "\n".join(filter(None, [stdout_text, stderr_text]))

            comment_lines = [
                "## ROUTER_GATE: SETUP_FAILED",
                "",
                f"**Command:** `{cmd}`",
                f"**Exit code:** {exit_code}",
                f"**Action:** dispatch aborted; ticket reverted to `{pre_dispatch_state}`.",
            ]
            if combined:
                preview = "\n".join(combined.splitlines()[-20:])
                comment_lines += ["", "```", preview, "```"]

            await update_ticket(self._db, ticket_id, state=pre_dispatch_state)
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(comment_lines),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state(pre_dispatch_state)
            self._log(
                f"[red]✗[/red] [bold]{ticket_id}[/bold]"
                f" setup failed `{cmd}` (exit {exit_code})"
                f" → reverted to {pre_dispatch_state}"
            )
            return False

        return True

    async def _check_pr_mergeability(self, pr_url: str) -> str:
        """Check GitHub PR mergeability via gh CLI.

        Returns 'MERGEABLE', 'CONFLICTING', or 'UNKNOWN'.
        """
        import json

        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "mergeable",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            data = json.loads(stdout.decode(errors="replace"))
            return str(data.get("mergeable", "UNKNOWN"))
        except Exception:
            return "UNKNOWN"

    async def _check_pr_base_branch(self, pr_url: str) -> str:
        """Return the base branch name for a PR, or empty string if unavailable."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "baseRefName",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return ""
            data = json.loads(stdout.decode(errors="replace"))
            return str(data.get("baseRefName", ""))
        except Exception:
            return ""

    async def _fix_pr_base_mismatch(
        self, ticket_id: str, bad_pr_url: str, correct_base: str
    ) -> str:
        """Close a PR targeting the wrong base and create a correct one.

        Returns the new PR URL on success, empty string on failure.
        """
        from orch.pr import create_pr

        # Close the bad PR
        close = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "close",
            bad_pr_url,
            "--comment",
            f"Closing: PR targets wrong base branch. Re-creating against `{correct_base}`.",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        await close.communicate()

        # Determine the worktree dir for cwd
        ticket = await get_ticket(self._db, ticket_id)
        worktree_path = getattr(ticket, "worktree_path", "") or ""
        cwd = Path(worktree_path) if worktree_path else self._repo_root

        try:
            new_url = await create_pr(
                self._db,
                ticket_id,
                base_branch=correct_base,
                cwd=cwd,
            )
        except Exception:
            logger.exception("Failed to auto-create correct PR for %s", ticket_id)
            return ""
        else:
            self._log(
                f"[green]✓[/green] [bold]{ticket_id}[/bold]"
                f" created correct PR: {new_url}"
            )
            return new_url

    async def _check_pr_review_state(self, pr_url: str) -> str:
        """Return the latest meaningful review state for a PR via gh CLI.

        Returns 'APPROVED', 'CHANGES_REQUESTED', or 'Not available'.
        Used as a fallback when the reviewer approved on the PR but did not write
        a ## REVIEW_DECISION: ticket comment.

        Checks two sources (in order):
        1. Formal GitHub PR reviews (gh pr view --json reviews)
        2. PR issue comments containing a ## REVIEW_DECISION: marker
           (reviewers sometimes write here instead of as a formal review)
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "reviews,comments",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return "Not available"
            data = json.loads(stdout.decode(errors="replace"))

            # 1. Formal PR reviews — walk newest first, skip COMMENTED/PENDING.
            reviews: list[dict[str, object]] = data.get("reviews", [])
            for review in reversed(reviews):
                state = str(review.get("state", "")).upper()
                if state == "APPROVED":
                    return "APPROVED"
                if state in ("CHANGES_REQUESTED", "DISMISSED"):
                    return "CHANGES_REQUESTED"

            # 2. PR issue comments — scan newest first for ## REVIEW_DECISION: marker.
            comments: list[dict[str, object]] = data.get("comments", [])
            for comment in reversed(comments):
                body: str = str(comment.get("body", "") or "")
                if body.startswith(_REVIEW_DECISION_PREFIX):
                    first_line = body.splitlines()[0]
                    decision = first_line.removeprefix(_REVIEW_DECISION_PREFIX).strip()
                    if decision == "APPROVED":
                        return "APPROVED"
                    if decision in ("CHANGES_REQUESTED", "NEEDS_HUMAN_REVIEW"):
                        return "CHANGES_REQUESTED"
        except Exception:
            pass
        return "Not available"

    async def _load_pr_status_check_rollup(self, pr_ref: str) -> list[object] | None:
        """Fetch GitHub statusCheckRollup for a linked PR, or None if unavailable."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "view",
            pr_ref,
            "--json",
            "statusCheckRollup",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        raw_stdout, raw_stderr = await proc.communicate()
        if proc.returncode:
            stderr = raw_stderr.decode(errors="replace").strip()
            logger.warning("gh pr view failed for %s: %s", pr_ref, stderr)
            return None

        try:
            payload = json.loads(raw_stdout.decode(errors="replace") or "{}")
        except json.JSONDecodeError:
            logger.warning("Invalid gh pr view JSON for %s", pr_ref)
            return None

        rollup = payload.get("statusCheckRollup")
        if isinstance(rollup, list):
            return rollup
        return None

    async def _apply_code_review_ci_gate(self, ticket: object) -> str:
        """Apply deterministic CI gating for Code Review tickets."""
        linked_pr = getattr(ticket, "linked_pr", "") or ""
        if not linked_pr:
            return "allow"

        rollup = await self._load_pr_status_check_rollup(str(linked_pr))
        if rollup is None:
            return "allow"

        gate_status = _classify_status_check_rollup(rollup)
        ticket_id = str(getattr(ticket, "id", ""))

        if gate_status == "pending":
            self._log(
                f"[dim]↺[/dim] [bold]{ticket_id}[/bold]"
                " reviewer gate waiting on CI; skipping this poll"
            )
            return "skip"

        if gate_status == "failed":
            await update_ticket(self._db, ticket_id, state="Rework")
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        "## ROUTER_GATE: CI_FAILED",
                        "",
                        f"**Result:** Required CI checks failed for linked PR {linked_pr}.",
                        "**Action:** moved to `Rework` without invoking reviewer.",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state("Rework")
            self._log(
                f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold] reviewer gate failed CI → Rework"
            )
            return "rerouted"

        return "allow"

    async def _apply_ready_to_merge_gate(self, ticket: object) -> str:
        """Apply deterministic gating for merger dispatch.

        Checks (in order): risk score, review decision, PR mergeability, required CI.
        Returns 'allow', 'skip', or 'rerouted'.
        """
        ticket_id = str(getattr(ticket, "id", ""))
        risk_score = getattr(ticket, "risk_score", None)

        if not isinstance(risk_score, int) or not (1 <= risk_score <= 5):
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        "## ROUTER_GATE: RISK_SCORE_INVALID",
                        "",
                        f"**Result:** Ticket risk score is missing or invalid: {risk_score!r}.",
                        "**Action:** moved to `Needs Human Review` without invoking merger.",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state("Needs Human Review")
            self._log(
                f"[red]↑[/red] [bold]{ticket_id}[/bold]"
                " merger gate missing valid risk score → Needs Human Review"
            )
            await self._fire_webhook(ticket_id, "Ready to Merge", "Needs Human Review")
            return "rerouted"

        linked_pr = str(getattr(ticket, "linked_pr", "") or "")
        if not linked_pr:
            await update_ticket(self._db, ticket_id, state="Needs Human Review")
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        "## ROUTER_GATE: NO_LINKED_PR",
                        "",
                        "**Result:** Ticket has no linked pull request.",
                        "**Action:** moved to `Needs Human Review` without invoking merger."
                        " Create a PR and link it, then re-queue.",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state("Needs Human Review")
            self._log(
                f"[red]↑[/red] [bold]{ticket_id}[/bold]"
                " merger gate no linked PR → Needs Human Review"
            )
            await self._fire_webhook(ticket_id, "Ready to Merge", "Needs Human Review")
            return "rerouted"

        # PR base branch gate — verify the PR targets the expected issue branch.
        # If mismatched, auto-recover: close the bad PR, create a correct one, and
        # continue the merge flow. This prevents an infinite NHR loop when the coder
        # bypasses pr-create and uses raw gh pr create with the wrong base.
        expected_base = feature_branch_name(getattr(ticket, "issue_id", None))
        pr_base = await self._check_pr_base_branch(linked_pr)
        if pr_base and pr_base != expected_base:
            self._log(
                f"[yellow]⚙[/yellow] [bold]{ticket_id}[/bold]"
                f" PR base `{pr_base}` ≠ `{expected_base}`"
                " — closing bad PR and creating correct one"
            )
            new_pr = await self._fix_pr_base_mismatch(
                ticket_id, linked_pr, expected_base
            )
            if new_pr:
                # Re-read the linked_pr for downstream gates
                linked_pr = new_pr
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(
                        [
                            "## ROUTER_GATE: PR_BASE_MISMATCH (auto-recovered)",
                            "",
                            f"**Result:** PR base branch was `{pr_base}`,"
                            f" expected `{expected_base}`.",
                            f"**Action:** closed bad PR, created {new_pr}"
                            f" targeting `{expected_base}`.",
                        ]
                    ),
                    author="router",
                )
            else:
                # Auto-recovery failed — escalate
                await update_ticket(self._db, ticket_id, state="Needs Human Review")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(
                        [
                            "## ROUTER_GATE: PR_BASE_MISMATCH",
                            "",
                            f"**Result:** PR base branch is `{pr_base}`,"
                            f" expected `{expected_base}`.",
                            "**Action:** auto-recovery failed. Moved to"
                            " `Needs Human Review`.",
                        ]
                    ),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Needs Human Review")
                self._log(
                    f"[red]↑[/red] [bold]{ticket_id}[/bold]"
                    " PR base mismatch auto-recovery failed → Needs Human Review"
                )
                await self._fire_webhook(
                    ticket_id, "Ready to Merge", "Needs Human Review"
                )
                return "rerouted"

        comments = await get_ticket_comments(self._db, ticket_id)
        review_decision, _review_comment = _extract_current_review_decision(comments)

        # Fallback: if no REVIEW_DECISION ticket comment, check GitHub PR review state.
        # Handles the case where the reviewer approved on the PR but forgot the ticket comment.
        linked_pr_for_review = str(getattr(ticket, "linked_pr", "") or "")
        if review_decision == "Not available" and linked_pr_for_review:
            pr_review = await self._check_pr_review_state(linked_pr_for_review)
            if pr_review in ("APPROVED", "CHANGES_REQUESTED"):
                self._log(
                    f"  [dim]{ticket_id} no ticket REVIEW_DECISION comment —"
                    f" using GitHub PR review state: {pr_review}[/dim]"
                )
                review_decision = pr_review

        if review_decision != "APPROVED":
            gate_state = "Needs Human Review"
            gate_name = "REVIEW_DECISION_MISSING"
            result = f"Latest review decision is {review_decision}."
            action = "moved to `Needs Human Review` without invoking merger."

            if review_decision == "CHANGES_REQUESTED":
                gate_state = "Rework"
                gate_name = "REVIEW_CHANGES_REQUESTED"
                action = "moved to `Rework` without invoking merger."
            elif review_decision == "NEEDS_HUMAN_REVIEW":
                gate_name = "REVIEW_NEEDS_HUMAN_REVIEW"

            await update_ticket(self._db, ticket_id, state=gate_state)
            await add_comment(
                self._db,
                ticket_id,
                "\n".join(
                    [
                        f"## ROUTER_GATE: {gate_name}",
                        "",
                        f"**Result:** {result}",
                        f"**Action:** {action}",
                    ]
                ),
                author="router",
            )
            if self._tui:
                self._tui.set_ticket_state(gate_state)
            self._log(
                f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                f" merger gate routed review decision → {gate_state}"
            )
            await self._fire_webhook(ticket_id, "Ready to Merge", gate_state)
            return "rerouted"

        # PR mergeability gate — skip if no linked PR.
        linked_pr = str(getattr(ticket, "linked_pr", "") or "")
        if linked_pr:
            mergeable = await self._check_pr_mergeability(linked_pr)
            if mergeable == "CONFLICTING":
                await update_ticket(self._db, ticket_id, state="Rework")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(
                        [
                            "## ROUTER_GATE: MERGE_CONFLICT",
                            "",
                            f"**Result:** PR mergeability is `{mergeable}`",
                            "**Action:** moved to `Rework` without invoking merger.",
                        ]
                    ),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Rework")
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                    " merger mergeability gate failed → Rework"
                )
                return "rerouted"

            # Required CI checks gate.
            rollup = await self._load_pr_status_check_rollup(linked_pr)
            if rollup is not None:
                gate_status = _classify_status_check_rollup(rollup)
                if gate_status == "pending":
                    self._log(
                        f"[dim]↺[/dim] [bold]{ticket_id}[/bold]"
                        " merger gate waiting on CI; skipping this poll"
                    )
                    return "skip"
                if gate_status == "failed":
                    await update_ticket(self._db, ticket_id, state="Rework")
                    await add_comment(
                        self._db,
                        ticket_id,
                        "\n".join(
                            [
                                "## ROUTER_GATE: CI_FAILED",
                                "",
                                "**Result:** Required CI checks failed"
                                f" for linked PR {linked_pr}.",
                                "**Action:** moved to `Rework` without invoking merger.",
                            ]
                        ),
                        author="router",
                    )
                    if self._tui:
                        self._tui.set_ticket_state("Rework")
                    self._log(
                        f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold]"
                        " merger gate failed CI → Rework"
                    )
                    return "rerouted"

        return "allow"

    async def _build_step_limit_handoff(self, ticket_id: str) -> str:
        """Build a coder handoff comment for when the step budget is exhausted.

        Unlike the NHR escalation, this is addressed to the NEXT coder dispatch:
        it explains what happened and how to pick up where the previous run left off.
        """
        ticket = await get_ticket(self._db, ticket_id)
        rework_count = getattr(ticket, "rework_loop_count", 0) if ticket else 0

        lines = [
            "## CODER HANDOFF — Step Budget Exhausted",
            "",
            f"**Rework loop:** {rework_count}",
            "",
            "The previous coder dispatch reached its step budget and was stopped by"
            " the harness before completing. Tool use was disabled on the final step,"
            " so the agent could not write this comment itself.",
            "",
            "**What to do next (for the incoming coder):**",
            "1. Run `git log --oneline -5` — look for a `WIP [router]` commit containing"
            " uncommitted work from the previous run.",
            "2. Review the ticket acceptance criteria and determine what is still missing.",
            "3. Continue from the WIP state: amend the WIP commit or add new commits.",
            "4. Run validation, commit, push, and move the ticket to `Code Review`.",
        ]

        # Include the most recent non-router comment (last coder text, if any)
        comments = await get_ticket_comments(self._db, ticket_id)
        last_coder = next((c for c in reversed(comments) if c.author == "coder"), None)
        if last_coder:
            preview = last_coder.body[:400]
            if len(last_coder.body) > 400:
                preview += "\n... *(truncated)*"
            lines += [
                "",
                "**Last coder comment (partial context):**",
                "",
                preview,
            ]

        return "\n".join(lines)

    async def _build_escalation_comment(
        self, ticket_id: str, exit_code: int, agent_type: str
    ) -> str:
        """Build an informative NHR escalation comment summarising what happened."""
        comments = await get_ticket_comments(self._db, ticket_id)
        ticket = await get_ticket(self._db, ticket_id)
        rework_count = getattr(ticket, "rework_loop_count", 0) if ticket else 0

        lines = [
            "## Needs Human Review — escalated by router",
            "",
            f"**Agent:** {agent_type}  |  **Exit code:** {exit_code}"
            f"  |  **Rework loops:** {rework_count}",
            "",
            "**Reason:** The agent exited without moving this ticket to the expected next state.",
        ]

        # Find the most recent non-router comment (reviewer/coder findings)
        review_comment = next((c for c in reversed(comments) if c.author not in ("router",)), None)
        if review_comment:
            preview = review_comment.body[:600]
            if len(review_comment.body) > 600:
                preview += "\n... *(truncated — see full comment above)*"
            lines += [
                "",
                f"**Last agent comment** (from {review_comment.author}):",
                "",
                preview,
            ]

        lines += [
            "",
            "**To resume:** move this ticket to `Rework` once the issues above are addressed.",
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
        """Fire webhook through the shared webhook module."""
        ticket = await get_ticket(self._db, ticket_id)
        if ticket is None:
            return

        try:
            event = Event(
                ticket_id=ticket_id,
                timestamp=datetime.now().isoformat(),
                actor="router",
                old_state=old_state,
                new_state=new_state,
            )
            config = self._config
            if self._webhook_url:
                config = config.model_copy(
                    update={
                        "webhook": config.webhook.model_copy(update={"url": self._webhook_url})
                    }
                )
            await fire_webhook(config, event, ticket, post_fn=self._webhook_post)
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
            except EOFError:
                return False
            except KeyboardInterrupt:
                return False
            else:
                return answer in ("", "y", "yes")
        finally:
            if self._tui:
                self._tui.resume()

    async def _handle_sigint(
        self,
        ticket_id: str,
        pre_dispatch_state: str,
        worktree_dir: Path,
        agent_type: str,
    ) -> None:
        """Prompt the operator when an agent is killed by a signal (e.g. Ctrl+C).

        Options:
        1. Reset to stage — revert ticket to pre-dispatch state
        2. Reset to start — delete worktree/branch, revert ticket to To Do
        3. Needs Human Review — existing escalation behavior
        """
        import sys

        if self._tui:
            self._tui.pause()

        try:
            self._console.print()
            self._console.print(
                f"[yellow bold]Agent interrupted[/yellow bold] — "
                f"[bold]{ticket_id}[/bold] ({agent_type})"
            )
            self._console.print()
            self._console.print(
                f"  [bold]1[/bold]  Reset to stage ([cyan]{pre_dispatch_state}[/cyan])"
            )
            self._console.print(
                "  [bold]2[/bold]  Reset to start"
                " (delete worktree & branch, revert to [cyan]To Do[/cyan])"
            )
            self._console.print("  [bold]3[/bold]  Move to [cyan]Needs Human Review[/cyan]")
            self._console.print()
            self._console.print("  Choice [1/2/3]: ", end="")
            sys.stdout.flush()

            try:
                answer = sys.stdin.readline().strip()
            except EOFError, KeyboardInterrupt:
                answer = "3"

            if answer == "1":
                await update_ticket(self._db, ticket_id, state=pre_dispatch_state)
                if self._tui:
                    self._tui.set_ticket_state(pre_dispatch_state)
                self._log(
                    f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold] reset to {pre_dispatch_state}"
                )
            elif answer == "2":
                await self._reset_ticket_to_start(ticket_id, worktree_dir)
            else:
                await update_ticket(self._db, ticket_id, state="Needs Human Review")
                comment_body = await self._build_escalation_comment(ticket_id, -2, agent_type)
                await add_comment(self._db, ticket_id, comment_body, author="router")
                if self._tui:
                    self._tui.set_ticket_state("Needs Human Review")
                self._log(f"[red]↑[/red] [bold]{ticket_id}[/bold] → Needs Human Review")
                await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")
        finally:
            if self._tui:
                self._tui.resume()

    async def _reset_ticket_to_start(self, ticket_id: str, worktree_dir: Path) -> None:
        """Full reset: remove worktree, delete branch, revert ticket to To Do."""
        # Remove the git worktree
        if worktree_dir.is_dir():  # noqa: ASYNC240
            remove = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "remove",
                "--force",
                str(worktree_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_root),
            )
            await remove.communicate()

        await update_ticket(self._db, ticket_id, state="To Do", worktree_path="")
        if self._tui:
            self._tui.set_ticket_state("To Do")
        self._log(
            f"[yellow]↺[/yellow] [bold]{ticket_id}[/bold] full reset → To Do (worktree removed)"
        )

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

    async def _default_create_worktree(
        self, ticket_id: str, branch: str, worktree_dir: Path, base_ref: str = "origin/main"
    ) -> None:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        if worktree_dir.is_dir():  # noqa: ASYNC240
            # Worktree already exists (re-dispatch) — fetch and rebase onto latest base.
            await self._rebase_existing_worktree(ticket_id, worktree_dir, branch, base_ref)
            return

        # Fetch origin so we always branch from the latest merged state.
        # This prevents merge conflicts caused by PRs merged since the last fetch.
        fetch = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        await fetch.communicate()

        # Create the worktree branching from the feature branch (or origin/main for legacy)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "add",
            str(worktree_dir),
            "-b",
            branch,
            base_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._repo_root),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Branch already exists (orphaned from a pruned worktree).
            # Force-update it to base_ref so we don't check out a stale commit.
            logger.debug(
                "git worktree add -b failed (%s), resetting existing branch to %s: %s",
                stderr.decode(errors="replace").strip(),
                base_ref,
                branch,
            )
            reset = await asyncio.create_subprocess_exec(
                "git",
                "branch",
                "-f",
                branch,
                base_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_root),
            )
            await reset.communicate()

            proc2 = await asyncio.create_subprocess_exec(
                "git",
                "worktree",
                "add",
                str(worktree_dir),
                branch,
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

        # Fix upstream: git worktree add -b <branch> <base_ref> sets upstream to
        # base_ref (e.g. origin/issue-81). A bare 'git push' then pushes to the
        # issue branch instead of the ticket branch. Push with -u to create the
        # correct remote tracking branch.
        if worktree_dir.is_dir():  # noqa: ASYNC240
            push_u = await asyncio.create_subprocess_exec(
                "git",
                "push",
                "-u",
                "origin",
                f"{branch}:{branch}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_dir),
            )
            await push_u.communicate()

    async def _rebase_existing_worktree(
        self, ticket_id: str, worktree_dir: Path, branch: str, base_ref: str
    ) -> None:
        """Fetch origin and rebase the existing worktree onto base_ref.

        If the worktree has uncommitted changes (e.g. coder exhausted its step budget
        before committing), auto-commits them as a WIP commit so git rebase can run.
        On conflict: aborts the rebase, moves ticket to Needs Human Review, and adds a
        structured router gate comment. On success: returns silently.
        """
        fetch = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_dir),
        )
        await fetch.communicate()

        # Check for uncommitted changes — git rebase refuses to run with a dirty tree.
        status_proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_dir),
        )
        status_out, _ = await status_proc.communicate()
        if status_out.strip():
            # Dirty worktree (agent likely hit step budget before committing).
            # Auto-commit as WIP so the rebase can proceed.
            add_proc = await asyncio.create_subprocess_exec(
                "git",
                "add",
                "-A",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_dir),
            )
            await add_proc.communicate()
            commit_proc = await asyncio.create_subprocess_exec(
                "git",
                "commit",
                "--no-verify",
                "-m",
                f"WIP [{ticket_id}]: auto-committed by router before rework rebase",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_dir),
            )
            _, commit_stderr = await commit_proc.communicate()
            if commit_proc.returncode != 0:
                stderr_text = commit_stderr.decode(errors="replace").strip()
                comment_lines = [
                    "## ROUTER_GATE: COMMIT_FAILED",
                    "",
                    "**Result:** Could not auto-commit dirty worktree before rebase.",
                    "**Action:** moved to `Needs Human Review`. Commit or stash changes"
                    " manually and re-queue.",
                ]
                if stderr_text:
                    comment_lines += ["", f"```\n{stderr_text}\n```"]

                await update_ticket(self._db, ticket_id, state="Needs Human Review")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(comment_lines),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Needs Human Review")
                self._log(
                    f"[red]↑[/red] [bold]{ticket_id}[/bold] WIP commit failed → Needs Human Review"
                )
                await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")
                return
            self._log(
                f"[yellow]⚙[/yellow] [bold]{ticket_id}[/bold]"
                " uncommitted changes auto-committed as WIP before rework rebase"
            )

            push_proc = await asyncio.create_subprocess_exec(
                "git",
                "push",
                "origin",
                f"HEAD:{branch}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_dir),
            )
            _, push_stderr = await push_proc.communicate()
            if push_proc.returncode != 0:
                stderr_text = push_stderr.decode(errors="replace").strip()
                comment_lines = [
                    "## ROUTER_GATE: PUSH_FAILED",
                    "",
                    "**Result:** Could not push WIP commit to the ticket branch before rebase.",
                    "**Action:** moved to `Needs Human Review`. Push or preserve the work"
                    " manually and re-queue.",
                ]
                if stderr_text:
                    comment_lines += ["", f"```\n{stderr_text}\n```"]

                await update_ticket(self._db, ticket_id, state="Needs Human Review")
                await add_comment(
                    self._db,
                    ticket_id,
                    "\n".join(comment_lines),
                    author="router",
                )
                if self._tui:
                    self._tui.set_ticket_state("Needs Human Review")
                self._log(
                    f"[red]↑[/red] [bold]{ticket_id}[/bold] WIP push failed → Needs Human Review"
                )
                await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")
                return

        rebase = await asyncio.create_subprocess_exec(
            "git",
            "rebase",
            base_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_dir),
        )
        _, rebase_stderr = await rebase.communicate()

        if rebase.returncode == 0:
            return

        # Rebase failed — abort and escalate to human review.
        abort = await asyncio.create_subprocess_exec(
            "git",
            "rebase",
            "--abort",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_dir),
        )
        await abort.communicate()

        stderr_text = rebase_stderr.decode(errors="replace").strip()
        comment_lines = [
            "## ROUTER_GATE: REBASE_CONFLICT",
            "",
            f"**Result:** Rebase onto `{base_ref}` failed with conflicts.",
            "**Action:** moved to `Needs Human Review`. Resolve conflicts manually and re-queue.",
        ]
        if stderr_text:
            comment_lines += ["", f"```\n{stderr_text}\n```"]

        await update_ticket(self._db, ticket_id, state="Needs Human Review")
        await add_comment(
            self._db,
            ticket_id,
            "\n".join(comment_lines),
            author="router",
        )
        if self._tui:
            self._tui.set_ticket_state("Needs Human Review")
        self._log(f"[red]↑[/red] [bold]{ticket_id}[/bold] rebase conflict → Needs Human Review")
        await self._fire_webhook(ticket_id, "In Progress", "Needs Human Review")

    def _db_path_abs(self) -> str:
        """Return the absolute path to the SQLite database."""
        return str(self._db.path.resolve())

    async def _default_run_agent(
        self, ticket_id: str, worktree_dir: Path, agent_type: str, payload: str
    ) -> int:
        import os

        # state_dir/logs/ — computed from repo_root so it's correct regardless
        # of how deep the worktree_dir nesting is (e.g. worktrees/issue-N/TICKET-ID).
        log_dir = self._state_dir / "logs"
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
                        f"  [dim]{ticket_id}[/dim] [dim]{label}[/dim]"
                        f" {line.decode(errors='replace').rstrip()}"
                    )

        await asyncio.gather(
            _read_stream(proc.stdout, stdout_chunks, "out"),  # type: ignore[arg-type]
            _read_stream(proc.stderr, stderr_chunks, "err"),  # type: ignore[arg-type]
        )
        await proc.wait()

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)
        self._last_dispatch_tokens = _sum_step_finish_total_tokens(stdout)
        log_file.write_bytes(b"=== STDOUT ===\n" + stdout + b"\n=== STDERR ===\n" + stderr)
        self._log(f"  [dim]{ticket_id} log → {log_file}[/dim]")

        return proc.returncode or 0
