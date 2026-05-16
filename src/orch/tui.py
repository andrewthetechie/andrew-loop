"""Terminal UI for the orch router."""

from __future__ import annotations

import contextlib
import json
import re
import select
import signal
import sys
import termios
import threading
import tty
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


class _LiveRenderable:
    """Wraps a zero-argument render function so Rich calls it fresh on every render pass.

    Layout slots populated with this renderable will recompute their content on every
    Rich Live auto-refresh frame (default 4Hz), so elapsed-time fields tick continuously
    even when no agent events arrive.
    """

    def __init__(self, render_fn: Callable[[], Panel]) -> None:
        self._render_fn = render_fn

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from console.render(self._render_fn(), options)


# Approximate context window limits by model name fragment (lowercase match)
_CONTEXT_LIMITS: list[tuple[str, int]] = [
    ("claude", 200_000),
    ("gemini-1.5", 1_000_000),
    ("gemini", 200_000),
    ("glm-4", 128_000),
    ("glm", 128_000),
    ("qwen3", 131_072),
    ("qwen", 131_072),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
]
_DEFAULT_CONTEXT_LIMIT = 128_000
_BAR_WIDTH = 20
_ABSOLUTE_PATH_RE = re.compile(r"/[^\s<>]+(?:/[^\s<>]+)+")


@dataclass
class _ActiveDispatch:
    ticket_id: str
    ticket_title: str
    ticket_state: str
    logical_agent: str
    model: str
    step_budget: int
    dispatch_start: datetime
    step: int = 0
    last_tool: str = "—"
    backend_id: str = ""
    physical_alias: str = ""
    step_reserve: int | None = None
    status: str = "dispatching"


def _context_limit(model: str) -> int:
    low = model.lower()
    for fragment, limit in _CONTEXT_LIMITS:
        if fragment in low:
            return limit
    return _DEFAULT_CONTEXT_LIMIT


def _context_bar(used: int, total: int, width: int = _BAR_WIDTH) -> Text:
    """Render a compact bar like ████████░░░░ 45%."""
    pct = min(used / total, 1.0) if total else 0.0
    filled = round(pct * width)
    bar = Text()
    if pct >= 0.90:
        color = "red"
    elif pct >= 0.70:
        color = "yellow"
    else:
        color = "green"
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f" {pct * 100:.0f}%", style=color if pct >= 0.70 else "")
    return bar


