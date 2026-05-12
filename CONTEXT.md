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

**Independent review gate**:
The workflow boundary where a **Reviewer** evaluates a **Ticket** after coding as a separate lifecycle step, not as a subtask inside implementation.
_Avoid_: inline review, self-review, embedded review

**Internal delegation**:
The practice of letting an implementation agent spawn helper agents inside a single **Ticket** without exposing those helpers as separate workflow items to the **Router**.
_Avoid_: child ticket, tracked subtask, router-visible subagent

**Implementation coordinator**:
The top-level implementation agent that owns one **Ticket**, decomposes the work internally, delegates to helper agents, and remains accountable for final validation and handoff.
_Avoid_: coder, worker, reviewer

**Leaf coder**:
An implementation-only helper agent that edits code for a bounded slice of a **Ticket** but does not own workflow transitions or independent review decisions.
_Avoid_: coordinator, reviewer, merger

**Orchestration-first coordinator**:
An **Implementation coordinator** that stays mostly focused on scoping, delegating, integrating, validating, and handing off, while only writing glue or fixup code when necessary.
_Avoid_: hands-on primary coder, full-context implementer

**Router-visible coder**:
The existing router-dispatched implementation role named `coder`. It is retained as the public workflow identity but now behaves as the **Implementation coordinator**.
_Avoid_: leaf coder, reviewer

**Native subagent delegation**:
Delegation performed inside a running OpenCode agent through the built-in Task tool and `mode: subagent` agent definitions, rather than by launching a separate external workflow.
_Avoid_: subprocess delegation, shell-spawned helper

**Subprocess fallback**:
Headless delegation that launches `opencode run` as a separate process when **Native subagent delegation** is unavailable or unsuitable.
_Avoid_: primary delegation path, in-session subagent

**Single-writer delegation**:
An **Internal delegation** rule where only one write-capable **Leaf coder** edits the ticket workspace at a time, while other helpers remain read-only.
_Avoid_: parallel writers, concurrent code-editing helpers

**Coordinator-owned workflow mutation**:
The rule that only the **Router-visible coder** acting as the **Implementation coordinator** may perform validation signoff, commits, pushes, pull-request updates, ticket comments, or ticket state transitions for the parent **Ticket**.
_Avoid_: helper-owned commit, subagent workflow transition, delegated PR action

**Direct worktree delegation**:
An **Internal delegation** pattern where the active write-capable **Leaf coder** edits the shared ticket worktree directly, and the **Implementation coordinator** inspects and integrates those changes afterward instead of applying returned patches manually.
_Avoid_: patch-only handoff, coordinator-applied subagent diff

**Conditional delegation**:
An orchestration policy where the **Router-visible coder** may complete trivial tickets alone, but invokes helper agents when ticket complexity, uncertainty, or scope size justifies the extra coordination cost.
_Avoid_: always delegate, never delegate

**Delegation trigger policy**:
A simple heuristic for **Conditional delegation**: delegate when the ticket likely spans multiple files or layers, when the coordinator cannot keep the relevant plan short after initial inspection, or when the work cleanly separates into search and edit phases.
_Avoid_: opaque score, premature routing formula

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
- The **Independent review gate** sits between `Code Review` and `Ready to Merge`
- **Internal delegation** happens within one **Ticket** and does not create new **Ticket** states
- The **Implementation coordinator** may use **Internal delegation** to assign bounded work to one or more **Leaf coders**
- The **Implementation coordinator** owns final validation and handoff for the parent **Ticket**
- An **Orchestration-first coordinator** minimizes direct coding to keep parent-ticket context small
- The **Router-visible coder** is the current public name for the **Implementation coordinator**
- **Native subagent delegation** is the preferred mechanism for **Internal delegation**
- **Subprocess fallback** is reserved for headless or non-native delegation cases
- **Single-writer delegation** is the initial safety rule for any write-capable **Leaf coder**
- **Coordinator-owned workflow mutation** keeps irreversible workflow actions with the **Router-visible coder**
- **Direct worktree delegation** is the initial edit-integration model for the write-capable **Leaf coder**
- **Conditional delegation** prevents helper overhead on trivial tickets while allowing deeper decomposition on context-heavy work
- The **Delegation trigger policy** governs when the **Router-visible coder** stays local versus invoking helper agents

## Example Dialogue

> **Dev:** "Can the **Coder** call the **Reviewer** before handing the ticket back?"
> **Domain expert:** "No — the **Independent review gate** stays a separate workflow step owned by the **Router**."
>
> **Dev:** "If the **Coder** spawns helpers, does the **Router** track them?"
> **Domain expert:** "No — that is **Internal delegation**, and the **Router** still only tracks the parent **Ticket**."
>
> **Dev:** "Who owns the ticket if helpers are writing code?"
> **Domain expert:** "The **Implementation coordinator** owns the ticket; **Leaf coders** only handle bounded implementation slices."
>
> **Dev:** "Does the coordinator still write lots of code?"
> **Domain expert:** "No — it is an **Orchestration-first coordinator** and only steps into glue or fixup work when integration requires it."
>
> **Dev:** "Do we need a custom tool that shells out to `opencode run` for subagents?"
> **Domain expert:** "Not by default — prefer **Native subagent delegation** and keep **Subprocess fallback** only for headless cases."
>
> **Dev:** "Can two helper coders edit the same ticket in parallel?"
> **Domain expert:** "Not initially — use **Single-writer delegation** so only one write-capable **Leaf coder** edits at a time."
>
> **Dev:** "When the router dispatches `coder`, is that still the old single worker?"
> **Domain expert:** "No — the **Router-visible coder** now refers to the **Implementation coordinator**, while `leaf-coder` is the hidden write helper."
>
> **Dev:** "Can the hidden writer commit and move the ticket after it edits the files?"
> **Domain expert:** "No — use **Coordinator-owned workflow mutation** so only the **Router-visible coder** performs commits, PR updates, and ticket transitions."
>
> **Dev:** "Should the hidden writer return a patch for the coordinator to apply?"
> **Domain expert:** "No — start with **Direct worktree delegation** so the active **Leaf coder** edits the shared worktree directly."
>
> **Dev:** "Should the coordinator always use helpers?"
> **Domain expert:** "No — use **Conditional delegation** so trivial tickets can stay local and only heavier work gets split."
>
> **Dev:** "What actually triggers helper use?"
> **Domain expert:** "Use the **Delegation trigger policy**: delegate for multi-file work, overloaded local context, or clean search-versus-edit splits."

## Flagged Ambiguities

_(none currently)_
