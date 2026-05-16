# PRD: Local Coder Optimization

Status: needs-triage

> Originally scoped around local Ryzen coder reliability. Coder backend selection (model, hardware, context cap, provider config, parallel dispatch, leases, quotas, cooldowns, failover) now lives in the **Backend allocator** PRD. This PRD covers the surrounding quality pipeline that applies to any **Coder backend**: hidden-helper configuration, the **Patch-review handoff gate**, deterministic **Dispatch payload** pruning, and hidden-helper concurrency under parallel dispatch.

## Related Work

- **`docs/prd/priority-allocated-coder-backends.md`** — owns **Coder backend** selection, parallel dispatch, leases, quotas, cooldowns, failover, and the **Backend catalog**.
- **`docs/adr/0002-logical-agents-with-priority-allocated-coder-backends.md`** — records the logical/physical agent split and priority-first allocation decision.
- **`docs/adr/0003-patch-review-handoff-gate-universal-across-coder-backends.md`** — records the decision that the **Patch-review handoff gate** is universal across **Coder backends**.

This PRD assumes the backend-allocator work is implemented and defers to it on any **Coder backend**, coder model selection, or parallel dispatch concern.

## Problem Statement

The **Router-visible coder** runs on whichever **Coder backend** the **Backend allocator** selects. That may be a local Ryzen Qwen Coder-class machine, a MacBook Pro, a DGX Spark, or a free cloud provider. Coder model quality varies across backends, but even with a strong backend the loop benefits from disciplined surrounding behavior: hidden-helper configuration that does not silently inherit the coder's backend-selected model, a deterministic pre-handoff quality gate so the **Independent review gate** is not wasted on avoidable mistakes, and **Dispatch payload** pruning that keeps the ticket contract intact without growing unboundedly.

The original motivation for this PRD was that lower-quality local coder models are more likely to skip optional prompt instructions, so the loop needs deterministic safeguards rather than prompt-level pleas. That motivation still drives the **Patch-review handoff gate** design. The reframed scope makes the same gate universal: every coder tier benefits from a conservative pre-handoff check on nontrivial diffs, and trivial-skip criteria already filter out the easy cases.

The user values slow and steady progress over throughput. The system should preserve high-quality incoming tickets, avoid unnecessary scout usage, and use deterministic enforcement where prompt-level instructions are unreliable.

## Solution

Optimize the coder side of the loop around independent hidden-helper model config, a conservative **Patch-review handoff gate** at the **Code Review** boundary, deterministic **Dispatch payload** pruning, and a small concurrency cap for Spark-backed hidden helpers under **Parallel coder dispatch**.

The **Router-visible coder** remains the **Implementation coordinator** and continues to run on whichever **Coder backend** the **Backend allocator** selects. The **Leaf coder** inherits the parent coder's selected backend through the **Agent harness** — it is not independently allocated. Hidden helpers (**patch-reviewer**, **codebase-scout**) are not **Coder backends** and not **Logical agents**; they are configured statically on a DGX Spark profile through hidden-helper config.

The system does not change the ticket lifecycle, router-visible states, or **Independent review gate**. Remote reviewers such as GLM can remain in use. The optimization makes the coder handoff reliable enough to feed the existing **Reviewer** and **Merger** stages with fewer avoidable rework loops, regardless of which **Coder backend** produced the diff.

Dispatch payload pruning is deterministic and **Coder backend**-agnostic. The pruner runs before backend allocation and sizes for the tightest expected coder context budget. The goal is not LLM summarization; the goal is to keep the ticket contract intact while dropping stale comments, redundant lifecycle chatter, oversized memory sections, and historical validation output that does not affect the current attempt.

## User Stories

