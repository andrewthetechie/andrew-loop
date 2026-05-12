# Repurpose the router-visible coder into an implementation coordinator

We will keep the existing ticket lifecycle and router-visible `coder` identity, but change that role into an orchestration-first implementation coordinator. The coordinator may use hidden OpenCode subagents through native Task-based delegation, with `leaf-coder`, `codebase-scout`, and `patch-reviewer` as the initial helper set. This preserves the independent review gate and current router/database model while reducing top-level coding context and letting implementation work be split into smaller bounded units.
