# Orchestra Workflow Guide

End-to-end guide for the solo developer: from a bare machine to a running agent pipeline that takes a PRD, decomposes it into tickets, codes them, reviews them, and merges them automatically.

---

## Section 1: Setup and Install

### Prerequisites

The following tools must be installed and available on `PATH` before running `orch`:

| Tool            | Purpose                                    | Install                                            |
| --------------- | ------------------------------------------ | -------------------------------------------------- |
| `uv`            | Python package manager                     | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `opencode`      | Agent harness (runs coder/reviewer/merger) | See opencode docs                                  |
| `gh`            | GitHub CLI (creates and merges PRs)        | `brew install gh` then `gh auth login`             |
| `rtk`           | Auto-rewrite hook for opencode             | See rtk docs                                       |
| `hindsight`     | Memory bank for agent learning             | See hindsight docs                                 |
| `npx` / Node.js | Runs GitNexus CLI                          | `brew install node`                                |

MCP servers required in your opencode/Claude config:

- **Hindsight** — agent memory recall
- **Serena** — symbolic code navigation
- **GitNexus** — code intelligence and impact analysis
- **Context7** — documentation lookup
- **PullMD** — PR context fetching

### Install `orch`

From the repository root:

```sh
uv sync
```

This installs the `orch` CLI into the project virtual environment. Run it via:

```sh
uv run orch --help
```

Or activate the environment first:

```sh
source .venv/bin/activate
orch --help
```

### Global Config

Create `~/.config/orchestra/config.toml` to set defaults that apply to all repos:

```toml
[router]
poll_interval = 10.0
max_rework_loops = 3

[webhook]
url = "https://your-webhook-endpoint.example.com/notify"
triggers = ["Needs Human Review", "Human Merge"]

[harness]
command = "opencode run --agent {agent} --format json --dir {worktree_dir}"

[hindsight]
url = "http://localhost:8888"
bank_id = ""
api_key = ""

[mcp.firecrawl]
url = ""
api_key = ""

[mcp.context7]
api_key = ""

[mcp.pullmd]
url = ""

[agents.coder]
model = "claude-sonnet-4-6"

[agents.reviewer]
model = "claude-sonnet-4-6"

[agents.merger]
model = "qwen/qwen-2.5-coder"

[agents.decomposer]
model = "claude-opus-4-6"
```

Config precedence (highest to lowest):

1. CLI flags / env vars (`ORCH_DB_PATH`)
2. `.orchestra/config.toml` (per-repo)
3. `~/.config/orchestra/config.toml` (global)
4. Built-in defaults

---

## Section 2: Starting a New Project

### `orch init`

Run `orch init` in the repository you want to automate:

```sh
cd ~/projects/my-repo
orch init
```

Example output:

```
  config: created
  db: created
  agents: created
  hindsight: created
  serena: created
  gitnexus: created
```

Statuses are `created`, `exists` (already present, unchanged), `skipped` (no model config), or `failed`.

**What `orch init` creates:**

```
.orchestra/
  config.toml       # repo-level config (edit this)
  state.db          # SQLite state database
  logs/             # per-ticket agent logs
  worktrees/        # git worktrees per ticket
opencode.json       # agent configs (auto-generated, do not edit)
.opencode/agents/   # agent prompt files (coder, reviewer, merger, decomposer)
```

It also:

- Creates a Hindsight memory bank for this repo
- Indexes the repo with Serena (symbolic code navigation)
- Runs `npx gitnexus analyze` to build the code intelligence index

**Flags:**

```sh
orch init --dir /path/to/repo    # target a different directory
orch init --no-externals         # skip hindsight/serena/gitnexus setup
```

### Verify with `orch doctor`

```sh
orch doctor
```

Example output (all passing):

```
  PASS  config: config.toml found
  PASS  database: database schema valid
  PASS  gitnexus: .gitnexus/ index found
  PASS  rtk: rtk available
  PASS  opencode: opencode available
  PASS  gh: gh available
```

If any check fails, fix the issue and re-run before proceeding. `orch doctor` exits with code 1 if any check fails.

```sh
orch doctor --no-tools    # skip rtk/opencode/gh checks (CI-friendly)
```

### Customize `.orchestra/config.toml`

After `orch init`, edit `.orchestra/config.toml` for repo-specific settings:

