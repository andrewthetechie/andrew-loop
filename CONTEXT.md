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
The rich prompt assembled by the router and passed to `opencode run`. Includes full ticket data, acceptance criteria, comments, rework instructions, validation commands, and workflow instructions. Designed to minimize agent tool calls.

**Rework instruction**:
A structured comment written by a review agent on a ticket when moving it to `Rework`. Contains explicit code patches that a local model can apply mechanically. Written to the ticket (not just PR comments) so the router can include it in the dispatch payload.

**Review epoch**:
The period between human interventions on a ticket. The automated rework loop count resets when a human moves a ticket out of `Needs Human Review`. Max 3 automated rework loops per epoch.

**Risk score**:
A 1-5 rating reflecting security concerns and breakage potential. Assigned during decomposition, confirmed by human. Gates merge (1-3 automated, 4-5 human), gates security review (1-2 skip, 3+ require), and influences priority.

**Base branch**:
The git branch active when `orch router start` is invoked. All PRs created during that router session target this branch. Supports both feature-branch workflows (agents build sub-tickets, human rolls up to main) and main-branch workflows.

**Deterministic layer**:
The `orch` CLI and its components (router, config, PR tools, validation runner). Handles all data gathering, assembly, and state management. The LLM agents are execution layers that receive structured payloads from the deterministic layer.

**Cost-optimized model**:
A Qwen-class LLM used for implementation tasks (coder, merger scope check). Treated like a talented junior engineer — executes discrete, well-described tasks. Not expected to handle complex reasoning, rebases, or vague instructions.

**Reasoning-optimized model**:
A Claude/GLM-class LLM used for tasks requiring deep reasoning (code review, security review, decomposition). Responsible for producing output that a cost-optimized model can act on mechanically.

## Relationships

- A **Ticket** has zero or many dependencies on other **Tickets** (DAG, no cycles)
- A **Ticket** has one **Risk score**
- A **Ticket** belongs to one **Review epoch** at a time
- The **Router** dispatches agents based on **Ticket** state and dependencies
- The **Router** constructs a **Dispatch payload** for each agent invocation
- A **Rework instruction** is attached to a **Ticket** by a review agent
- All PRs target the **Base branch**

## Flagged Ambiguities

_(none currently)_