1. As a solo developer, I want lower-tier **Coder backends** to be protected by deterministic gates, so that prompt compliance failures do not silently reduce code quality.
2. As a solo developer, I want the **Independent review gate** to remain available on remote GLM or another strong model, so that coder-side discipline does not force a full remote-model removal.
3. As a solo developer, I want hidden helpers (patch-reviewer, codebase-scout) to receive higher-capacity reasoning compute, so that pre-handoff critique and rare codebase synthesis use the strongest local reasoning available.
4. As a solo developer, I want **Leaf coder** to remain available for bounded implementation slices, so that the **Implementation coordinator** can keep parent context small on multi-file work.
5. As a solo developer, I want **Leaf coder** usage to trigger patch-review, so that direct worktree delegation receives an extra quality check before handoff.
6. As a solo developer, I want codebase-scout to remain rare, so that high-quality incoming tickets continue to carry **Expected files** and concrete acceptance criteria.
7. As a solo developer, I want patch-reviewer to run for nontrivial diffs, so that coder mistakes are caught before the **Independent review gate** spends a full cycle on them.
8. As a solo developer, I want trivial single-file low-risk tickets to avoid unnecessary patch-review overhead, so that the conservative policy still has a practical escape hatch.
9. As the **Implementation coordinator**, I want per-helper model configuration, so that hidden helper roles do not accidentally inherit the coder's backend-selected model.
10. As the **Implementation coordinator**, I want role-specific context limits for hidden helpers, so that each helper receives enough context for its job without triggering unnecessary KV-cache pressure.
11. As the **Implementation coordinator**, I want **Leaf coder** to inherit the active **Coder backend**, so that direct worktree delegation runs on the same physical inference target as the coordinator without a separate allocation step.
12. As the **Implementation coordinator**, I want a clear rejection when **Code Review** handoff requires patch-reviewer output, so that I know exactly how to unblock the ticket.
13. As the **Implementation coordinator**, I want **Dispatch payload** comments to include only current actionable context, so that I do not spend context budget interpreting stale lifecycle history.
14. As the **Implementation coordinator**, I want current rework instructions preserved, so that follow-up work can be applied mechanically.
15. As the **Implementation coordinator**, I want human comments preserved after the latest relevant review or router gate, so that human decisions are not pruned away.
16. As the **Implementation coordinator**, I want validation commands preserved exactly, so that local models run the intended project checks.
17. As the **Implementation coordinator**, I want historical validation output pruned unless it is relevant to the current state, so that old failures do not distract the current attempt.
18. As the **Implementation coordinator**, I want hidden-helper concurrency bounded, so that **Parallel coder dispatch** does not overwhelm the single Spark machine.
19. As the patch-reviewer, I want a compact but sufficient view of the current diff and relevant ticket contract, so that I can critique implementation risk without taking workflow ownership.
20. As the patch-reviewer, I want to remain read-only, so that quality review cannot mutate the worktree or ticket lifecycle.
21. As the **Reviewer**, I want coder handoffs to include fewer avoidable mistakes, so that review cycles focus on real design and behavior issues.
22. As the **Reviewer**, I want the **Independent review gate** to remain separate from the **Patch-review handoff gate**, so that pre-handoff critique does not replace authoritative review.
23. As the **Router**, I want the visible ticket lifecycle unchanged, so that this PRD does not require new workflow states.
24. As the **Router**, I want deterministic handoff checks at the **Code Review** boundary, so that the system can enforce conservative patch-review policy before reviewer dispatch.
25. As the **Router**, I want compact payload construction to be deterministic and testable, so that payload shape is predictable and debuggable.
26. As the **Router** operator, I want graceful stop to wait for in-flight hidden-helper sessions, so that patch-reviewer and codebase-scout output is not lost mid-dispatch.
27. As the **Router** operator, I want "waiting for hidden-helper slot" to be visible in the **Active dispatch view**, so that hidden-helper concurrency limits do not look like a hang.
28. As a maintainer, I want hidden-helper config to be independent of coder backend selection, so that future **Coder backend** additions do not silently change patch-reviewer or codebase-scout behavior.
29. As a maintainer, I want the first version to avoid LLM-based payload summarization, so that pruning cannot hallucinate or drop subtle requirements.
30. As a maintainer, I want the first version to avoid a router-visible Patch Review state, so that the workflow does not grow extra lifecycle complexity prematurely.
31. As a maintainer, I want patch-review policy to be conservative at first, so that quality is favored while real usage data accumulates.
32. As a maintainer, I want tests around hidden-helper config, the **Patch-review handoff gate**, hidden-helper concurrency, graceful stop, and payload pruning, so that future changes do not regress coder-handoff reliability.

## Implementation Decisions

### Scope boundary with backend-allocator

- Coder and **Leaf coder** model assignment is owned by the **Backend allocator** and the **Backend catalog**, not by this PRD.
- Coder context and output window caps are owned by the **Backend catalog**, not by this PRD.
- OpenAI-compatible provider endpoint configuration for the coder is owned by the **Backend catalog**, not by this PRD.
- **Parallel coder dispatch**, leases, quotas, cooldowns, and pre-execution backend failover are owned by the **Backend allocator**.

### Lifecycle and identity preservation

- Preserve the existing **Router**-visible lifecycle. The **Router** still dispatches coder, reviewer, and merger based on ticket state.
- Preserve the **Router-visible coder** identity. The coder continues to mean the **Implementation coordinator**, not a leaf worker.
- Keep remote reviewers in scope. This PRD does not require replacing GLM or other remote reasoning models for review.
- Do not add a router-visible Patch Review ticket state.
- Keep workflow mutation with the **Implementation coordinator**. Hidden helpers cannot own final validation signoff, commit, push, PR update, or ticket transition.

### Hidden-helper configuration