```toml
[router]
poll_interval = 10.0        # seconds between ticket polls
max_rework_loops = 3        # automated Rework dispatches before Needs Human Review

[webhook]
url = "https://hooks.example.com/my-project"
triggers = ["Needs Human Review", "Human Merge"]

[validation]
# Commands run inside the worktree before a ticket is considered done
# The agent is responsible for passing these — failing validation is a rework signal
commands = ["uv run pytest", "uv run ruff check src/"]

[hindsight]
url = "http://localhost:8888"
bank_id = "abc123"           # set by orch init; do not change
api_key = "your-api-key"
```

### Configure Backend Catalogs

When you want deterministic **Coder backend** selection, keep `coder` as the logical
workflow role and configure backend catalogs instead of creating new router-visible
agents. This follows
[ADR-0002](./adr/0002-logical-agents-with-priority-allocated-coder-backends.md):
the Router still dispatches logical `coder`, while the Agent harness binds the run to
a stable physical alias such as `coder__cloud_free`.

Example catalog file:

```toml
[[backends]]
id = "cloud-free"
logical_agents = ["coder"]
physical_alias = "coder__cloud_free"
model = "gpt-5-mini"
priority = 10
concurrency = 1
min_reserve = 8

[backends.quota]
mode = "fixed-window"
step_limit = 120
dispatch_limit = 10
window_seconds = 86400

[[backends]]
id = "burst-free"
logical_agents = ["coder"]
physical_alias = "coder__burst_free"
model = "gpt-4o-mini"
priority = 20
concurrency = 1

[backends.quota]
mode = "dynamic-429"

[[backends]]
id = "local-fast"
logical_agents = ["coder"]
physical_alias = "coder__local_fast"
model = "qwen3-coder"
priority = 30
concurrency = 2

[backends.quota]
mode = "unlimited"
```

Repo-level enablement, ordering, and overrides live in `.orchestra/config.toml`:

```toml
[router]
poll_interval = 10.0
max_parallel_coder_dispatches = 2
max_rework_loops = 3

[backends]
enabled = true
catalog_paths = ["backend-catalog.toml"]
order = ["cloud-free", "burst-free", "local-fast"]

[backends.overrides.cloud-free]
enabled = true
priority = 5

[backends.overrides.local-fast]
concurrency = 3
min_reserve = 12
```

Allocation rules:

- Lower numeric `priority` wins first.
- If priorities match, the repo `order` list breaks ties for listed backends.
- Remaining equal-priority ties fall back to backend id ordering, so selection stays deterministic.
- The Router only allocates backends that support the requested logical agent and still have free concurrency.

Quota and reserve behavior:

- `unlimited` backends do not consume tracked Step quota and do not need a persisted Step reserve.
- `fixed-window` backends must have enough remaining recorded Steps to cover the required Step reserve before dispatch starts.
- `fixed-window` backends can also set `dispatch_limit` for a coarse per-window dispatch cap.
- `dynamic-429` backends are not blocked by a fixed Step limit, but they still persist Step reserve and actual Step usage for observability.
- The Step reserve is `min_reserve` when configured; otherwise it defaults to the coder Step budget for that dispatch.
- If a request-limited backend falls below the required reserve, the allocator skips it and records `step quota below reserve` in dispatch history.
- `window_seconds` controls fixed-window resets, such as `60` for per-minute limits or `86400` for daily limits.
- Cooldowns are persisted in SQLite. Rate-limited and offline backends are skipped until their cooldown expires or an operator clears it.

---

## Section 3: Running the Decomposer

### Write a PRD

Create a PRD describing the system you want to build. No fixed format is required, but include:

- **What** the system does (user-facing behavior)
- **Why** it exists (problem being solved)
- **Constraints** (tech stack, performance, security)
- **Out of scope** (explicit exclusions)

Save it anywhere — the decomposer reads it as input. Convention: `.scratch/PRD.md`.

### Launch the Decomposer in opencode

Open opencode in your repo and start a session with the `decomposer` agent:

```sh
opencode --agent decomposer
```

Or from the CLI:

```sh
opencode run --agent decomposer --dir .
```

### The Interactive Grilling Process

The decomposer agent will:

1. Read your PRD
2. Ask clarifying questions (ambiguities, missing constraints, scope boundaries)
3. Propose a breakdown of vertical slices
4. Iterate until you approve the ticket set

Answer each question directly. The decomposer uses your answers to sharpen scope and add acceptance criteria. Expect 5-15 back-and-forth exchanges for a substantial PRD.

When you approve the decomposition, the decomposer writes tickets to `orch` via the `ticket-create` custom tool. Each ticket is created in `Draft` state.

