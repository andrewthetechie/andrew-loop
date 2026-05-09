# orch — Agent Developer Workflow

A deterministic orchestration system for solo developers. Write a PRD, decompose it into tickets, then hand off implementation to an automated pipeline that handles coding, code review, security audit, and merge.

## How it works

A developer writes a [PRD](https://en.wikipedia.org/wiki/Product_requirements_document) and runs it through the decomposer to produce a set of well-specified tickets. The `orch router` then polls for routable tickets and dispatches the correct specialist agent to each one using [opencode](https://opencode.ai) as the agent harness.

PRD → decomposer → tickets → router → coder → reviewer → merger → merged PR

Each ticket runs in its own git worktree. State (tickets, events, logs) lives in `~/.local/share/orch/{repo-id}/` — outside the target repository to survive any git operation.

## Requirements

- Python 3.14 + [uv](https://docs.astral.sh/uv/)
- [opencode](https://opencode.ai) (`bun add -g opencode-ai`)
- [rtk](https://github.com/rtk-ai/rtk)
- [gh](https://cli.github.com/) (GitHub CLI)
- [Serena](https://github.com/oraios/serena) (`uv tool install serena-agent`)
- [GitNexus](https://gitnexus.dev) (`npm install -g gitnexus`)
- Node.js 22 + Bun

Run `orch doctor` to verify all dependencies are present and configured.

## Capabilities

- **Ticket lifecycle** — To Do → In Progress → Code Review → Ready to Merge → Done, with Rework and Needs Human Review escalation paths
- **Dependency-aware routing** — respects ticket DAG, priority-scores by downstream impact
- **Baseline validation gate** — runs configured validators before each coder dispatch; blocks on pre-existing failures
- **Pre-dispatch validation** — runs validators before reviewer/merger and includes results in the dispatch payload, saving tokens
- **Merge conflict detection** — reviewer and coder both check and reject/resolve merge conflicts before proceeding
- **Hindsight integration** — retains ticket outcomes, review findings, and validation failures to a memory bank that improves future agent runs
- **TUI** — split-pane terminal UI with live ticket status, router state, agent info, and context usage (`orch router start --tui`)
- **Manual approval mode** — pause before each dispatch to review and approve (`-m` flag)

## Setup

See [docs/workflow.md](docs/workflow.md) for installation, configuration, and first-run instructions.
