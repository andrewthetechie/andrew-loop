# Agent System

An automated developer workflow pipeline. A solo developer writes a PRD, decomposes it into tickets, and hands off implementation to an agent pipeline that handles coding, review, security audit, and merge autonomously.

## Language

**Ticket**:
A single tracked unit of work in the pipeline — created by the decomposer or human, stored in SQLite, progressed through the lifecycle by agents. Contains title, description, acceptance criteria, file paths, risk score, dependencies, and linked PR.
_Avoid_: issue (reserved for the mattpocock skill ecosystem's issue tracker convention)

**Router**:
The deterministic polling loop in `orch` that finds routable tickets and dispatches the correct specialist agent. Owns dispatch decisions, priority scoring, and dependency enforcement. Contains no LLM.
_Avoid_: scheduler, dispatcher (router is the canonical term)

**Dispatch payload**:
The rich prompt assembled by the router and passed to the agent harness. Includes full ticket data, acceptance criteria, comments, rework instructions, validation commands, and workflow instructions. Designed to minimize agent tool calls.

**Agent harness**:
The subprocess invoked by the router to run an LLM agent session. Accepts CLI args (agent type, worktree dir, model, step budget), creates a session with custom tools and the agent's system prompt, streams structured JSON events on stdout for TUI consumption, and enforces a step budget. Currently implemented as a Pi SDK wrapper in `src/harness/`.
_Avoid_: runtime, executor, runner (overloaded)

**Rework instruction**:
A structured comment written by a review agent on a ticket when moving it to `Rework`. Contains explicit code patches that a local model can apply mechanically. Written to the ticket (not just PR comments) so the router can include it in the dispatch payload.

**Review epoch**:
The period between human interventions on a ticket. The automated rework loop count resets when a human moves a ticket out of `Needs Human Review`. Max 3 automated rework loops per epoch.

**Risk score**:
A 1-5 rating reflecting security concerns and breakage potential. Assigned during decomposition, confirmed by human. Gates merge (1-3 automated, 4-5 human), gates security review (1-2 skip, 3+ require), and influences priority.

**PRD** (Product Requirements Document):
A GitHub issue containing the full specification for a cohesive set of work. Created via `orch decompose <file.md>`. Serves as the anchor for a feature cycle — its GitHub issue number determines the feature branch name and scopes which tickets the router will work.
_Avoid_: epic, sprint, milestone

**Feature branch**:
The git branch corresponding to a PRD, named `issue-{github_issue_number}` (e.g. `issue-14`). All ticket branches fork from this branch; all ticket PRs target this branch. Merging the feature branch into `main` is a human step performed after all tickets are Done.
_Avoid_: base branch (overloaded), integration branch

**Issue mode**:
The default router operating mode. The router works all tickets belonging to one GitHub issue (PRD) until they are all Done or all remaining tickets are blocked. Entered via `--issue N` or prompted on startup. A ticket's `issue_id` field determines membership.

**All-issues mode**:
An optional router operating mode (`--all-issues`). The router works tickets across all open PRDs in issue-number order — completing or blocking each issue's tickets before moving to the next.

**Base branch**:
The feature branch targeted by ticket PRs. Derived from the ticket's `issue_id` as `issue-{issue_id}`. Replaces the previous hardcoded `main` target. The human merges the feature branch into `main` to close a PRD cycle.

**Deterministic layer**:
The `orch` CLI and its components (router, config, PR tools, validation runner). Handles all data gathering, assembly, and state management. The LLM agents are execution layers that receive structured payloads from the deterministic layer.

**Cost-optimized model**:
A Qwen-class LLM used for implementation tasks (coder) and constrained judgment tasks (merger scope check). Treated like a talented junior engineer — executes discrete, well-described tasks. Not expected to handle complex reasoning, rebases, or vague instructions.

**Reasoning-optimized model**:
A Claude/GLM-class LLM used for tasks requiring deep reasoning (code review, security review, decomposition). Responsible for producing output that a cost-optimized model can act on mechanically.

**Pre-check gate**:
Deterministic validation performed by the router before dispatching an expensive agent. Includes mergeability, CI status, review decision parsing, risk-score gating, and pre-run validation. Failures short-circuit dispatch — the router moves the ticket to the appropriate state (Rework, Human Merge, Needs Human Review) without invoking an LLM.

## Relationships

- A **Ticket** has zero or many dependencies on other **Tickets** (DAG, no cycles)
- A **Ticket** has one **Risk score**
- A **Ticket** belongs to one **Review epoch** at a time
- The **Router** dispatches agents based on **Ticket** state and dependencies
- The **Router** constructs a **Dispatch payload** for each agent invocation
- A **Rework instruction** is attached to a **Ticket** by a review agent
- A **Ticket** belongs to one **PRD** via `issue_id`
- All ticket PRs target the **Feature branch** derived from the ticket's **PRD**
- The **Router** in **Issue mode** scopes work to one **PRD**'s tickets at a time

## Flagged Ambiguities

_(none currently)_
