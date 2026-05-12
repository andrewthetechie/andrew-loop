# PRD: Implementation Coordinator And Hidden Subagents

## Summary

Change the router-visible `coder` from a single-session implementation worker into an orchestration-first implementation coordinator that can selectively delegate work to hidden OpenCode subagents. The workflow state machine stays the same: the router still dispatches `coder`, `reviewer`, and `merger`, and the independent review gate remains intact.

The goal is to reduce parent-agent context growth, improve task success on context-heavy tickets, and let implementation be split into smaller bounded units without turning subagents into router-visible workflow items.

## Problem

The current router-visible `coder` owns the entire implementation flow in one session:

- read the full dispatch payload
- inspect the codebase
- implement changes
- validate
- commit and push
- create or update the pull request
- move the ticket to `Code Review`

This works for straightforward tickets, but larger tickets push the implementation context into the 60-75k range. As context grows, the agent becomes less reliable, has a harder time keeping a compact plan, and spends more budget re-reading or re-holding context that could be delegated.

The project already has strong lifecycle boundaries:

- the `Router` owns dispatch and ticket progression
- the `Reviewer` is an independent review gate
- the `Merger` is a final scope-match gate

What is missing is a way for the implementation phase to split its own work into smaller internal units without changing the outer workflow model.

## Goals

- Reduce context pressure on the router-visible implementation agent.
- Allow implementation work to be split into smaller bounded units inside one ticket.
- Preserve the independent review gate as a separate lifecycle step.
- Preserve the current router and ticket state machine.
- Keep irreversible workflow actions accountable to one top-level role.
- Support a minimal adaptive model policy so expensive reasoning is used only where it adds value.

## Non-Goals

- Introduce router-visible child tickets or subtask states.
- Collapse review into the implementation phase.
- Allow multiple write-capable helpers to edit the same ticket in parallel initially.
- Redesign the `reviewer` or `merger` roles.
- Build subprocess-based subagent orchestration as the primary path.
- Create a general-purpose multi-agent planner for every agent role in the system.

## Users

- Solo developer running `orch` on a PRD-backed ticket queue.
- Router-visible `coder` acting as implementation coordinator.
- Hidden helper agents used only within an implementation session.

## User Stories

- As a solo developer, I want large tickets to be broken into smaller internal units so implementation is more reliable.
- As the router-visible implementation agent, I want to delegate narrow search or editing tasks without giving up ownership of the ticket.
- As the reviewer, I want implementation to arrive as one coherent PR without losing the independent review boundary.
- As a future maintainer, I want the outer ticket lifecycle to stay stable even if implementation becomes internally multi-agent.

## Product Decisions

### 1. Keep the existing workflow boundary

- `reviewer` remains an independent review gate.
- Internal helper agents do not replace or inline review.
- The router still sees only one parent ticket and one router-visible implementation role.

### 2. Repurpose the router-visible `coder`

- Keep the public workflow identity named `coder`.
- Change its behavior to an orchestration-first implementation coordinator.
- The coordinator owns ticket-level responsibility, planning, integration, validation, commit, push, PR update, and ticket transition.

### 3. Use hidden native OpenCode subagents

- Prefer OpenCode native subagents via the Task tool and `mode: subagent`.
- Use `hidden: true` for internal helpers so they are not part of the public workflow surface.
- Keep `opencode run --agent ... --model ...` as a subprocess fallback only for headless or unsupported cases.

### 4. Start with three helper types

- `leaf-coder`: write-capable implementation helper for a bounded change slice.
- `codebase-scout`: read-only helper for search, context gathering, symbol tracing, pattern lookup, and impact lookup.
- `patch-reviewer`: read-only helper that critiques a proposed diff before the independent reviewer gate.

### 5. Enforce single-writer delegation

- Only one write-capable `leaf-coder` may edit the ticket worktree at a time.
- Other helpers must remain read-only.
- Parallel write-capable helpers are out of scope for the first version.

### 6. Keep workflow mutation with the coordinator

- Hidden helpers do not commit.
- Hidden helpers do not push.
- Hidden helpers do not create or update PRs.
- Hidden helpers do not write authoritative ticket comments.
- Hidden helpers do not move ticket state.

The router-visible coordinator is the sole owner of irreversible workflow actions for the parent ticket.

### 7. Start with direct worktree delegation

- The active write-capable helper edits the shared ticket worktree directly.
- The coordinator inspects the resulting diff, integrates glue code if needed, validates, and performs the workflow mutation steps.
- Patch-return handoff is not the default first implementation.

### 8. Make delegation conditional

The coordinator should not always delegate. It should stay local for trivial work and invoke helpers only when the extra coordination cost is justified.

Initial delegation trigger policy:

- delegate when the ticket likely spans multiple files or layers
- delegate when the coordinator cannot keep a short local plan after initial inspection
- delegate when the work cleanly separates into search and edit phases

