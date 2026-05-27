# Wait / long-thread reducer performance TODO

## Goal

Reduce input/UI lag while `wait` is active in very large threads without weakening
wait correctness. The concrete profiled failure mode is that both the runner
scheduler and the `wait` tool repeatedly rebuild full thread state for huge event
logs after small status/countdown events.

## Evidence to preserve

- Target process profiled with `sudo py-spy` against PID `778537`.
- 30s profile: `/tmp/egg-pyspy-778537-30s-1779865357.raw`, 3924 samples, 0 errors.
- Hot paths:
  - MainThread: `run_forever -> discover_runner_actionable_cached -> _reduce_thread_events`.
  - Worker thread: `wait_tool -> wait_for_threads -> thread_state -> _reduce_thread_events`.
- Hottest line: `eggthreads/eggthreads/tool_state.py:343`, full `events` fetch/list conversion, about 58% exclusive samples.
- Next hot area: JSON payload decode in `_reduce_loaded_thread_events`.
- Triggering event class: frequent persisted `tool_call.summary` countdown/status events, especially from long `wait` calls.

## Invariants

- `wait` completion remains deterministic and event-log based:
  - no active non-expired stream;
  - no unresolved tool call;
  - no runner-actionable work;
  - latest API-triggering user/tool message has a later LLM result/error.
- `tool_call.summary` is UI/status metadata only. It may update `ToolCallState.summary`, but it must not advance the tool lifecycle or create/remove runner actionability.
- Expired open-stream leases must still be released before deciding completion.
- Existing public APIs should keep their behavior. Prefer private helpers/local caching over new public interfaces.
- Optimize the profiled root cause before touching terminal rendering.

## Phase 1 — Make `tool_call.summary` reducer-incremental-safe

- [x] Extend the cached reducer's incremental tail path so a tail containing only already-safe events plus `tool_call.summary` does not force a full replay.
- [x] Apply summary events by copying only the affected cached `ToolCallState` objects, so previous cached reductions are not mutated.
- [x] Recompute cheap derived fields (`next_runner_actionable`, coarse state) from the updated in-memory states/messages.
- [x] Preserve full-rebuild fallback for hard events:
  - new tool-call declarations;
  - approval/execution/finish/output approval/tool result messages;
  - `continue` interrupts;
  - stream closes that may interrupt active tool executions.
- [x] Add tests showing:
  - summary tails after an active tool do not call `_reduce_loaded_thread_events`;
  - incremental summary results match a full rebuild, including `ToolCallState.summary`;
  - prior cached reductions are not mutated by later summary updates.
- [x] Run focused reducer tests.

## Phase 2 — Make `wait_for_threads` polling cheap when nothing meaningful changed

- [x] Avoid calling full `thread_state()` every poll when a cheap check is enough:
  - missing thread -> finish as not found;
  - paused thread -> state is paused;
  - active non-expired open stream -> state is running;
  - expired open stream -> release, then continue checks.
- [x] Track each waited thread's last event watermark during the wait call. If the watermark did not change and the previous poll already proved the thread unfinished, reuse that result instead of recomputing reducer/completion state.
- [x] Use cached reducer-backed actionability in `_thread_wait_complete` where possible, so the completion path does not re-scan via the older uncached actionability helper.
- [x] Add tests for repeated polling on an unchanged unfinished thread and an active-open-stream thread, verifying reducer calls are not repeated unnecessarily.
- [x] Keep timeout result reporting accurate enough for current API behavior.

## Phase 3 — Reduce persisted countdown churn from long-running tools

- [x] Inspect current `tool_call.summary` emission cadence for bash/Python/generic tools and `wait`.
- [x] Persist summaries only when the visible text changes materially or at a sparse cadence. Do not remove start/end/failure visibility.
- [x] Prefer computing purely time-based countdown display locally if there is an existing local UI path; do not add a new public API unless needed.
- [x] Add focused tests for summary throttling behavior.

## Phase 4 — Follow-up profiling / regression checks

- [ ] Re-run a short profile against the long `T9JM`/`FQ7A` scenario or an equivalent synthetic long-thread wait.
- [ ] Confirm the dominant samples are no longer repeated full `_reduce_thread_events` rebuilds on summary-only tails.
- [ ] If scrollback remains laggy after wait CPU is fixed, address the separate `TranscriptScrollbackSource` deep-scroll cache/prepend issue in a new TODO or follow-up phase.

## Status notes

- 2026-05-26: Created plan from py-spy evidence. Next implementation slice: Phase 1 only, because it directly targets the profiled full-rebuild-on-summary cause and should be contained to reducer code plus tests.
- 2026-05-27: Phase 1 implemented. `tool_call.summary` now stays on the incremental reducer path, copying only updated `ToolCallState` entries and preserving hard-event full-rebuild fallbacks. Focused reducer tests pass.
- 2026-05-27: Phase 2 implemented. `wait_for_threads` now uses cheap missing/paused/open-stream checks, caches unchanged unfinished poll results by event watermark within a wait call, and uses reducer-cached actionability in `_thread_wait_complete`. Focused wait/reducer tests pass.
- 2026-05-27: Phase 3 implemented. Timeout countdown summaries now emit immediately, then at a sparse shared cadence for runner-managed tools and wait-for-tool-call polling. Focused throttling/wait/tool-timeout tests pass.