- Add first-class model configuration for hidden helper agents. Hidden helpers must not inherit a coder's backend-selected model by accident.
- Configure patch-reviewer on a DGX Spark profile.
- Configure codebase-scout on a DGX Spark profile, while keeping scout usage rare and escalation-driven.
- Use role-specific context limits for hidden helpers: roughly 190k context for patch-reviewer and codebase-scout.
- Keep hidden-helper output limits modest. Helpers should produce concise critiques and synthesis rather than long speculative narratives.
- Patch-reviewer and codebase-scout are not **Coder backends** and not **Logical agents**. They are spawned by the **Implementation coordinator** via **Internal delegation**.
- **Leaf coder** inherits the active **Coder backend** through the **Agent harness**. It does not get an independent model assignment in this PRD.

### Patch-review handoff gate

- Use patch-reviewer as a conservative pre-handoff quality gate for nontrivial diffs.
- Enforce patch-reviewer requirements deterministically at the **Code Review** handoff boundary rather than only through prompt instructions.
- The **Patch-review handoff gate** is universal across **Coder backends**. The gate is not skipped based on backend identity or perceived backend quality (see ADR-0003).
- The gate should reject attempts to move a ticket to **Code Review** when conservative policy requires patch-reviewer output and none has been recorded for the current dispatch attempt.
- The gate should provide an actionable failure message that tells the coder to run patch-reviewer, record the delegation output, and retry the state transition.
- The conservative patch-review policy should require patch review for most nontrivial new work.
- A diff may skip patch-review only when it is clearly trivial: low risk, exactly one changed file, no sensitive path category, no weakened tests, no **Leaf coder** usage, first-pass validation success, and no current rework concern about architecture, behavior, or coverage.
- Sensitive categories include configuration, prompts, database or migrations, router behavior, workflow state, pull request handling, subprocess execution, authentication, security, and persistence.
- The patch-review verdict is scoped to the current dispatch attempt. A verdict from a prior dispatch attempt does not satisfy the gate for a new attempt, because the diff may have changed and re-running patch-reviewer is acceptable cost.
- The patch-review verdict is recorded as a **ticket comment** (consistent with the existing **Rework instruction** pattern), but the gate consults only the current attempt's verdict.
- Keep patch-reviewer read-only. It critiques the proposed diff but does not commit, push, update PRs, comment authoritatively, or move ticket state.

### Dispatch payload pruning

- **Dispatch payload** pruning is deterministic only. Do not use an LLM to summarize the payload in the first version.
- **Dispatch payload** assembly runs before **Backend allocator** selection and does not depend on which **Coder backend** will be chosen. The payload is sized for the tightest expected coder context budget.
- Extract payload pruning into a deep module with a simple interface that receives agent role, ticket state, ticket data, comments, memory context, and validation data, and returns a compact structured payload model or render-ready sections.
- Preserve the Ticket section, acceptance criteria, risk score, linked PR, **Expected files**, test expectations, validation commands, and workflow instructions.
- For coder To Do dispatches, include human comments and the latest actionable router or coder handoff comments, but omit stale approvals, old coder summaries, and routine router status.
- For coder Rework dispatches, include the latest reviewer changes-requested instruction or latest relevant router gate, plus human comments after that point.
- For reviewer dispatches, include the latest coder implementation summary, delegation records, relevant prior review decision for re-review, and the current PR reference.
- Cap Hindsight Context for coder dispatches. Prefer concrete conventions, validation failure patterns, review findings, and similar ticket outcomes over broad historical context.
- Cap validation result output. For reviewer dispatch, include router pre-validation status and useful failure excerpts rather than full historical stdout and stderr.
- Include explicit truncation markers when pruning content. Never silently drop or shorten content in a way that hides that pruning occurred.
- The payload does not include cross-backend failure history. **Backend allocator** observability handles allocation history through its CLI and TUI surfaces.

### Hidden-helper concurrency under parallel dispatch

- Bound hidden-helper concurrency at the **Agent harness** layer with a simple semaphore, separate from the **Backend allocator**.
- Default to 1 concurrent patch-reviewer session and 1 concurrent codebase-scout session, configurable.
- The semaphore is enforced by the **Implementation coordinator**'s harness layer, not by the **Backend allocator**. The **Backend allocator** is reserved for **Logical agents** in the first milestone.
- The **Implementation coordinator** waits on the semaphore before delegating to a hidden helper. Waiting is acceptable cost on a slow-and-steady loop.
- Surface "waiting for hidden-helper slot" as a dispatch status in the **Active dispatch view** so it is not mistaken for a hang.
- **Graceful router stop** waits for in-flight hidden-helper sessions (patch-reviewer, codebase-scout) to finish as part of waiting for the parent **Router-visible coder** dispatch. Hidden helpers are not abandoned mid-session.

## Testing Decisions

