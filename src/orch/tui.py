"""Terminal UI for the orch router."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.live import Live
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
        self._refresh()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.__exit__(*args)

    def pause(self) -> None:
        """Temporarily stop the live display (e.g. to show an interactive prompt)."""
        if self._live and self._live.is_started:
            self._live.stop()

    def resume(self) -> None:
        """Restart the live display after a pause."""
        if self._live and not self._live.is_started:
            self._live.start(refresh=True)

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
    ) -> None:
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
        self._step_budget = step_budget
        self._total_cost = 0.0
        self._refresh()

    def set_stage(self, stage: str) -> None:
        self._stage = stage
        self._stage_start = datetime.now()
        self._refresh()

    def set_ticket_state(self, state: str) -> None:
        self._ticket_state = state
        self._issue_tickets = [
            (ticket_id, state if ticket_id == self._ticket_id else ticket_state)
            for ticket_id, ticket_state in self._issue_tickets
        ]
        self._refresh()

    def on_agent_done(self) -> None:
        self._stage = "polling"
        self._agent_type = "—"
        self._agent_model = "—"
        self._context_used = 0
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
        self._refresh()

    def parse_agent_line(self, ticket_id: str, raw: bytes) -> None:
        """Parse one opencode JSON output line and add a human-readable log entry."""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            self.log(f"  [dim]{text[:120]}[/dim]")
            return

        event_type = data.get("type", "")
        part = data.get("part", {})

        if event_type == "step_start":
            self._step += 1
            self.set_stage(f"step {self._step}")
            self.log(f"[bold cyan]{ticket_id}[/bold cyan] step {self._step}")

        elif event_type == "step_finish":
            tokens = part.get("tokens", {})
            cost = part.get("cost", 0)
            total_tok = tokens.get("total", 0)
            cost_str = f"${cost:.4f}" if cost else "—"
            reason = part.get("reason", "")

            # Use total tokens as context proxy — robust across all providers.
            # Local models (opencode-go/qwen, glm) often don't report input/output
            # breakdown separately. total_tok grows with each step as context accumulates
            # and is always a reliable non-zero signal of context window usage.
            if total_tok:
                self._context_used = total_tok

            if cost:
                self._total_cost += cost
            self._refresh()
            self.log(
                f"  step {self._step} done  "
                f"[dim]tokens={total_tok}  cost={cost_str}  reason={reason}[/dim]"
            )

        elif event_type == "text":
            agent_text = part.get("text", "").strip()
            if not agent_text:
                return
            for line in agent_text.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if "LOOP STATE:" in stripped:
                    stage = stripped.split("LOOP STATE:", 1)[-1].strip().strip("`").strip()
                    self.set_stage(stage)
                    self.log(f"  [bold]{stripped}[/bold]")
                else:
                    self._agent_text.append(Text(stripped[:120], style="italic"))
                    self._refresh()

        elif event_type == "tool_use":
            tool = part.get("tool", "?")
            state = part.get("state", {})
            status = state.get("status", "?")
            inp = state.get("input", {})
            self._last_tool = tool
            brief = self._tool_brief(tool, inp)

            if status == "completed":
                output_raw = str(state.get("output", ""))
                output = output_raw.replace("\n", " ")[:100]
                self.log(f"  [green]✓[/green] [cyan]{tool}[/cyan]{brief}  [dim]{output}[/dim]")
            elif status == "error":
                err = str(state.get("error", ""))[:100]
                self.log(f"  [red]✗[/red] [cyan]{tool}[/cyan]{brief}  [red]{err}[/red]")
            else:
                self.log(f"  [yellow]...[/yellow] [cyan]{tool}[/cyan]{brief}")

            self._refresh()

    # ── Internal rendering ──────────────────────────────────────────────────────

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
        return Panel(t, title="[bold]Router Info[/bold]", border_style="blue")

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

    def _tail_lines(self, lines: deque[Text] | list[Text]) -> Text:
        term_height = self._console.height or 40
        log_height = max(5, (term_height * 2 // 3) - 4)
        combined = Text()
        for i, line in enumerate(list(lines)[-log_height:]):
            if i:
                combined.append("\n")
            combined.append_text(line)
        return combined

    def _render_event_log(self) -> Panel:
        return Panel(
            self._tail_lines(self._log),
            title="[bold]Event Log[/bold]",
            border_style="dim",
            padding=(0, 1),
        )

    def _render_agent_stream(self) -> Panel:
        return Panel(
            self._tail_lines(self._agent_text),
            title="[bold]Agent Stream[/bold]",
            border_style="dim",
            padding=(0, 1),
        )

    @staticmethod
    def _elapsed(start: datetime | None) -> str:
        if start is None:
            return "—"
        secs = int((datetime.now() - start).total_seconds())
        return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"

    @staticmethod
    def _tool_brief(tool: str, inp: dict[str, Any]) -> str:
        if tool == "read":
            path = inp.get("filePath", "")
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
            return f": {inp.get('filePath', '')}"
        if tool == "bash":
            cmd = inp.get("command", "")[:80]
            return f": {cmd}" if cmd else ""
        if tool == "ticket-update":
            state = inp.get("state", "")
            return f" → {state}" if state else ""
        if tool == "ticket-read":
            return f": {inp.get('ticket_id', '')}"
        return ""
