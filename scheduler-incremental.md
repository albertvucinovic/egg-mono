# Scheduler incremental computation TODO

## Goal

Make scheduler actionability checks O(new events) instead of O(thread history), so
long threads remain responsive while tools, child waits, and LLM streams are
active.

The problem is not primarily terminal painting or countdown display anymore. The
scheduler hot path repeatedly asks whether large threads are runnable by replaying
and JSON-decoding their full event logs whenever their event watermark changes.

## Evidence to preserve

- 2026-05-27 py-spy profile after wait-countdown work:
  - profile: `/tmp/egg-pyspy-1384050-lag2-30s-1779914365.raw`
  - target process CPU: ~86%
  - all samples were on `MainThread`
  - hot path:
    - `SubtreeScheduler.run_forever`
    - `discover_runner_actionable_cached`
    - `_reduce_thread_events`
    - `_reduce_loaded_thread_events`
    - `json.loads` / JSON decoder
  - selected path aggregates:
    - `_reduce_thread_events`: ~86.6%
    - `discover_runner_actionable_cached`: ~84.7%
    - `_reduce_loaded_thread_events`: ~53.9%
    - JSON load/decode: ~44%
  - UI paint was effectively absent in this sample.
- Earlier wait-specific profiles showed the same reducer problem through
  `wait_tool -> wait_for_threads -> thread_state`, but the latest profile shows
  the scheduler alone is sufficient to saturate the main loop.
- Countdown `tool_call.summary` events were first reduced to a sparse cadence and
  then removed for automatic timeout countdowns. Timeout display is now computed
  locally from `tool_call.execution_started.timeout_sec` and stream start time.
  This removed one churn source, but normal stream/tool lifecycle tails can still
  invalidate the scheduler reducer.

## Design principle

The scheduler should be an incremental state machine over event tails, not a
periodic full-history interpreter.

Events remain the source of truth. A full rebuild remains the correctness
fallback. But ordinary scheduler work should maintain a compact derived state and
apply only events since the previous watermark.

Beautiful target shape:

```text
SQLite events/open_streams are truth
        |
        v
compact SchedulerThreadState per thread
        | apply tail events
        v
next RunnerActionable / coarse scheduler state
```

The scheduler should not need static transcript rendering, token stats, snapshot
JSON, or full message history. It needs only enough state to decide RA1/RA2/RA3
and respect leases.

## Invariants

- Event log remains canonical; caches may be dropped at any time.
- Leases/open streams remain the coordination primitive for multiple UIs and
  scheduler instances:
  - non-expired open stream means the thread is running elsewhere or already
    leased by this process;
  - scheduler must not run RA1/RA2/RA3 concurrently with another active lease;
  - expired leases may be taken over, preserving existing interrupt semantics.
- Runnable semantics must stay identical to current `RunnerActionable` behavior:
  - RA3 user tool work has priority over RA2 and RA1;
  - RA2 assistant tool work has priority over RA1;
  - RA1 triggers only from API-visible user/tool messages after the latest LLM
    boundary;
  - no RA1 while any assistant tool call is unpublished;
  - auto-approved tools and global approval semantics are preserved;
  - `continue` / skipped messages are respected.
- Full rebuild fallback must remain for hard/global events until their
  incremental semantics are explicit.
- Scheduler improvements must not make UI/status/token code more complex.
  Separate consumers should use separate projections where useful.

## Current architecture summary

### Scheduler loop

- `SubtreeScheduler.run_forever` collects the subtree each poll.
- It bulk-loads max event seqs, active open streams, and scheduling settings.
- It skips threads whose max event seq matches `last_checked_seq`.
- It skips threads with active non-expired leases from `_active_open_threads_bulk`.
- It calls `discover_runner_actionable_cached(db, tid)` for changed, non-leased
  threads.
- `discover_runner_actionable_cached` calls `_reduce_thread_events(db, tid)` and
  returns `next_runner_actionable`.

### Reducer

- `_reduce_thread_events` is a shared process-local reducer cache keyed by
  `(db_path, thread_id, max_event_seq)`.
- It already supports a small incremental safe tail for:
  - plain `msg.create` without tool calls/tool result linkage;
  - `stream.open`, `stream.delta`, `stream.close`;
  - non-`continue` `control.interrupt`;
  - `tool_call.summary`.
- It falls back to full replay for normal tool lifecycle mutations:
  - `msg.create` that declares tool calls;
  - tool result `msg.create` with `tool_call_id`;
  - `tool_call.approval`;
  - `tool_call.execution_started`;
  - `tool_call.finished`;
  - `tool_call.output_approval`;
  - `msg.edit` / continue-related events.
- Full replay fetches all thread events and decodes all payloads, which is what
  py-spy shows dominating CPU.

### Open streams / leases

- `ThreadsDB.try_open_stream` refuses active non-expired leases, can take over
  expired leases, and appends `control.interrupt` for takeover.
