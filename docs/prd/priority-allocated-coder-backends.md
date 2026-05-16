# PRD: Priority-Allocated Coder Backends

Status: needs-triage

## Problem Statement

The local coder optimization plan makes the router-visible coder depend on local and free cloud inference capacity. The user has multiple possible Coder backends: local Ryzen inference, a MacBook Pro, a DGX Spark when idle, and free cloud model providers with low request limits. The current Router treats a workflow role as a single agent configuration and runs one dispatch at a time, so it cannot deterministically allocate coder work across multiple capacity targets, avoid exhausted free quotas, fall back from unavailable machines, or explain which backend ran a ticket.

OpenCode also binds the model at the agent configuration level. That creates a design pressure to create `coder1`, `coder2`, and `coder3` agents, but that would leak infrastructure into the workflow model and conflict with the existing domain language where `coder` is the single router-visible implementation role. The system needs a way to use multiple physical inference targets while preserving one logical router-visible coder.

The user wants deterministic priority-first backend selection, quota-aware use of free cloud resources, safe failure handling, and parallel coder dispatch when multiple dependency-unblocked tickets and multiple backends are available. The system must remain debuggable: an operator should be able to see why a backend was selected, why another was skipped, which tickets are active, and how much quota remains.

## Solution

Introduce a Coder backend allocation system behind the logical router-visible coder. The Router continues to select the logical `coder` agent from ticket state, then asks a Backend allocator for an eligible Coder backend. The selected backend provides a stable physical agent alias for the Agent harness, allowing OpenCode to bind backend-specific models without creating new router-visible workflow roles.

Backend allocation is deterministic and priority-first. The allocator evaluates configured Coder backends in order, skipping disabled, leased, cooled-down, quota-insufficient, unhealthy, or ticket-incompatible backends. It selects the first eligible backend using configured priority and stable tie-breaking. This naturally fills parallel capacity: a high-priority free backend is used first, then local backends are used when the free backend is leased, cooled down, exhausted, or below the required Step reserve.

The system persists Backend state in SQLite: leases, Step reserves, actual Step quota usage, cooldowns, failure counts, and dispatch attempt history. This prevents router restarts from forgetting in-flight work or consumed free-provider quota. Backend definitions live in TOML Backend catalogs, with repo-level enablement, ordering, and overrides.

Parallel coder dispatch becomes an explicit Router capability. The first version enables backend allocation only for the logical coder, while keeping the allocator model generic enough for future logical agents. The Router may dispatch multiple dependency-unblocked coder tickets concurrently, each on a separate Backend lease, while serializing shared git setup and running the expensive agent sessions in parallel.

## User Stories