### Reviewing Draft Tickets

After the session ends, confirm tickets were created:

```sh
orch tickets list --state Draft
```

Example output:

```
 ID          Title                              State   Risk  PR
 TKT-0001    Scaffold CLI with SQLite schema    Draft   3     -
 TKT-0002    Ticket CRUD commands               Draft   2     -
 TKT-0003    Custom opencode tools              Draft   3     -
```

---

## Section 4: Reviewing Tickets

### List and Inspect

```sh
orch tickets list --state Draft
orch tickets list                    # all states
orch tickets list --json             # JSON output for scripting
```

Show a single ticket:

```sh
orch tickets show TKT-0001
```

Example output:

```
TKT-0001: Scaffold CLI with SQLite schema
State: Draft
Risk: 3
PR: -
Depends on: -
Blocks: TKT-0002, TKT-0003

Description
Implement the orch CLI entry point with click...

Acceptance Criteria
- [ ] orch init creates .orchestra/state.db
- [ ] All tables exist after init
```

### Edit a Ticket

Open the ticket in your `$EDITOR`:

```sh
orch tickets edit TKT-0001
```

This opens a YAML representation. Edit any field and save. To apply from a file:

```sh
orch tickets edit TKT-0001 --from-file updated.yaml
```

**Adjusting risk scores:** `risk_score` is 1-5. Higher scores prompt deeper reviewer scrutiny. Set these before promoting.

**Adjusting dependencies:** Use `orch tickets create --depends-on` when creating, or re-create with dependencies. The router enforces dependency order — a ticket won't dispatch until all its dependencies reach `Done`.

### Promote Tickets to Ready

When tickets look correct, promote them from `Draft` to `To Do`:

```sh
# Promote all Draft tickets at once
orch tickets promote

# Promote a single ticket
orch tickets promote --ticket-id TKT-0001
```

Example output:

```
Promoted 3 tickets to To Do: TKT-0001, TKT-0002, TKT-0003
```

`promote` validates required fields (`title`, `description`, `acceptance_criteria`) and rejects tickets missing them.

### Import Tickets from a File

If you have a YAML file with multiple tickets (e.g., from a planning session):

```sh
orch tickets import tickets.yaml
```

The file must be a YAML list:

```yaml
- title: Implement feature X
  description: |
    Build the X module.
  acceptance_criteria: |
    - [ ] X works end-to-end
  risk_score: 2

- title: Add tests for X
  description: |
    Full test coverage for X.
  acceptance_criteria: |
    - [ ] Coverage >= 90%
  risk_score: 1
  depends_on: [TKT-0001]
```

---

## Section 5: Running the Workflow

### Start the Router

```sh
orch router start
```

Example output:

```
Router started (poll interval: 10s)
```

The router polls every 10 seconds (configurable), picks the highest-priority `To Do` ticket with all dependencies satisfied, and dispatches it. Stop with `Ctrl-C`.

```sh
orch router start --interval 30    # poll every 30 seconds
```

### Backend-Allocated Coder Dispatch

When backend catalogs are enabled, `orch router start` still works in terms of the
logical `coder` role. The difference is that each coder dispatch is allocated onto a
configured physical backend alias at run time.

- Allocation is strict priority-first and deterministic.
- Each active coder dispatch owns its own Backend lease.
- `router.max_parallel_coder_dispatches` defaults to `1`, so existing serial behavior remains unchanged until you opt in.
- When parallelism is greater than `1`, the Router can launch multiple dependency-unblocked coder tickets at once if backend capacity allows it.
- Reviewer and merger dispatch remain outside backend allocation in this milestone.

Parallel safety boundaries in v1:

- Only dependency-unblocked tickets are considered for concurrent coder dispatch.
- Each ticket still gets its own branch and worktree under `.orchestra/worktrees/`.
- Shared git setup is serialized through the Router's git setup gate before agents run.
- Agent sessions overlap only after each ticket's worktree and branch preparation succeeds.
- If worktree setup gates a ticket, that ticket is moved through the existing safety path without cancelling unrelated active coder dispatches.
- Conflict-aware batching is intentionally out of scope for v1. Dependencies plus downstream review and merge gates handle cross-ticket conflicts.

### Backend Observability

Static and runtime backend state is operator-visible:

```sh
orch backends status
orch backends history ORCH-001
orch backends reset-cooldown cloud-free
```

`orch backends status` shows two tables:

