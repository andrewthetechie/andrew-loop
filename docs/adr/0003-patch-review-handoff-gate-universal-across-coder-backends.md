# Make the patch-review handoff gate universal across coder backends

The **Patch-review handoff gate** at the **Code Review** transition will fire for every nontrivial **Router-visible coder** dispatch regardless of which **Coder backend** produced the diff. Trivial-skip criteria (single file, low risk, no sensitive paths, no **Leaf coder** usage, first-pass validation success, no architecture or behavior concerns) remain the only way to bypass the gate. The **Backend catalog** does not gain a "trust tier", "skip patch-review", or per-backend quality flag.

## Considered Options

- **Per-backend trust tier**: tag each **Coder backend** with a quality tier and skip patch-review when a high-tier backend produced the diff. Rejected because it introduces a fuzzy quality judgment into the **Backend catalog**, couples the local-coder PRD to backend identity, and creates a slippery slope toward backend-specific gate logic.
- **Expanded trivial-skip on high-tier backends**: keep the universal gate but make trivial-skip more permissive when the diff came from a stronger backend. Rejected because it combines the worst of both approaches — universal gate logic plus per-backend exceptions to it.
- **Drop the gate entirely**: rely on the **Independent review gate** alone. Rejected because the gate's value is catching coder mistakes before the **Reviewer** spends a full cycle, which applies regardless of coder tier.

## Consequences

- The **Patch-review handoff gate** can be implemented and tested without any reference to which **Coder backend** ran the dispatch.
- The **Backend catalog** stays focused on capacity, quota, cooldown, and failure concerns; it does not encode coder quality judgments.
- Strong **Coder backends** (free cloud reasoning models, future paid remote models) will sometimes consume a Spark patch-review cycle even when the diff is fine. This is accepted because Spark patch-review is cheap relative to a wasted **Independent review gate** cycle, and trivial-skip criteria already filter out the genuinely easy cases.
- If a future milestone shows that strong backends produce diffs that consistently pass patch-review, the trivial-skip criteria can be expanded, but per-backend skip logic is explicitly out of scope.