1. As a solo developer, I want one logical router-visible coder to use multiple inference backends, so that infrastructure choices do not create extra workflow roles.
2. As a solo developer, I want the Router to prefer free cloud coder capacity before local machines, so that limited free resources are used before unlimited but slower local fallback.
3. As a solo developer, I want free-provider daily limits to be respected, so that the Router does not repeatedly dispatch to an exhausted backend.
4. As a solo developer, I want per-minute provider limits to be respected, so that high-throughput providers are not overused in a short burst.
5. As a solo developer, I want unknown-limit providers to remain usable until they return rate-limit errors, so that undocumented free capacity can still contribute.
6. As a solo developer, I want 429 responses to put a backend into cooldown, so that the Router stops wasting attempts on unavailable quota.
7. As a solo developer, I want offline local machines to enter exponential cooldown, so that the Router does not hammer a MacBook or local server that is unavailable.
8. As a solo developer, I want the Router to fall back from a pre-execution backend failure to the next backend, so that transient provider or endpoint failures do not block an otherwise routable ticket.
9. As a solo developer, I want the Router to avoid blind retry after possible worktree mutation, so that backend failover does not duplicate or corrupt partial implementation work.
10. As a solo developer, I want multiple coder tickets to run in parallel when dependencies allow it, so that multiple local and free inference targets can be used at the same time.
11. As a solo developer, I want parallel coder dispatch to respect ticket dependencies, so that dependent tickets are not started before their prerequisites are done.
12. As a solo developer, I want parallel coder dispatch to use per-ticket worktrees, so that simultaneous implementation sessions do not edit the same checkout.
13. As a solo developer, I want the human ticket author or reviewer to remain responsible for dependency accuracy, so that the Router does not need complex conflict prediction in the first version.
14. As a solo developer, I want the Merger and existing PR checks to handle merge conflicts downstream, so that parallel coder dispatch can ship without conflict-aware batching.
15. As a solo developer, I want shared git setup to be serialized, so that parallel dispatch does not create confusing worktree, fetch, rebase, or branch races.
16. As a solo developer, I want agent sessions to run concurrently after setup, so that model runtime is parallelized while shared git operations remain deterministic.
17. As a solo developer, I want the Router to reserve enough Step quota for a full coder loop before dispatch, so that a provider with only a few requests left is not started on work it cannot finish.
18. As a solo developer, I want below-reserve backends to leave circulation until their quota window restores capacity, so that the Router does not gamble on incomplete coder loops.
19. As a solo developer, I want Step quota to approximate request-limited provider usage, so that OpenRouter-style and NIM-style limits are tracked more accurately than full dispatch counts.
20. As a solo developer, I want dispatch-count quotas to remain available as a coarse safety cap, so that backends can still be constrained when step telemetry is unavailable.
21. As a solo developer, I want token quotas to be optional and telemetry-dependent, so that the system does not pretend token accounting is reliable when it is not.
22. As a solo developer, I want backend definitions in a global catalog, so that free cloud and local provider metadata can be shared across repositories.
23. As a solo developer, I want repo-level overrides, so that a specific project can enable, disable, reorder, or change reserves for known backends.
24. As a solo developer, I want deterministic tie-breaking, so that repeated allocation decisions are reproducible and easy to debug.
25. As a solo developer, I want allocation reasons persisted, so that I can understand why a ticket ran on one backend instead of another.
26. As a solo developer, I want a CLI command to view backend status, so that I can inspect leases, quota, cooldowns, and failure state without opening a database.
27. As a solo developer, I want a CLI command to view backend history for a ticket, so that I can debug fallback chains and partial failures.
28. As a solo developer, I want a CLI command to reset a backend cooldown, so that I can manually recover a backend after fixing an offline server or provider issue.
29. As a router operator, I want the TUI to show all active dispatches, so that parallel coder sessions are visible at a glance.
30. As a router operator, I want the TUI to show the selected dispatch's stream and details, so that I can inspect one active agent without losing global status.
31. As a router operator, I want the TUI to show backend id, physical alias, model, Step reserve, Step usage, runtime, last tool, and status, so that backend-specific behavior is observable.
32. As a router operator, I want the TUI to support switching between active dispatches, so that multiple running coders can be inspected without a full dashboard.
33. As a router operator, I want global event logs to include ticket and backend identity, so that interleaved parallel output remains readable.
34. As a router operator, I want graceful stop to wait for active dispatches, so that shutdown does not abandon leases or lose quota reconciliation.
35. As a router operator, I want a second interrupt to support hard cancellation, so that I can still stop runaway or stuck dispatches when needed.
36. As a router operator, I want stale lease recovery after a crash, so that the Router does not forget active or ambiguous prior dispatches.
37. As a maintainer, I want backend allocation extracted into a deep module, so that Router complexity does not grow around quotas, leases, cooldowns, and failure classification.
38. As a maintainer, I want the Backend allocator to be generic by logical agent, so that future roles can reuse it without redesign.
39. As a maintainer, I want backend allocation enabled only for coder in the first milestone, so that review and merge quality constraints remain unchanged.
40. As a maintainer, I want physical OpenCode aliases generated from backend definitions, so that model binding is stable and safe under parallel dispatch.
41. As a maintainer, I want physical aliases hidden below the workflow boundary, so that ticket lifecycle states remain tied to logical agents only.
42. As a maintainer, I want backend state persisted in SQLite, so that parallel dispatch remains safe across process restarts.
43. As a maintainer, I want cached failure-driven Backend health only in the first milestone, so that the Router does not consume quota or hammer endpoints with live preflight checks.
44. As a maintainer, I want backend allocation tests to be deterministic, so that priority ordering, quotas, reserves, and fallback behavior can be proven without running OpenCode.
45. As a maintainer, I want Router integration tests around parallel coder dispatch, so that the existing lifecycle stays intact while multiple coders run concurrently.