- `Backend Status` lists configured metadata such as id, logical agents, priority, concurrency, model, physical alias, quota mode, and Step reserve.
- `Backend Runtime` lists active leases, remaining recorded Steps, cooldown deadline, consecutive failure count, and the last failure reason.

`orch backends history <ticket-id>` shows one ticket's allocation attempts in order:

- `Fallback chain` shows the exact backend-selection sequence.
- `Attempt History` shows backend id, physical alias, outcome, skipped reason, Step usage as `reserved/actual`, and lease status.
- Skipped attempts stay visible, which makes priority and cooldown decisions debuggable.

`orch backends reset-cooldown <backend-id>` removes persisted cooldown state for a configured backend so it becomes eligible immediately on the next allocation pass.

### TUI Active Dispatch View

`orch router start` uses the TUI by default unless you pass `--no-tui`. With parallel
coder dispatch enabled, the TUI becomes the main operator view for active backend work.

- `Router Info` shows an `Active Dispatches` table with ticket, logical agent, backend id, worker alias or model, Step usage, runtime, last tool, and status.
- The lower-left `Event Log` remains global across all dispatches.
- The lower-right `Agent Stream` shows only the currently selected dispatch, so output from multiple coders does not mix.
- `Tab` cycles to the next active dispatch and `Shift-Tab` cycles backward.
- When the selected dispatch finishes, the stream follows another active dispatch if one exists; otherwise the panel returns to an empty state.

### Monitor with `orch status`

In a second terminal:

```sh
orch status
```

Example output:

```
                      Workflow Status
 ID          Title                        State        Risk  PR                         Assignee
 TKT-0001    Scaffold CLI                 Done         3     github.com/org/repo/pr/1   -
 TKT-0002    Ticket CRUD commands         In Progress  2     -                          coder
 TKT-0003    Custom opencode tools        To Do        3     -                          -
```

### Follow the Event Log

```sh
orch log -f
```

Prints state transitions as they happen:

```
2026-05-07T14:01:02Z  TKT-0002  router: To Do → In Progress
2026-05-07T14:03:44Z  TKT-0002  coder: In Progress → Code Review
2026-05-07T14:05:01Z  TKT-0002  reviewer: Code Review → Ready to Merge
2026-05-07T14:05:03Z  TKT-0002  merger: Ready to Merge → Done
```

Show the last N events:

```sh
orch log -n 20
```

### State Transitions

Normal path for a ticket:

```
To Do → In Progress → Code Review → Ready to Merge → Done
```

Rework path (reviewer sends back):

```
Code Review → Rework → In Progress → Code Review → ...
```

After the configured rework loop limit (default 3) the router automatically escalates:

```
Rework → Needs Human Review
```

Agent mapping:

| State            | Agent dispatched |
| ---------------- | ---------------- |
| `To Do`          | coder            |
| `Rework`         | coder            |
| `Code Review`    | reviewer         |
| `Ready to Merge` | merger           |

### When the Webhook Fires

If `webhook.url` is configured in `.orchestra/config.toml`, the router sends a POST request when a ticket enters `Needs Human Review` or `Human Merge`:

```json
{
  "ticket_id": "TKT-0002",
  "title": "Ticket CRUD commands",
  "old_state": "Rework",
  "new_state": "Needs Human Review",
  "risk_score": 2,
  "linked_pr": null
}
```

The router tries once and moves on (it does not block the polling loop). Check `.orchestra/logs/TKT-0002.log` to understand why escalation happened.

### What Normal Execution Looks Like

A healthy run for a low-complexity ticket takes 5-15 minutes end to end:

1. Router picks ticket, moves to `In Progress`, creates `git worktree add .orchestra/worktrees/TKT-0002 -b ticket/TKT-0002`
2. `opencode run --agent coder --dir .orchestra/worktrees/TKT-0002` runs; agent reads the ticket, implements, runs validation commands, sets state to `Code Review`
3. Router picks it up at next poll, dispatches reviewer
4. Reviewer reads the diff, scores it; if passing, sets state to `Ready to Merge`
5. Router dispatches merger; merger runs `gh pr create` / `gh pr merge`, sets state to `Done`
6. Hindsight retains learnings from the completed ticket

Agent stdout/stderr is written to `.orchestra/logs/<ticket-id>.log`.

---

## Section 6: Human Intervention

### Handling `Needs Human Review`

A ticket reaches `Needs Human Review` when:

