# egg-mono invariants

These are the major design invariants for `egg-mono`. Keep this file short: it should capture durable architecture promises, not implementation details or temporary plans.

- Every default tool has detailed LLM-facing help.
- The SQLite database schema is stable and should remain unchanged unless there is an explicit migration/compatibility plan.
- Performance for very long threads is a priority; use incremental computation, cached reducer/snapshot state, and bounded rendering/loading wherever possible.
- Every thread is the root of a descendant-thread tree from the perspective of the current view.
  - Switching to a subthread makes that subthread the root of the tree currently being inspected.
- Descendants must not be able to inspect parent or sibling data; ancestors may inspect descendants under explicit descendant-selection/access rules.
- Descendants inherit at least the security guarantees of their direct ancestor.
- Everything meaningful should be inspectable and transparent to the user.
  - Every meaningful action should be represented by inspectable state or events.
  - Streaming tokens, tool-call arguments, and tool outputs should be visible in real time, including in `displayVerbosity=min`.
- Scheduler ownership is not process-local state.
  - Every Egg/EggW process should run a scheduler for every root thread it visits.
  - Different schedulers working on the same DB compete for per-thread leases.
- Leases, not UI state, decide whether work is actively running.
  - Idle resident schedulers are not active thread work.
  - Expired leases must be recoverable/takeover-safe.
- User-initiated commands and tool calls should have predictable approval semantics, including automatic approval where the user explicitly initiated the action.
- Provider capabilities and artifacts should be explicit, inspectable, and access-controlled; never rely on hidden provider behavior when Egg can model it directly.
- Terminal Egg and EggW should preserve the same core thread, tool, scheduler, visibility, and access semantics even when their UIs differ.