- Tests should focus on external behavior and observable contracts, not internal implementation details.
- Hidden-helper model configuration tests should verify that patch-reviewer and codebase-scout receive independent model assignments and that hidden helpers do not inherit a coder's backend-selected model.
- OpenCode generation tests for hidden helpers should verify that generated agent config includes correct hidden-helper models (patch-reviewer, codebase-scout) and their context and output limits.
- The **Patch-review handoff gate** tests should verify that nontrivial coder diffs without patch-reviewer delegation output cannot move to **Code Review**.
- The **Patch-review handoff gate** tests should verify that trivial low-risk single-file changes can move to **Code Review** without patch-reviewer output.
- The **Patch-review handoff gate** tests should verify that **Leaf coder** usage makes patch-reviewer mandatory.
- The **Patch-review handoff gate** tests should verify that sensitive file categories make patch-reviewer mandatory.
- The **Patch-review handoff gate** tests should verify that an actionable rejection message is returned to the coder when patch review is required.
- The **Patch-review handoff gate** tests should verify that a patch-review verdict from a prior dispatch attempt does not satisfy the gate for a new attempt.
- Payload pruning tests should verify coder To Do dispatches keep the ticket contract and remove stale comments.
- Payload pruning tests should verify coder Rework dispatches keep the latest actionable rework instruction and human comments after it.
- Payload pruning tests should verify reviewer dispatches keep implementation summaries and delegation records needed for review.
- Payload pruning tests should verify Hindsight Context caps are applied with visible truncation markers.
- Payload pruning tests should verify validation result output is capped with visible truncation markers.
- Payload pruning tests should verify the payload does NOT include cross-backend failure history.
- Hidden-helper concurrency tests should verify the semaphore enforces the configured maximum concurrent patch-reviewer and codebase-scout sessions (default 1 each), with dispatch A blocking until dispatch B releases the slot.
- **Graceful router stop** tests should verify that an in-flight hidden-helper session blocks the parent dispatch from completing until the helper finishes, and is not abandoned.
- Prompt tests should verify the shared logical coder prompt describes the conservative patch-review policy, rare scout usage, **Leaf coder** usage, and role ownership boundaries.
- Config tests should follow existing agent config and initialization test patterns in the codebase.
- **Router** and tool tests should follow existing ticket state transition, router gate, custom tool, and **Dispatch payload** tests already present in the project.

## Out of Scope

- Eliminating remote LLMs from the full workflow.
- Replacing GLM or other remote reviewer models.
- Redesigning the **Independent review gate** or merger stages.
- Adding a router-visible Patch Review state.
- Creating router-visible child tickets or subtask states.
- Making codebase-scout part of every coder dispatch.
- Using LLM-based payload summarization.
- Parallel write-capable **Leaf coder** sessions.
- Database schema redesign solely for this feature unless the implementation discovers existing delegation records are insufficient.
- Coder and **Leaf coder** model assignment (owned by the **Backend allocator** and **Backend catalog**).
- Coder context window or output caps (owned by the **Backend catalog**).
- OpenAI-compatible provider endpoint configuration for the coder (owned by the **Backend catalog**).
- Allocating patch-reviewer or codebase-scout across multiple backends. This is a possible future extension of the **Backend allocator** to additional roles but is out of scope for this PRD.
- Per-**Coder backend** "trust tier" or per-backend patch-review skip. The **Patch-review handoff gate** is universal across **Coder backends**.
- Per-**Coder backend** **Dispatch payload** variation. Payload pruning is backend-agnostic.
- Persistent patch-review verdict across dispatch attempts. The verdict is scoped to the current attempt.

## Further Notes

This PRD originated alongside a hardware deployment plan that uses a Ryzen AI inference machine for the hot coder path and a DGX Spark for slower, higher-capacity local reasoning helpers. After the backend-allocator work, the Ryzen machine is described as one entry in the **Backend catalog** rather than as a coder-role default in this PRD. The DGX Spark remains directly assigned here for patch-reviewer and codebase-scout because those hidden helpers do not go through the **Backend allocator** in the first milestone.

The user values slow and steady progress over raw speed. This PRD therefore favors conservative quality gates and predictable deterministic pruning over aggressive throughput optimization.

The key quality bet is that incoming tickets remain well defined by decomposition and review policy. Coder-side discipline should not compensate for vague tickets by encouraging frequent codebase-scout usage. If scout usage rises, the likely fix is better **Codebase-grounded decomposition** or richer **Expected files** in the **Dispatch payload**.

The first implementation should be small and measurable: independent hidden-helper model config, the conservative **Patch-review handoff gate**, deterministic **Dispatch payload** pruning, and a hidden-helper concurrency semaphore. More elaborate routing can wait until metrics show where the loop actually fails.