class RouterTUI:
    """Split-pane TUI: top 1/3 status, bottom 2/3 split into log and agent stream."""

    MAX_LOG_LINES = 500

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._live: Live | None = None
        self._log: deque[Text] = deque(maxlen=self.MAX_LOG_LINES)
        self._agent_text: deque[Text] = deque(maxlen=self.MAX_LOG_LINES)
        self._dispatch_agent_text: dict[str, deque[Text]] = {}
        self._event_log_scroll_offset = 0
        self._keyboard_stop = threading.Event()
        self._keyboard_thread: threading.Thread | None = None
        self._active_dispatches: dict[str, _ActiveDispatch] = {}
        self._selected_dispatch_ticket_id: str | None = None

        # Ticket state
        self._ticket_id: str = "—"
        self._ticket_title: str = "—"
        self._ticket_state: str = "—"
        self._total_tickets: int = 0
        self._issue_label: str = "—"
        self._issue_tickets: list[tuple[str, str]] = []
        self._risk_score: int | None = None
        self._rework_count: int = 0
        self._dependencies: list[str] = []

        # Router state
        self._stage: str = "polling"
        self._step: int = 0
        self._last_tool: str = "—"
        self._stage_start: datetime | None = None
        self._dispatch_start: datetime | None = None
        self._router_start: datetime = datetime.now()

        # Agent state
        self._agent_type: str = "—"
        self._agent_model: str = "—"
        self._context_used: int = 0  # input tokens in current step
        self._context_limit: int = _DEFAULT_CONTEXT_LIMIT
        self._root_session_id: str = ""  # first sessionID seen = parent agent
        self._step_budget: int = 50
        self._total_cost: float = 0.0

        self._layout = self._build_layout()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __enter__(self) -> RouterTUI:
        self._live = Live(
            self._layout,
            console=self._console,
            refresh_per_second=4,
            screen=False,
            vertical_overflow="visible",
        )
        self._live.__enter__()
        self._start_keyboard_listener()
        self._refresh()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop_keyboard_listener()
        if self._live:
            self._live.__exit__(*args)

    def pause(self) -> None:
        """Temporarily stop the live display (e.g. to show an interactive prompt)."""
        self._stop_keyboard_listener()
        if self._live and self._live.is_started:
            self._live.stop()

    def resume(self) -> None:
        """Restart the live display after a pause."""
        if self._live and not self._live.is_started:
            self._live.start(refresh=True)
            self._start_keyboard_listener()

    # ── Status updates (called by Router) ──────────────────────────────────────

    def set_dispatching(
        self,
        ticket_id: str,
        ticket_title: str,
        ticket_state: str,
        agent_type: str,
        total_tickets: int = 0,
        issue_label: str = "—",
        issue_tickets: list[tuple[str, str]] | None = None,
        risk_score: int | None = None,
        rework_count: int = 0,
        dependencies: list[str] | None = None,
        model: str = "",
        step_budget: int = 50,
        backend_id: str = "",
        physical_alias: str = "",
        step_reserve: int | None = None,
    ) -> None:
        dispatch = _ActiveDispatch(
            ticket_id=ticket_id,
            ticket_title=ticket_title,
            ticket_state=ticket_state,
            logical_agent=agent_type,
            model=model or "—",
            step_budget=step_budget,
            dispatch_start=datetime.now(),
            backend_id=backend_id,
            physical_alias=physical_alias,
            step_reserve=step_reserve,
        )
        self._active_dispatches[ticket_id] = dispatch
        self._dispatch_agent_text.setdefault(ticket_id, deque(maxlen=self.MAX_LOG_LINES))
        self._selected_dispatch_ticket_id = ticket_id

        self._ticket_id = ticket_id
        self._ticket_title = ticket_title
        self._ticket_state = ticket_state
        self._total_tickets = total_tickets
        self._issue_label = issue_label
        self._issue_tickets = issue_tickets or []
        self._risk_score = risk_score
        self._rework_count = rework_count
        self._dependencies = dependencies or []
        self._stage = f"dispatching {agent_type}"
        self._stage_start = datetime.now()
        self._dispatch_start = datetime.now()
        self._step = 0
        self._last_tool = "—"
        self._agent_type = agent_type
        self._agent_model = model or "—"
        self._context_used = 0
        self._context_limit = _context_limit(model)
        self._root_session_id = ""
        self._step_budget = step_budget
        self._total_cost = 0.0
        self._refresh()

    def set_stage(self, stage: str) -> None:
        self._stage = stage
        self._stage_start = datetime.now()
        self._refresh()

    def set_ticket_state(self, state: str) -> None:
        self._ticket_state = state
        active = self._active_dispatches.get(self._ticket_id)
        if active is not None:
            active.ticket_state = state
            active.status = state
        self._issue_tickets = [
            (ticket_id, state if ticket_id == self._ticket_id else ticket_state)
            for ticket_id, ticket_state in self._issue_tickets
        ]
        self._refresh()

    def set_dispatch_waiting_for_helper(self, ticket_id: str, helper_role: str) -> None:
        """Mark an active dispatch as waiting for a hidden-helper slot.

        Updates the dispatch status so the Active Dispatch view shows
        "waiting for hidden-helper slot" instead of the default status.
        This prevents operators from mistaking the wait for a hang.
        """
        active = self._active_dispatches.get(ticket_id)
        if active is None:
            return
        active.status = "waiting for hidden-helper slot"
        self._refresh()

    def select_next_dispatch(self) -> None:
        self._cycle_selected_dispatch(1)

    def select_previous_dispatch(self) -> None:
        self._cycle_selected_dispatch(-1)

    def on_agent_done(self, ticket_id: str | None = None) -> None:
        completed_ticket_id = ticket_id or self._ticket_id
        self._active_dispatches.pop(completed_ticket_id, None)
        self._dispatch_agent_text.pop(completed_ticket_id, None)
        if self._selected_dispatch_ticket_id == completed_ticket_id:
            self._selected_dispatch_ticket_id = next(iter(self._active_dispatches), None)
        self._stage = "polling"
        self._agent_type = "—"
        self._agent_model = "—"
        self._context_used = 0
        if not self._active_dispatches:
            self._agent_text.clear()
        self._stage_start = None
        self._dispatch_start = None
        self._refresh()

    # ── Logging ────────────────────────────────────────────────────────────────

    def log(self, msg: str, *, style: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"{ts} ", style="dim")
        if style:
            line.append(msg, style=style)
        else:
            line.append_text(Text.from_markup(msg))
        self._log.append(line)
        self._event_log_scroll_offset = min(
            self._event_log_scroll_offset,
            self._max_event_log_scroll_offset(),
        )
        self._refresh()

    def scroll_event_log_up(self, lines: int | None = None) -> None:
        """Move the Event Log viewport toward older entries."""
        amount = lines if lines is not None else self._visible_log_height()
        self._event_log_scroll_offset = min(
            self._event_log_scroll_offset + max(1, amount),
            self._max_event_log_scroll_offset(),
        )
        self._refresh()

    def scroll_event_log_down(self, lines: int | None = None) -> None:
        """Move the Event Log viewport toward newer entries."""
        amount = lines if lines is not None else self._visible_log_height()
        self._event_log_scroll_offset = max(0, self._event_log_scroll_offset - max(1, amount))
        self._refresh()

    def scroll_event_log_to_latest(self) -> None:
        """Return the Event Log viewport to follow-latest mode."""
        self._event_log_scroll_offset = 0
        self._refresh()

    def scroll_event_log_to_oldest(self) -> None:
        """Move the Event Log viewport to the oldest retained entries."""
        self._event_log_scroll_offset = self._max_event_log_scroll_offset()
        self._refresh()

    def parse_agent_line(self, ticket_id: str, raw: bytes) -> None:
        """Parse one opencode JSON output line and add a human-readable log entry."""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        dispatch = self._active_dispatches.get(ticket_id)
        prefix = self._dispatch_log_prefix(ticket_id, dispatch)
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            self.log(f"  [dim]{text[:120]}[/dim]")
            return

        event_type = data.get("type", "")
        part = data.get("part", {})

        if event_type == "step_start":
            if dispatch is not None:
                dispatch.step += 1
                dispatch.status = f"step {dispatch.step}"
            current_step = dispatch.step if dispatch is not None else self._step + 1
            self._step = current_step
            self.set_stage(f"step {current_step}")
            self.log(f"{prefix} step {current_step}")

        elif event_type == "step_finish":
            session_id = data.get("sessionID", "")
            tokens = part.get("tokens", {})
            cost = part.get("cost", 0)
            total_tok = tokens.get("total", 0)
            cost_str = f"${cost:.4f}" if cost else "—"
            reason = part.get("reason", "")

            # Lock onto the first sessionID as the parent agent. Subagents
            # emit step_finish events with different sessionIDs and their own
            # cumulative token counts which would inflate the context display.
            if not self._root_session_id and session_id:
                self._root_session_id = session_id

            if total_tok and session_id == self._root_session_id:
                self._context_used = total_tok

            if cost:
                self._total_cost += cost
            self._refresh()
            self.log(
                f"{prefix} step {self._step} done  "
                f"[dim]tokens={total_tok}  cost={cost_str}  reason={reason}[/dim]"
            )

        elif event_type == "text":
            agent_text = part.get("text", "").strip()
            if not agent_text:
                return
            stream = self._agent_stream_for_ticket(ticket_id)
            for line in agent_text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if "LOOP STATE:" in stripped:
                    stage = stripped.split("LOOP STATE:", 1)[-1].strip().strip("`").strip()
                    self.set_stage(stage)
                    self.log(f"{prefix} [bold]{stripped}[/bold]")
                else:
                    stream.append(Text(stripped[:120], style="italic"))
                    self._refresh()

        elif event_type == "tool_use":
            tool = part.get("tool", "?")
            state = part.get("state", {})
            status = state.get("status", "?")
            inp = state.get("input", {})
            if self._is_task_tool(tool):
                self._log_task_tool_event(ticket_id, prefix, tool, status, inp, state)
                self._refresh()
                return

            if dispatch is not None:
                dispatch.last_tool = tool
            self._last_tool = tool
            brief = self._tool_brief(tool, inp)

            if status == "completed":
                output_raw = str(state.get("output", ""))
                output = self._compact_log_fragment(output_raw, max_chars=80)
                self.log(
                    f"{prefix} [green]✓[/green] [cyan]{tool}[/cyan]{brief}  [dim]{output}[/dim]"
                )
            elif status == "error":
                err = self._compact_log_fragment(str(state.get("error", "")), max_chars=80)
                self.log(f"{prefix} [red]✗[/red] [cyan]{tool}[/cyan]{brief}  [red]{err}[/red]")
            else:
                self.log(f"{prefix} [yellow]...[/yellow] [cyan]{tool}[/cyan]{brief}")

            self._refresh()

    # ── Internal rendering ──────────────────────────────────────────────────────

    def _log_task_tool_event(
        self,
        ticket_id: str,
        prefix: str,
        tool: str,
        status: str,
        inp: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        helper = self._task_helper_name(inp)
        task = self._task_description(inp)
        task_suffix = f": {escape(task)}" if task else ""
        self._last_tool = f"{tool}:{helper}"
        stream_lines = self._agent_stream_for_ticket(ticket_id)

        if status == "completed":
            output = self._compact_log_fragment(str(state.get("output", "")), max_chars=80)
            self.log(
                f"{prefix} [green]✓[/green] [cyan]{helper} done[/cyan]  "
                f"[dim]{escape(output)}[/dim]"
            )
            stream_message = f"{helper} finished"
            if output:
                stream_message += f": {output}"
            stream_lines.append(Text(stream_message[:120], style="italic"))
        elif status == "error":
            err = self._compact_log_fragment(str(state.get("error", "")), max_chars=80)
            self.log(
                f"{prefix} [red]✗[/red] [cyan]{helper} failed[/cyan]  [red]{escape(err)}[/red]"
            )
            stream_message = f"{helper} failed"
            if err:
                stream_message += f": {err}"
            stream_lines.append(Text(stream_message[:120], style="italic red"))
        else:
            self.log(f"{prefix} [yellow]...[/yellow] [cyan]{helper} working[/cyan]{task_suffix}")
            stream_message = f"{helper} working"
            if task:
                stream_message += f": {task}"
            stream_lines.append(Text(stream_message[:120], style="italic"))

    @staticmethod
    def _dispatch_log_prefix(ticket_id: str, dispatch: _ActiveDispatch | None) -> str:
        prefix = f"[bold cyan]{ticket_id}[/bold cyan]"
        if dispatch is None or not dispatch.backend_id:
            return prefix
        worker = dispatch.physical_alias or dispatch.model
        if worker:
            return f"{prefix} [dim]({dispatch.backend_id} / {escape(worker)})[/dim]"
        return f"{prefix} [dim]({dispatch.backend_id})[/dim]"

    @staticmethod
    def _is_task_tool(tool: str) -> bool:
        return str(tool).lower() == "task"

    @staticmethod
    def _task_helper_name(inp: dict[str, Any]) -> str:
        for key in ("subagent_type", "agent", "agent_type", "helper", "role"):
            value = str(inp.get(key, "")).strip()
            if value:
                return value
        return "helper"

    @staticmethod
    def _task_description(inp: dict[str, Any]) -> str:
        for key in ("description", "task", "title", "instructions", "prompt"):
            value = str(inp.get(key, "")).strip()
            if value:
                for line in value.splitlines():
                    stripped = line.strip()
                    if stripped:
                        return RouterTUI._compact_log_fragment(stripped, max_chars=80)
        return ""

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="status", ratio=1),
            Layout(name="bottom", ratio=2),
        )
        layout["status"].split_row(
            Layout(name="ticket_info"),
            Layout(name="router_info"),
            Layout(name="agent_info"),
        )
        layout["bottom"].split_row(
            Layout(name="event_log"),
            Layout(name="agent_stream"),
        )
        # Live renderables: Rich calls the render function fresh on every refresh frame
        # so elapsed-time fields tick at 4Hz even when no agent events arrive.
        layout["ticket_info"].update(_LiveRenderable(self._render_ticket_info))
        layout["router_info"].update(_LiveRenderable(self._render_router_info))
        layout["agent_info"].update(_LiveRenderable(self._render_agent_info))
        layout["event_log"].update(_LiveRenderable(self._render_event_log))
        layout["agent_stream"].update(_LiveRenderable(self._render_agent_stream))
        return layout

    def _refresh(self) -> None:
        """Force an immediate Rich Live refresh.

        State fields are updated by callers before this is called; the live renderables
        will pick them up on the next render frame automatically. Explicit refresh
        here gives <250ms latency for important events (dispatch, log lines, etc.).
        """
        if self._live:
            self._live.refresh()

    def _render_ticket_info(self) -> Panel:
        t = Table.grid(padding=(0, 2))
        # min_width covers the longest label ("Total tickets" = 13 chars) so Rich never
        # truncates labels. The value column takes remaining space and truncates values.
        t.add_column(style="dim", no_wrap=True, min_width=13)
        t.add_column(overflow="ellipsis", no_wrap=True, ratio=1)
        t.add_row("Ticket", f"[bold cyan]{self._ticket_id}[/bold cyan]")
        t.add_row("Title", self._ticket_title)
        t.add_row("Risk", str(self._risk_score) if self._risk_score is not None else "—")
        t.add_row("Status", f"[yellow]{self._ticket_state}[/yellow]")
        t.add_row("Rework Count", str(self._rework_count))
        t.add_row("Dependencies", "\n".join(self._dependencies) if self._dependencies else "—")
        t.add_row("Issue", self._issue_label)
        t.add_row("Total tickets", str(self._total_tickets))

        issue_table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        issue_table.add_column("Ticket ID", no_wrap=True)
        issue_table.add_column("Status", overflow="ellipsis", ratio=1)
        if self._issue_tickets:
            for ticket_id, ticket_state in self._issue_tickets:
                issue_table.add_row(ticket_id, ticket_state)
        else:
            issue_table.add_row("—", "—")

        body = Group(t, Rule(style="blue"), issue_table)
        return Panel(body, title="[bold]Ticket Info[/bold]", border_style="blue")

    def _render_router_info(self) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(overflow="ellipsis", no_wrap=True)
        t.add_row("Stage", f"[bold]{self._stage}[/bold]")
        t.add_row("Step", str(self._step) if self._step else "—")
        t.add_row("Last tool", f"[cyan]{self._last_tool}[/cyan]")
        t.add_row("Stage runtime", self._elapsed(self._stage_start))
        t.add_row("Agent runtime", self._elapsed(self._dispatch_start))
        t.add_row("Total runtime", self._elapsed(self._router_start))

        body: Group | Table = t
        if self._active_dispatches:
            active = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
            active.add_column("Ticket", no_wrap=True)
            active.add_column("Agent", no_wrap=True)
            active.add_column("Backend", no_wrap=True)
            active.add_column("Worker", overflow="ellipsis", ratio=1)
            active.add_column("Steps", no_wrap=True)
            active.add_column("Runtime", no_wrap=True)
            active.add_column("Last Tool", overflow="ellipsis", ratio=1)
            active.add_column("Status", overflow="ellipsis", ratio=1)
            for dispatch in self._active_dispatches.values():
                worker = dispatch.physical_alias or dispatch.model
                step_usage = (
                    f"{dispatch.step}/{dispatch.step_reserve}"
                    if dispatch.step_reserve is not None
                    else str(dispatch.step)
                )
                active.add_row(
                    dispatch.ticket_id,
                    dispatch.logical_agent,
                    dispatch.backend_id or "—",
                    worker,
                    step_usage,
                    self._elapsed(dispatch.dispatch_start),
                    dispatch.last_tool,
                    dispatch.status,
                )
            body = Group(t, Rule("[bold]Active Dispatches[/bold]", style="blue"), active)

        return Panel(body, title="[bold]Router Info[/bold]", border_style="blue")

    def _render_agent_info(self) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim", no_wrap=True)
        t.add_column(overflow="ellipsis", no_wrap=True)

        t.add_row("Agent", f"[bold magenta]{self._agent_type}[/bold magenta]")
        t.add_row("Runtime", self._elapsed(self._dispatch_start))

        # Model — truncate long provider prefixes for display
        model_display = self._agent_model
        if "/" in model_display:
            model_display = model_display.split("/", 1)[1]
        t.add_row("Model", f"[dim]{model_display}[/dim]")

        # Step budget
        step_str = f"{self._step}/{self._step_budget}" if self._step else f"—/{self._step_budget}"
        t.add_row("Steps", step_str)

        # Context bar
        if self._context_used:
            bar = _context_bar(self._context_used, self._context_limit)
            ctx_row = Text()
            ctx_row.append_text(bar)
            ctx_row.append(
                f"  {self._context_used // 1000}k/{self._context_limit // 1000}k",
                style="dim",
            )
            t.add_row("Context", ctx_row)
        else:
            t.add_row("Context", "[dim]—[/dim]")

        # Cost
        cost_str = f"${self._total_cost:.4f}" if self._total_cost else "—"
        t.add_row("Cost", f"[dim]{cost_str}[/dim]")

        return Panel(t, title="[bold]Agent Info[/bold]", border_style="magenta")

    def _visible_log_height(self) -> int:
        term_height = self._console.height or 40
        return max(5, (term_height * 2 // 3) - 4)

    def _max_event_log_scroll_offset(self) -> int:
        return max(0, len(self._log) - self._visible_log_height())

    def _tail_lines(self, lines: deque[Text] | list[Text], *, scroll_offset: int = 0) -> Text:
        log_height = self._visible_log_height()
        max_line_width = max(24, (self._console.width or 80) // 2 - 8)
        all_lines = list(lines)
        if scroll_offset:
            offset = min(scroll_offset, max(0, len(all_lines) - log_height))
            end = max(0, len(all_lines) - offset)
            visible_lines = all_lines[max(0, end - log_height) : end]
        else:
            visible_lines = all_lines[-log_height:]

        combined = Text()
        for i, line in enumerate(visible_lines):
            if i:
                combined.append("\n")
            display_line = line.copy()
            display_line.truncate(max_line_width, overflow="ellipsis")
            combined.append_text(display_line)
        return combined

    def _render_event_log(self) -> Panel:
        title = "[bold]Event Log[/bold]"
        if self._event_log_scroll_offset:
            title += f" [dim](scroll +{self._event_log_scroll_offset}, End/latest)[/dim]"
        else:
            title += " [dim](PgUp/PgDn, k/j)[/dim]"
        return Panel(
            self._tail_lines(self._log, scroll_offset=self._event_log_scroll_offset),
            title=title,
            border_style="dim",
            padding=(0, 1),
        )

    def _render_agent_stream(self) -> Panel:
        selected_ticket_id = self._selected_dispatch_ticket_id
        title = "[bold]Agent Stream[/bold]"
        if selected_ticket_id is not None:
            title += f" [dim]({selected_ticket_id})[/dim]"
        return Panel(
            self._tail_lines(self._selected_agent_stream()),
            title=title,
            border_style="dim",
            padding=(0, 1),
        )

    def _start_keyboard_listener(self) -> None:
        if self._keyboard_thread and self._keyboard_thread.is_alive():
            return
        stream = sys.stdin
        if not stream.isatty():
            return
        self._keyboard_stop.clear()
        self._keyboard_thread = threading.Thread(
            target=self._keyboard_loop,
            name="orch-tui-keyboard",
            daemon=True,
        )
        self._keyboard_thread.start()

    def _stop_keyboard_listener(self) -> None:
        self._keyboard_stop.set()
        if self._keyboard_thread and self._keyboard_thread.is_alive():
            self._keyboard_thread.join(timeout=0.2)
        self._keyboard_thread = None

    def _keyboard_loop(self) -> None:
        fd = sys.stdin.fileno()
        with contextlib.suppress(termios.error, OSError):
            old_attrs = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not self._keyboard_stop.is_set():
                    readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if not readable:
                        continue
                    key = sys.stdin.read(1)
                    if key == "\x03":
                        signal.raise_signal(signal.SIGINT)
                        continue
                    if key == "\x1b":
                        key = self._read_escape_sequence(key)
                    self._handle_key(key)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    @staticmethod
    def _read_escape_sequence(prefix: str) -> str:
        sequence = prefix
        while len(sequence) < 4:
            readable, _, _ = select.select([sys.stdin], [], [], 0.01)
            if not readable:
                break
            sequence += sys.stdin.read(1)
        return sequence

    def _handle_key(self, key: str) -> None:
        if key in ("k", "\x1b[5~", "\x1b[A"):
            self.scroll_event_log_up()
        elif key in ("j", "\x1b[6~", "\x1b[B"):
            self.scroll_event_log_down()
        elif key in ("g", "\x1b[H", "\x1b[1~"):
            self.scroll_event_log_to_oldest()
        elif key in ("G", "\x1b[F", "\x1b[4~"):
            self.scroll_event_log_to_latest()
        elif key == "\t":
            self.select_next_dispatch()
        elif key == "\x1b[Z":
            self.select_previous_dispatch()

    def _cycle_selected_dispatch(self, direction: int) -> None:
        ticket_ids = list(self._active_dispatches)
        if len(ticket_ids) < 2:
            return
        selected_ticket_id = self._selected_dispatch_ticket_id
        if selected_ticket_id not in ticket_ids:
            self._selected_dispatch_ticket_id = ticket_ids[0]
            self._refresh()
            return
        current_index = ticket_ids.index(selected_ticket_id)
        self._selected_dispatch_ticket_id = ticket_ids[
            (current_index + direction) % len(ticket_ids)
        ]
        self._refresh()

    def _agent_stream_for_ticket(self, ticket_id: str | None) -> deque[Text]:
        if ticket_id is None or ticket_id not in self._active_dispatches:
            return self._agent_text
        return self._dispatch_agent_text.setdefault(
            ticket_id,
            deque(maxlen=self.MAX_LOG_LINES),
        )

    def _selected_agent_stream(self) -> deque[Text]:
        return self._agent_stream_for_ticket(self._selected_dispatch_ticket_id)

    @staticmethod
    def _elapsed(start: datetime | None) -> str:
        if start is None:
            return "—"
        secs = int((datetime.now() - start).total_seconds())
        return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"

    @staticmethod
    def _tool_brief(tool: str, inp: dict[str, Any]) -> str:
        if tool == "read":
            path = RouterTUI._compact_path(str(inp.get("filePath", "")))
            offset = inp.get("offset")
            limit = inp.get("limit")
            suffix = f":{offset}" if offset else ""
            suffix += f" ({limit} lines)" if limit else ""
            return f": {path}{suffix}" if path else ""
        if tool == "glob":
            return f": {inp.get('pattern', '')}"
        if tool == "grep":
            return f": {inp.get('pattern', '')!r}"
        if tool in ("edit", "write"):
            path = RouterTUI._compact_path(str(inp.get("filePath", "")))
            return f": {path}" if path else ""
        if tool == "bash":
            cmd = inp.get("command", "")[:80]
            return f": {cmd}" if cmd else ""
        if tool == "ticket-update":
            state = inp.get("state", "")
            return f" → {state}" if state else ""
        if tool == "ticket-read":
            return f": {inp.get('ticket_id', '')}"
        if RouterTUI._is_task_tool(tool):
            helper = RouterTUI._task_helper_name(inp)
            task = RouterTUI._task_description(inp)
            return f": {helper} - {task}" if task else f": {helper}"
        return ""

    @staticmethod
    def _compact_log_fragment(value: str, *, max_chars: int) -> str:
        one_line = value.replace("\n", " ")
        one_line = _ABSOLUTE_PATH_RE.sub(
            lambda match: RouterTUI._compact_path(match.group(0), max_chars=48),
            one_line,
        )
        if len(one_line) > max_chars:
            return one_line[: max_chars - 3] + "..."
        return one_line

    @staticmethod
    def _compact_path(path: str, *, max_chars: int = 72) -> str:
        if len(path) <= max_chars:
            return path

        parts = [part for part in path.split("/") if part]
        tail: list[str] = []
        budget = max_chars - 4
        used = 0
        for part in reversed(parts):
            next_used = used + len(part) + (1 if tail else 0)
            if next_used > budget:
                break
            tail.insert(0, part)
            used = next_used
        if not tail:
            return "..." + path[-budget:]
        return ".../" + "/".join(tail)