- `ThreadsDB.heartbeat` extends lease lifetime without appending events.
- `ThreadsDB.release` deletes the lease row.
- `thread_state` already checks `current_open` before reducing.
- Scheduler already skips active open threads before discovery, but if a large
  thread is non-leased and changed, discovery can still full-reduce it.

## Target abstraction

Introduce an explicit scheduler projection, likely in `eggthreads/tool_state.py`
or a small private sibling module if the file grows too much:

```python
@dataclass
class SchedulerThreadState:
    thread_id: str
    max_event_seq: int
    skipped_msg_ids: set[str]
    msg_seq_by_id: dict[str, int]
    user_seqs: list[int]
    last_llm_boundary_seq: int
    llm_invokes: set[str]
    last_llm_stream_boundary_seq: int
    last_assistant_seq: int
    messages_after_records: list[EventRecord]
    tool_call_states: dict[str, ToolCallState]
    global_auto_approval: bool
    global_approval_intervals: list[tuple[int, Optional[int]]]
    current_global_start: Optional[int]
    next_runner_actionable: Optional[RunnerActionable]
    coarse_state_without_lease: str
```

This may initially reuse `_ThreadEventReduction` if that keeps the change small,
but the conceptual boundary should be clear: scheduler state is a compact event
projection, not the full transcript.

## Phase 1 — Cheap lease-aware scheduler guardrails

- [x] Ensure scheduler never calls `discover_runner_actionable_cached` for an
      active non-expired lease, including sticky idle checks.
- [x] Review `_is_thread_idle`: it currently calls `is_thread_runnable` before
      checking open streams. Reverse this order or use the existing bulk lease
      state so sticky scheduling cannot full-reduce actively leased huge threads.
- [x] When a thread is skipped because of an active lease, record enough lease
      state to avoid repeatedly reconsidering it until either:
      - lease disappears/expires, or
      - event seq changes in a way that matters after the lease ends.
      Do not hide lease expiry; expired leases must still become eligible.
- [x] Add tests proving active leased threads do not invoke
      `discover_runner_actionable_cached` through either the main scheduling pass
      or sticky idle path.

## Phase 2 — Incremental normal tool lifecycle tails

Extend the reducer tail path for the normal events that make tool execution
advance after a cached baseline:

- [ ] `tool_call.approval`
  - [x] explicit `granted` / `denied` by `tool_call_id`;
  - [ ] `all-in-turn` using cached `user_seqs` and current turn boundaries;
  - [ ] global approval / revoke using incremental global approval state.
- [x] `tool_call.execution_started`
  - set `execution_started=True`;
  - record `owner_invoke_id`;
  - preserve `timeout_sec` payload as display metadata only if needed elsewhere.
- [x] `tool_call.finished`
  - set `finished_reason` and `finished_output`.
- [x] `tool_call.output_approval`
  - set `output_decision` and `last_output_approval_payload`.
- [ ] tool result `msg.create` with `tool_call_id`
  - mark the matching tool call `published=True`;
  - also add it to `messages_after_records` if it is an API-visible tool message
    after the current LLM boundary, because it can trigger RA1.
- [ ] assistant/user `msg.create` with declared `tool_calls`
  - create/replace `ToolCallState` entries for the declared calls;
  - update message-after-boundary records as current full reducer does.
- [ ] non-continue `control.interrupt` that references active tool invokes
  - apply the same interrupted-tool synthesis as full rebuild for affected
    tool calls.
- [ ] stream close for active tool invokes
  - preserve the existing conservative behavior or implement exact incremental
    interruption synthesis. Do not silently mark tools finished incorrectly.
- [x] Recompute `next_runner_actionable` and coarse state from compact state after
      applying the tail.
- [ ] Add equivalence tests comparing incremental reductions to full rebuilds for
      each lifecycle transition and for a combined assistant-tool round trip.
  - Current status: explicit approval, execution_started, finished, and
    output_approval are covered; combined round trip remains for later slices.
- [x] Add mutation-safety tests: old cached states must not be mutated by later
      incremental updates.

## Phase 3 — Hard-event boundary and fallback policy

Document and enforce a small set of events that still force full rebuild:

- [ ] `msg.edit` and `msg.delete` until edit/delete semantics are incremental.
- [ ] `control.interrupt` with `purpose='continue'` because it rewrites the
      effective message boundary and skipped set.
- [ ] malformed/reused tool-call ids that cannot be resolved against cached
      state.
- [ ] schema/version mismatch or impossible ordering.

Add instrumentation counters in tests or debug-only local helpers so we can say:

```text
normal stream/tool tails: incremental
continue/edit/delete: full rebuild fallback
```

Do not add noisy runtime logging by default.

## Phase 4 — Scheduler-owned projection cache

- [ ] Decide whether scheduler uses the existing `_REDUCER_CACHE` or a smaller
      scheduler-owned cache.
- [ ] Preferred long-term design: a private scheduler cache keyed by
      `(db_path, thread_id)` that stores one mutable/current projection and
      applies event tails by watermark.