## Implementation Decisions

- Keep `coder` as the logical router-visible workflow role.
- Do not create router-visible `coder1`, `coder2`, or `coder3` roles.
- Introduce Coder backends as schedulable inference capacity targets for logical coder dispatches.
- Generate stable physical agent aliases for Coder backends when the Agent harness requires model binding at agent-config time.
- Preserve logical agent identity in tickets, workflow state, metrics, logs, and user-facing lifecycle behavior.
- Record physical alias and backend id as execution details, not as workflow roles.
- Build a Backend allocator as a dedicated deterministic module instead of embedding backend selection directly inside the Router.
- Model backend allocation generically by Logical agent, but enable it only for router-visible coder dispatches in the first milestone.
- Use strict Priority backend allocation with lower numeric priority selected first.
- Use configured order and backend id as deterministic tie-breakers when priorities match.
- Reject weighted, random, or probabilistic load balancing for the first version.
- Store static backend definitions in TOML Backend catalogs.
- Support repo-level backend enablement, ordering, and overrides on top of global catalog definitions.
- Support fixed-window, dynamic-429, and unlimited quota modes.
- Prefer Step quota for request-limited providers because OpenCode step events are the closest router-observable approximation of provider request counts.
- Keep dispatch-count quota as a fallback or coarse safety cap.
- Add token quota only where reliable telemetry exists.
- Require a Step reserve before starting a coder dispatch on a request-limited backend.
- Default the coder Step reserve to the coder step budget unless overridden.
- Treat below-reserve request-limited backends as ineligible until the quota window restores enough capacity.
- Persist Backend leases, Step reserves, quota usage, cooldowns, failure counters, and dispatch attempts in SQLite.
- On successful or post-execution attempts, reconcile actual step usage against the Step reserve.
- On pre-execution failure with no possible worktree mutation, allow automatic retry on the next eligible backend.
- On post-execution or ambiguous failure, do not automatically retry on another backend; preserve or escalate the ticket attempt through existing safety behavior.
- On 429 responses, apply Retry-After when available and otherwise use the backend's configured cooldown default.
- On offline or connection failures, apply circuit breaker or exponential cooldown behavior.
- Use cached, failure-driven Backend health in the first milestone.
- Do not run live preflight checks for any backend in the first milestone.
- Add parallel coder dispatch behind configurable maximum parallelism.
- Keep the default parallelism compatible with existing serial behavior until explicitly increased.
- Use dependency-unblocked routable batches to find multiple coder candidates.
- Keep reviewer and merger dispatch effectively out of parallel backend allocation in the first milestone.
- Serialize shared git setup through a Git setup gate before launching concurrent agent sessions.
- Run agent sessions concurrently after each ticket's worktree and branch preparation is complete.
- Preserve existing worktree isolation and branch naming behavior for each ticket.
- Defer conflict-aware batching; dependency modeling and downstream merge gates handle cross-ticket conflicts in the first milestone.
- Implement Graceful router stop for multiple active dispatches.
- Treat hard interrupt or crash recovery as ambiguous/post-execution unless the system can prove no agent step started.
- Add backend status, backend history, and reset-cooldown CLI surfaces.
- Update the TUI with an Active dispatch view and selected-dispatch detail/stream.
- Show backend identity, selected physical alias, model, Step reserve, Step usage, cooldown, skipped reasons, and active capacity in operator-visible output.

