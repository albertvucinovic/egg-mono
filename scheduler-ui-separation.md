# Scheduler/UI separation TODO

## Context

Fresh live `py-spy` on `/home/albert/Private/Projekti/bitcoinzero/polymarket/.egg` after the snapshot-tail fix showed the TUI process still lagging while tools/LLM turns run. The snapshot full-rebuild hot path is mostly gone, but the TUI asyncio loop is still doing synchronous runner/scheduler bookkeeping and token/event reductions.

Representative hot stacks from the current live process:

- `SubtreeScheduler.run_forever -> discover_runner_actionable_cached -> _reduce_thread_events -> SELECT/events rows -> json.loads`
- `ThreadRunner._run_ra1_llm -> _last_stream_close_seq -> SELECT/events rows -> json.loads`
- `ThreadRunner.run_once -> build_tool_call_states -> _reduce_thread_events`
- `update_panels -> current_token_stats -> thread_token_stats/total_token_stats/provider_context_token_stats -> json.loads`
- smaller Rich rendering/panel metrics work

The user does **not** want a fully independent runner daemon/process. Desired invariant:

> If the TUI is turned off, nobody is running the threads.

Therefore the first strategy is an asyncio/cooperative separation inside the TUI-owned runtime: keep scheduler/runner lifecycle owned by the TUI, but ensure scheduler/runner bookkeeping cannot monopolize the UI render/input loop.

## Constraints

- Do not create an independent scheduler daemon.
- Preserve TUI-owned execution: closing/cancelling the TUI must cancel running scheduler/runner tasks.
- Prefer minimal, reversible, testable changes.
- Do not add broad public configuration unless needed.
- Optimize based on py-spy hot paths, not speculation.
- Keep UI/input/render latency-sensitive paths bounded; no unbounded full-history scans from the render tick.

## Phase 1 — Cooperative scheduler fairness

Goal: stop `SubtreeScheduler.run_forever` from monopolizing the TUI loop when checking large/changed threads.

- [x] Add cooperative yields in scheduler candidate discovery so large subtrees or expensive reducer calls do not block the UI loop for a whole scheduling pass.
- [x] Limit/checkpoint expensive runnable discovery work per loop iteration if practical without changing semantics.
- [x] Preserve scheduling correctness: runnable threads must still be discovered and scheduled; this is fairness, not skipping work forever.
- [x] Add focused tests around scheduler behavior if a new mechanism is introduced; otherwise run existing scheduler tests.

## Phase 2 — Incremental derived thread state

Goal: treat Egg's append-only event log as an input stream and maintain rebuildable materialized views instead of replaying/parsing the full history on hot paths.

Conceptual mapping:

- input collection: `events`
- timestamp/frontier: `event_seq`
- materialized view: per-thread derived state
- change propagation: `apply_event(state, event)` over `events_since(processed_event_seq)`
- source of truth: SQLite events; any cache must be disposable/rebuildable

Initial derived state target:

- `processed_event_seq`
- `skipped_msg_ids`
- `msg_seq_by_id`
- `user_seqs`
- `last_llm_boundary_seq`
- known LLM invoke IDs / stream boundaries
- `tool_call_states`
- `next_runner_actionable`
- `coarse_thread_state_without_lease`

Implementation strategy:

- [ ] Replace or augment `_reduce_thread_events()` with an incremental per-thread cache that starts from the previous reduced state and applies only events after `processed_event_seq`.
- [ ] Keep a safe full-rebuild fallback for rare hard/reset events while the incremental reducer is young:
  - `msg.edit`
  - `msg.delete`
  - `control.interrupt` with `purpose=continue`
  - other events whose semantics are easier to rebuild than patch initially
- [ ] Incrementally maintain the LLM boundary currently recomputed by `_last_stream_close_seq()`.
- [ ] Ensure callers that need public/mutable `ToolCallState` objects still receive copies, not cache-owned objects.
- [ ] Preserve correctness for assistant tool calls, user tool calls, interrupted tool streams, skipped messages, global tool approval, and auto-approved tools.
- [ ] Add focused tests comparing incremental results to a forced full rebuild over representative histories.

## Phase 3 — Runner hot-scan reductions

Goal: reduce synchronous full-history work during a single TUI-owned runner turn.

- [ ] Route `_last_stream_close_seq()` through the incremental derived-state boundary so RA1 trigger discovery does not scan/parse the full event log on large stream-heavy threads.
- [ ] Avoid `build_tool_call_states()` after RA1 when the just-completed turn obviously has no pending assistant tool calls, or use a cheaper check.
- [ ] Preserve correctness for assistant tool-call turns, interrupted streams, continue interrupts, and skipped messages.
- [ ] Add focused tests for any optimized boundary/tool-state logic.

## Phase 4 — UI panel/token-stat throttling

Goal: keep render/input ticks bounded while streams are active.

- [ ] Ensure `update_panels()` never calls expensive token/reducer functions more often than necessary during active streams.
- [ ] Prefer cached/stale token metrics while streaming over blocking input/scroll.
- [ ] Avoid duplicate header TPS/token metric calls in the same panel update.
- [ ] Preserve visible metrics when idle or after stream completion.

## Phase 5 — Optional TUI-owned subprocess fallback

Only if asyncio/cooperative fixes are not enough.

- [ ] Consider a child runner process started and owned by the TUI, with parent-death/cancellation semantics.
- [ ] The child must not be an independent daemon; TUI exit must stop execution.
- [ ] Use SQLite/open_stream leases as IPC/state, but keep lifecycle tied to TUI.
- [ ] Ask before implementing this phase.

## Validation plan

- [x] Focused unit/regression tests for touched modules.
- [x] Existing scheduler tests.
- [ ] Existing UI/token tests.
- [x] `git diff --check`.
- [ ] Re-profile live polymarket TUI after Phase 1–3.

## Status notes

- 2026-05-18: Created after user rejected an independent runner daemon and asked for scheduler/UI separation using worker-manager. Next: Phase 1 cooperative scheduler fairness.
- 2026-05-18: Phase 1 implemented cooperatively inside `SubtreeScheduler.run_forever`: added private scheduler fairness checkpoints after bulk scheduler bookkeeping and through sticky/discovery/scheduling loops. This keeps runner lifecycle TUI-owned and does not add a daemon/process. Added regression coverage that a UI-like asyncio task runs before a large runnable-discovery pass completes. Focused tests passed: `pytest -q eggthreads/tests/test_scheduler_slots.py`; `pytest -q eggthreads/tests/test_headless_subtree_scheduler.py`; `git diff --check`.
- 2026-05-18: Added incremental computation/change-propagation phase. Egg's event log should be treated as an append-only input stream, with reducer/tool/boundary/token views maintained incrementally from `event_seq` frontiers instead of full-history replay on TUI hot paths.