- The coder agent exited without completing (non-zero exit or no state transition)
- The rework loop count reached the configured limit without passing review
- The reviewer agent explicitly escalated

**Steps:**

1. Check what happened:

```sh
orch tickets show TKT-0002
cat .orchestra/logs/TKT-0002.log
```

2. Review the branch if code was written:

```sh
git worktree list
cd .orchestra/worktrees/TKT-0002
git log --oneline -10
git diff main
```

3. Fix the issue manually, or edit the ticket to clarify requirements:

```sh
orch tickets edit TKT-0002
```

4. Add a comment documenting what you did:

```sh
orch tickets comment TKT-0002 "Fixed auth import issue manually; acceptance criteria clarified."
```

5. Move the ticket back to `To Do` to re-dispatch, or to `Code Review` if code is ready for review:

```sh
orch tickets move TKT-0002 "To Do"
# or, if you fixed the code yourself:
orch tickets move TKT-0002 "Code Review"
```

The router will pick it up on the next poll.

### Handling `Human Merge`

`Human Merge` means the merger agent could not merge the PR automatically (merge conflicts, branch protection rules, required reviews, etc.).

1. Check the ticket and logs:

```sh
orch tickets show TKT-0002
cat .orchestra/logs/TKT-0002.log
```

2. Resolve conflicts or address required checks manually:

```sh
cd .orchestra/worktrees/TKT-0002
git rebase main
# resolve conflicts
git push --force-with-lease origin ticket/TKT-0002
```

3. Merge via `gh` CLI or the GitHub UI:

```sh
gh pr merge TKT-0002 --squash --delete-branch
```

4. Mark the ticket done:

```sh
orch tickets move TKT-0002 Done
```

### Worktree Cleanup

After tickets complete, their worktrees accumulate in `.orchestra/worktrees/`. Prune them:

```sh
orch worktrees prune
```

Output:

```
Prune 4 Done worktree(s)? [y/N]: y
Pruned 4, 0 skipped
```

Skip the confirmation prompt in scripts:

```sh
orch worktrees prune --yes
```

---

## Common Failure Scenarios

### `orch doctor` fails on `database`

The state.db doesn't exist or has missing tables. Run `orch init` to create it:

```sh
orch init
orch doctor
```

### `orch doctor` fails on `gitnexus`

The GitNexus index hasn't been built:

```sh
npx gitnexus analyze .
orch doctor
```

### `orch doctor` fails on `opencode` / `rtk` / `gh`

Install the missing tool and ensure it's on `PATH`. For `gh`, also authenticate:

```sh
gh auth login
```

### Router dispatches the same ticket repeatedly

The ticket state is stuck in `In Progress` from a previous crashed session. Reset it:

```sh
orch tickets move TKT-0002 "To Do"
```

### Agent exits immediately with no changes

Check the agent log:

```sh
cat .orchestra/logs/TKT-0002.log
```

Common causes: missing MCP server (Hindsight/Serena not running), `ORCH_DB_PATH` not set in the agent environment, or the ticket has no `description`.

### Webhook not firing

Verify `webhook.url` is set in `.orchestra/config.toml` (not just the global config). The URL must be reachable from the machine running the router.

### Rework loops not stopping

The router escalates after `router.max_rework_loops` rework loops. If the reviewer keeps sending back due to a persistent code quality issue, inspect the reviewer log, edit the ticket's acceptance criteria to be more precise, reset `rework_loop_count` manually via direct DB edit, and move back to `To Do`.

---

## Quick Reference

```sh
# Setup
orch init                                  # initialize repo
orch doctor                                # verify environment

# Tickets
orch tickets list                          # all tickets
orch tickets list --state Draft            # filter by state
orch tickets show TKT-0001                 # show detail
orch tickets edit TKT-0001                 # edit in $EDITOR
orch tickets move TKT-0001 "To Do"         # change state
orch tickets comment TKT-0001 "message"    # add comment
orch tickets promote                       # Draft → To Do (all)
orch tickets promote --ticket-id TKT-0001  # promote one
orch tickets import tickets.yaml           # bulk import

# Workflow
orch router start                          # start the agent loop
orch status                                # dashboard
orch log -f                                # live event stream
orch log -n 50                             # last 50 events

# PRs
orch pr create TKT-0001                    # create PR (agents do this)
orch pr update TKT-0001                    # push updates

# Cleanup
orch worktrees prune                       # remove Done worktrees
orch worktrees prune --yes                 # skip confirm
```