## Testing Decisions

- Tests should validate observable allocation behavior rather than private implementation details.
- Backend catalog tests should verify TOML loading, global catalog definitions, repo overrides, deterministic ordering, and validation errors.
- Backend allocator tests should cover priority-first selection, tie-breaking, disabled backends, leased backends, cooldowns, quota exhaustion, below-reserve skipping, and ticket constraint filtering.
- Quota tests should cover fixed-window Step quota, dynamic-429 cooldowns, unlimited backends, dispatch-count fallback, reserve creation, reserve reconciliation, and quota window reset behavior.
- Failure classifier tests should distinguish pre-execution failures, post-execution failures, 429 quota failures, offline endpoint failures, and ambiguous failures.
- Retry tests should verify that pre-execution failures fall back to the next backend and post-execution failures do not blindly retry.
- SQLite state tests should verify lease persistence, stale lease detection, cooldown persistence, failure count persistence, and attempt history across Router restarts.
- Agent config generation tests should verify stable physical aliases, shared logical coder prompt and permissions, backend-specific model binding, and preservation of logical agent identity.
- Router tests should verify serial compatibility when maximum parallel coder dispatches is one.
- Router tests should verify multiple dependency-unblocked coder tickets can run concurrently when maximum parallelism and backend capacity allow.
- Router tests should verify dependency-blocked tickets are not dispatched in parallel with unfinished dependencies.
- Router tests should verify git setup is serialized while agent sessions run concurrently.
- Router tests should verify no duplicate dispatch is created for the same ticket.
- Router tests should verify graceful stop waits for active dispatches and reconciles leases.
- Router tests should verify hard interruption records ambiguous active dispatches safely.
- TUI tests should verify the Active dispatch view renders multiple in-flight dispatches.
- TUI tests should verify selected-dispatch switching changes the detail/stream view.
- TUI tests should verify backend identity and step usage are visible for active dispatches.
- CLI tests should verify backend status, backend history, and reset-cooldown behavior.
- Metrics tests should verify backend id, physical alias, logical agent, model, step count, token count, and failure classification are recorded.
- Existing router, ticket, worktree, and state transition tests should remain valid unless intentionally expanded for parallel coder behavior.

## Out of Scope

- Replacing the logical router-visible coder with multiple workflow roles.
- Backend allocation for reviewer, merger, decomposer, patch-reviewer, or codebase-scout in the first milestone.
- Weighted, random, or adaptive load balancing.
- Live backend preflight checks.
- Automatic conflict-aware batching based on expected file overlap.
- Automatic dependency inference between tickets.
- Provider API integrations that discover limits dynamically.
- Exact provider request counting when OpenCode does not expose reliable request telemetry.
- Full dashboard or web UI for backend observability.
- Parallel merge or parallel review workflow redesign.
- Automatic recovery from post-execution partial work beyond existing preservation and escalation behavior.
- Model benchmarking, quantization selection, or local inference server management.

## Further Notes

This PRD builds on the local coder optimization plan and ADR-0002. The core design principle is to keep infrastructure below the workflow boundary: the Router still works in terms of logical agents and tickets, while the Backend allocator handles physical inference capacity.

The first version should bias toward deterministic safety over maximizing utilization. Free provider quota should be used when it is clearly available, but a backend below the required Step reserve should be skipped rather than gambled on. Offline machines and unknown free providers should be cooled down rather than retried aggressively.

The TUI work is not cosmetic. Parallel dispatch makes the existing single-agent mental model inaccurate, so operator observability is part of the feature's correctness boundary. The system must show which backend is running which ticket and why allocation decisions happened.