- [ ] Expose a private helper such as:

```python
def discover_runner_actionable_incremental(db, thread_id) -> Optional[RunnerActionable]:
    ...
```

  or evolve `discover_runner_actionable_cached` so callers keep their API while
  internals become genuinely incremental.
- [ ] Bound memory:
  - keep one current projection per active thread in process;
  - do not keep all historical reductions;
  - prune cache entries for deleted/unknown threads or subtree changes.
- [ ] Preserve process-local nature. No persisted scheduler cache until there is
      evidence process restarts need it.

## Phase 5 — Scheduler loop integration

- [ ] Use the incremental actionability helper in `SubtreeScheduler.run_forever`.
- [ ] Keep `last_checked_seq` semantics:
  - non-runnable idle threads may be skipped until their event watermark changes;
  - runnable-but-unscheduled threads must not be watermarked away;
  - active leased threads must be cheap and safe for multi-UI operation.
- [ ] Review sticky scheduling with the new helper so idle checks do not trigger
      full history scans.
- [ ] Keep fairness yields, but the main win should be less work, not only more
      yielding.

## Phase 6 — Status/wait consumers

- [ ] `thread_state` and `wait_for_threads` can reuse the same compact projection
      for coarse state, but only after scheduler equivalence tests are strong.
- [ ] Keep `wait_for_threads` cheap unchanged-watermark cache from `wait-fix.md`.
- [ ] Avoid token stats, snapshots, or UI rendering from depending on the
      scheduler projection unless there is a clear local benefit.

## Phase 7 — Benchmarks and profiling acceptance

Add focused synthetic tests/benchmarks that run in the checkout without requiring
real provider calls:

- [ ] Build a long historical thread with at least tens of thousands of events
      and many completed tool calls.
- [ ] Warm the scheduler projection once.
- [ ] Append normal tail events one by one:
  - summary-only;
  - stream deltas;
  - tool lifecycle sequence;
  - final tool message;
  - next user/tool-triggering message.
- [ ] Assert normal tails do not call `_reduce_loaded_thread_events` after warmup.
- [ ] Assert actionability matches a full rebuild after each tail.
- [ ] Measure tail-apply timing and record order-of-magnitude result in this TODO.
- [ ] Re-run py-spy on the long `T9JM`/`FQ7A` style scenario after restart. Target:
  - scheduler no longer dominated by full `_reduce_thread_events` rebuilds;
  - active UI scroll/input lag is not caused by scheduler CPU saturation.

## Implementation notes / likely pitfalls

- `ToolCallState` objects in cached projections must be copied/replaced when
  mutated so old cached snapshots and public `build_tool_call_states` results are
  not contaminated.
- `all-in-turn` approval depends on user-turn boundaries. Cache enough ordered
  `user_seqs` to apply it exactly.
- `continue` is intentionally hard. Do not fake it incrementally until the
  skipped-set/boundary behavior is formally modeled.
- Assistant tool-call protocol matters: RA1 must wait until assistant tool calls
  are published as tool messages.
- Tool stream close currently synthesizes interrupted tool output for active
  tools. Preserve this behavior or keep it as a full rebuild fallback.
- Active leases should be checked before expensive actionability. This matters
  for multiple UIs and for tools like `wait` that hold a lease while polling.
- Do not introduce a second public actionability semantic. If a new helper is
  added, it should be private or transparently replace the existing cached helper.

## First recommended implementation slice

Start with Phase 1 plus a small regression test: fix `_is_thread_idle` ordering
and any active-lease scheduler paths that can still call actionability. This is a
small, safe improvement and aligns with the multi-UI lease model.

Then implement Phase 2 in small event-family slices, committing each after tests:

1. explicit tool lifecycle transitions for known tool ids;
2. tool result message publication;
3. assistant/user tool-call declarations;
4. approval/global approval semantics;
5. interruption/close synthesis or documented fallback.

## Status notes

- 2026-05-27: Plan created after researching scheduler, reducer, lease, wait,
  and UI streaming paths. No implementation yet. Current preceding commits:
  `bd6ca44` local timeout display, `fc77985` no automatic countdown summaries,
  `06b6a69` wait unchanged-poll cache, `3956f30` summary incremental reducer.
- 2026-05-27: Phase 1 implemented. `_is_thread_idle` now checks active leases
  before actionability, the scheduler records active-lease watermarks and clears
  them when leases disappear/expire, and regression tests cover main scheduling
  plus sticky idle active-lease skips. Phase 2 reducer-tail work remains next.
- 2026-05-27: Phase 2 first lifecycle slice implemented. Incremental tails now
  handle explicit per-tool `granted`/`denied` approval, execution start,
  finished, and output approval for resolvable existing tool calls, with RA/coarse
  recomputation and mutation-safety/equivalence tests. Unknown tool ids,
  all-in-turn, global approval/revoke, publication, declarations, and interrupt/
  close synthesis remain full-rebuild/later-slice work.
