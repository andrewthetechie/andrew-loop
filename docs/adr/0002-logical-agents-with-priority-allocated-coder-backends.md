# Use logical agents with priority-allocated coder backends

The Router will keep `coder` as the logical workflow role and schedule coder dispatches onto configured Coder backends instead of creating router-visible roles such as `coder1`, `coder2`, or `coder3`. Each Coder backend may have a stable physical agent alias for OpenCode model binding, but the ticket lifecycle, metrics, and user-facing workflow preserve the logical `coder` identity. Backend allocation will be deterministic priority-first with persisted leases, step reserves, quotas, cooldowns, failure classification, and observability so the Router can use local and free cloud inference capacity without leaking infrastructure into the domain model.

Coder backends will be defined in TOML backend catalogs with repo-level enablement, ordering, and overrides. Request-limited providers use step-based quotas because OpenCode step events are the closest router-observable approximation of provider requests; dispatch-count quotas remain a coarse fallback and token quotas may be added only when reliable telemetry exists.

## Considered Options

- Create multiple router-visible coder agents: rejected because it pollutes the workflow model and conflicts with `Router-visible coder` as a single implementation role.
- Rewrite the `coder` OpenCode model config before each dispatch: rejected because parallel dispatch would race over shared agent config.
- Use weighted or random load balancing: rejected because deterministic replay and debuggable allocation reasons matter more than even distribution.
- Run live health preflight for every backend: rejected for this milestone because health checks can consume quota or repeatedly hammer offline local machines.
- Use JSON or YAML for the backend catalog: rejected because TOML matches the existing orch configuration model and is readable for operator-maintained backend lists.

## Consequences

- The system needs a backend allocator module rather than embedding provider selection directly in the Router.
- Backend state must be persisted in SQLite so leases, quotas, cooldowns, and failure history survive Router restarts.
- OpenCode agent generation must support stable physical aliases per backend while keeping logical agent identity separate.
- Parallel coder dispatch can be enabled through backend leases and dependency-safe routable batches without adding new ticket states.
- The TUI must become multi-dispatch aware, showing active dispatches, selected-dispatch streams, backend identity, step reserve usage, cooldowns, and allocation reasons rather than assuming one current agent.