Do not delegate for:

- straightforward rework
- small single-file fixes
- obvious mechanical edits

### 9. Keep model routing simple

Use a minimal adaptive policy:

- `reviewer` and `decomposer` remain reasoning-optimized
- `leaf-coder` stays cost-optimized
- the coordinator may use a cheap or reasoning-optimized model depending on ticket complexity

Do not hardwire a complex vendor-specific routing matrix in the first version.

## Functional Requirements

### Coordinator behavior

- The router-visible `coder` must read the ticket dispatch payload and decide whether to delegate.
- The coordinator must retain ownership of the full ticket outcome even when helpers are used.
- The coordinator must maintain a short explicit local plan before invoking helpers.
- The coordinator must validate the final integrated result before commit.

### Helper agent definitions

- The system must define hidden OpenCode subagents for `leaf-coder`, `codebase-scout`, and `patch-reviewer`.
- Each helper must have its own prompt.
- Each helper must have role-specific permissions.
- The coordinator must only have task permission to helpers that are part of this design.

### Permissions model

- `leaf-coder` must be allowed to read and edit the ticket worktree.
- `leaf-coder` must not own commit, PR, or ticket-transition actions.
- `codebase-scout` must be read-only.
- `patch-reviewer` must be read-only.
- `reviewer` remains a separate router-dispatched role, not an internal helper.

### Routing and lifecycle

- The router must continue dispatching the same visible roles by ticket state.
- The ticket lifecycle must remain unchanged in the first version.
- Hidden helpers must not create router-visible subtasks or ticket states.

### Workspace behavior

- Hidden helpers must operate within the parent ticket worktree.
- Only one write-capable helper may edit at a time.
- The coordinator must inspect the resulting changes before validation and commit.

### Fallback behavior

- If native subagent delegation is unavailable, the coordinator may fall back to a subprocess path.
- The subprocess path must remain secondary and explicit.
- The existence of a fallback must not reshape the primary design around shelling out.

### Observability

- The system should record enough information to understand when delegation was used and what helper roles participated.
- The design should reuse existing storage such as `subtask_context` where possible instead of inventing router-visible child workflow objects immediately.
- Metrics should remain understandable at the parent ticket level.

## Acceptance Criteria

- The router still dispatches `coder`, `reviewer`, and `merger` exactly as before.
- A delegated implementation ticket can be completed without introducing new ticket states.
- The router-visible `coder` can invoke hidden OpenCode helpers through native Task-based delegation.
- `leaf-coder`, `codebase-scout`, and `patch-reviewer` exist as distinct hidden helper definitions with separate prompts and permissions.
- Only one write-capable helper can edit the worktree at a time.
- Hidden helpers cannot perform the authoritative commit, push, PR update, or ticket state transition for the parent ticket.
- The independent review gate remains a separate `reviewer` dispatch after coding completes.
- Trivial tickets can still be completed by the coordinator without delegation.
- Delegation can be triggered for multi-file or context-heavy tickets using the documented simple heuristic.

## Constraints

- Must fit the current router-centric workflow.
- Must fit the current OpenCode-based harness model.
- Must not require a database redesign before proving value.
- Must not depend on GitHub review-state semantics for correctness.
- Must preserve deterministic router ownership of the public ticket lifecycle.

## Risks

- The coordinator may still accumulate too much context if it delegates too late or integrates too much code itself.
- Native subagent behavior may impose permission or session constraints not yet reflected in the current agent config.
- Direct shared-worktree editing can create confusion unless the single-writer rule is enforced clearly.
- If helper prompts are weak, the system may add coordination overhead without improving outcomes.
- Minimal model routing may need revision once real delegation metrics exist.

## Rollout Plan

### Phase 1: Hidden helper scaffolding

- Add hidden helper definitions and prompts.
- Add task permissions for the router-visible coordinator.
- Keep the current `coder` workflow intact except for the ability to delegate.

### Phase 2: Coordinator prompt redesign

- Rewrite the router-visible `coder` prompt around orchestration-first behavior.
- Add the conditional delegation heuristic.
- Preserve final validation, commit, PR, and ticket-transition ownership in the coordinator.

### Phase 3: Observability

- Capture delegation events and helper outputs in existing or minimal new storage.
- Record which helper roles were used for a ticket.
- Compare delegated versus non-delegated outcomes and token usage.

### Phase 4: Hardening

- Tune helper prompts and permissions.
- Reassess when to use cheap versus reasoning-optimized coordinator models.
- Decide whether subprocess fallback is needed in practice.

## Out Of Scope Follow-Ups

- Parallel write-capable helpers.
- Router-visible child subtasks.
- Reviewer delegation redesign.
- Automatic helper-selection scoring based on historical metrics.
- Full cross-ticket multi-agent coordination.
