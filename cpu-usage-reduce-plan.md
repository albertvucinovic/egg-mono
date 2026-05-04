# CPU Usage Reduction Plan

This document is the continuation plan for reducing CPU usage in `egg-mono` without losing functionality, correctness, or latency. Treat it as the task ledger: **after every meaningful step, update this file before continuing** so the work can safely resume in another session.

## Operating rules for future sessions

- Work in small, reversible steps. Do not bundle unrelated optimizations.
- Preserve behavior first; CPU reduction is invalid if it changes thread semantics, tool semantics, streaming semantics, recovery, or UI responsiveness.
- Prefer measurement and targeted fixes over broad rewrites.
- Before editing code, identify the minimal target files. If a change appears to require more than 3 files or a new public interface, stop and ask the user.
- Reuse existing helpers and tests. Avoid parallel systems for the same state unless the old path is removed or clearly delegated.
- The SQLite event log remains the source of truth. Any cache/materialized state must be rebuildable.
- Every refactor touching brittle semantics should have focused tests before or with the change.
- After each meaningful step, update:
  - the checkbox/status below,
  - `Progress log`,
  - `Current work cursor`,
  - `Known risks / open questions`, if affected.

### Definition: meaningful step

A meaningful step is any completed unit such as:

- a profiling/baseline measurement,
- a code change,
- a test addition/update,
- a design decision that narrows scope,
- a failed attempt that teaches something,
- discovery of a brittle area or correctness risk.

## Current work cursor

- Status: Phase 1.1, 1.2, and 1.4 completed; REPL polling, snapshot logging, and web live TPS fixes committed.
- Last updated: after Phase 1.1 REPL/session polling fix.
- Recommended next action: continue with Phase 1.3 tool stream chunk sequence queries.

## Progress log

- Initial plan created in `cpu-usage-reduce-plan.md`.
- Phase 1.4 completed: fixed `eggw/eggw/routes/stats.py` missing `datetime` import/time helper so live LLM TPS is no longer silently swallowed; added `eggw/tests/test_api.py::TestTokenStats::test_get_stats_includes_live_llm_tps`. Tests run: `python -m pytest eggw/tests/test_api.py::TestTokenStats -q` (2 passed).
- Phase 1.2 completed: converted eager per-event `SnapshotBuilder` info logging to guarded lazy debug logging in `eggthreads/eggthreads/snapshot.py`. Tests run: `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py -q` (14 passed).
- Phase 1.1 completed: added a shared 50ms sleep to Docker Python REPL eval polling and removed duplicate Bash Docker REPL sleeps in `eggthreads/eggthreads/session.py`. Tests run: `python -m pytest eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py -q` (12 passed) and `python -m pytest eggthreads/tests/test_session_config.py -q -k 'not docker_session_status_skeleton_when_available'` (17 passed, 1 deselected). Full `test_session_config.py` hit an environment issue because `/workspace/.egg` is read-only in this runtime, not because of this change.

## High-level strategy

1. Measure first enough to avoid guessing.
2. Apply safe quick wins that remove obvious busy work.
3. Reduce repeated full event-log scans with a single cached reducer.
4. Make snapshots and token stats incremental.
5. Move polling/rendering toward event-driven dirty flags.
6. Consider Go only as optional sidecars after Python-side architecture fixes are measured.

## Phase 0 — Baseline and guardrails

- [ ] Capture baseline idle CPU for the TUI.
  - Suggested command: run `./egg/egg.sh` in a real-ish terminal, then inspect process CPU with `top`, `htop`, or `py-spy top`.
  - Record conditions: thread size, streaming/idle, terminal size, sandbox/session enabled state.
- [ ] Capture baseline idle CPU for the web backend and frontend.
  - Run `./eggw/eggw.sh`, open one tab, then multiple tabs on the same thread.
  - Record backend CPU and browser CPU separately if possible.
- [ ] Capture baseline CPU during a long LLM stream or mocked stream.
  - Track CPU per 1k `stream.delta` events if possible.
- [ ] Capture baseline scheduler cost with many threads.
  - Use existing tests/examples or a small script to create many idle child threads.
- [ ] Identify focused test suites for the hot paths before changing them.
  - Core scheduler/tool state: `eggthreads/tests/test_scheduler_slots.py`, `test_tool_state_runner_actionable.py`, `test_continue_thread.py`, `test_events_and_open_streams.py`.
  - Session/REPL: `eggthreads/tests/test_auto_session_repl.py`, `test_python_repl_tool.py`, `test_bash_repl_tool.py`, `test_repl_bridge.py`.
  - TUI/rendering: `egg/tests/test_streaming_tui.py`, `eggdisplay/tests/*`.
  - Web: `eggw/tests/test_api.py`, frontend e2e only if relevant.

## Phase 1 — Safe quick wins

### 1.1 Fix REPL/session busy polling

- [x] Inspect Docker Python REPL request/response loop in `eggthreads/eggthreads/session.py`.
  - Confirmed `_execute_python_docker()` checked response files in a loop without sleeping when no response existed.
- [x] Add the smallest latency-preserving wait strategy.
  - Added shared `_DOCKER_EVAL_POLL_SEC = 0.05` fallback sleep after timeout check and after servicing tool requests. This matches the container-side `sessiond` polling cadence and avoids a host-side busy spin while keeping expected eval latency around the existing 50ms poll granularity.
- [x] Remove duplicate sleeps in the Bash Docker path if confirmed.
  - Confirmed and replaced the two consecutive `time.sleep(0.05)` calls with one shared sleep.
- [x] Run focused session/REPL tests.
  - `python -m pytest eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py -q` (12 passed).
  - `python -m pytest eggthreads/tests/test_session_config.py -q -k 'not docker_session_status_skeleton_when_available'` (17 passed, 1 deselected).
  - Full `test_session_config.py` was attempted but `test_docker_session_status_skeleton_when_available` failed because this runtime has read-only `/workspace/.egg`; record as environment issue.
- [x] Update this plan with exact files touched, tests run, CPU/latency observation, and any remaining risk.
  - Files touched: `eggthreads/eggthreads/session.py`, `cpu-usage-reduce-plan.md`.

### 1.2 Remove avoidable snapshot logging overhead

- [x] Inspect `eggthreads/eggthreads/snapshot.py` logging.
  - Confirmed per-event `logger.info(f"...")` eager f-strings inside snapshot rebuild.
- [x] Convert to guarded/lazy debug logging or remove noisy info logs if tests indicate no dependency.
  - Converted the per-event logs to `logger.debug(..., args)` guarded by `logger.isEnabledFor(logging.DEBUG)`.
- [x] Run snapshot/continue/thread tests.
  - `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py -q` (14 passed).
- [x] Update this plan.
  - Files touched: `eggthreads/eggthreads/snapshot.py`, `cpu-usage-reduce-plan.md`.

### 1.3 Avoid per-tool-delta `MAX(chunk_seq)` queries

- [ ] Inspect tool streaming helpers in `eggthreads/eggthreads/runner.py`.
  - Known risk from analysis: `emit_tool_stream_delta()` calls `db.max_chunk_seq(invoke_id) + 1` for each tool-output stream event.
- [ ] Reuse a local chunk sequence allocator for tool streams, matching the LLM streaming path.
- [ ] Keep event ordering and `events_delta_unique` invariant intact.
- [ ] Add/update focused test if existing coverage does not catch chunk sequence continuity.
- [ ] Run tool streaming/tool timeout tests.
- [ ] Update this plan.

### 1.4 Fix silent live TPS issue in web stats route

- [x] Inspect `eggw/eggw/routes/stats.py`.
  - Confirmed correctness issue: route referenced `datetime` without importing it; exception was swallowed and live TPS became `None`.
- [x] Add the missing import or use an existing shared time helper if local and simple.
  - Used timezone-aware `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")` locally.
- [x] Run focused web route tests.
  - `python -m pytest eggw/tests/test_api.py::TestTokenStats -q` (2 passed).
- [x] Update this plan.
  - Files touched: `eggw/eggw/routes/stats.py`, `eggw/tests/test_api.py`, `cpu-usage-reduce-plan.md`.

### 1.5 Throttle/cache live stats in TUI panel updates

- [ ] Inspect `egg/egg/app.py`, `egg/egg/panels.py`, `egg/egg/formatting.py`, and `egg/egg/streaming.py`.
  - Known issue: panel loop calls `current_token_stats()` frequently; that can call `total_token_stats()` and scan streaming tail.
- [ ] Add a small cache/throttle for token stats/TPS while preserving prompt responsiveness.
  - Prefer event-ingestion dirty flags from `EventWatcher` over simple time-based polling if the change stays small.
  - If starting with throttle, use a short interval such as 250–500ms during streaming and longer while idle.
- [ ] Ensure typing/rendering latency does not regress.
- [ ] Run TUI streaming tests.
- [ ] Update this plan.

## Phase 2 — Reduce repeated event-log scans

### 2.1 Design a single per-thread event reducer

- [ ] Inventory all callers that reconstruct state from events.
  - `build_tool_call_states()`.
  - `_last_stream_close_seq()`.
  - `_iter_messages_after()`.
  - `discover_runner_actionable()`.
  - `thread_state()`.
  - child status/wait helpers.
  - web tool/state routes.
- [ ] Write down the reducer output shape before coding.
  - Candidate fields:
    - `max_event_seq`,
    - `skipped_msg_ids`,
    - `last_llm_boundary_seq`,
    - `messages_after_boundary`,
    - `tool_call_states`,
    - `next_runner_actionable`,
    - `coarse_thread_state`,
    - `pending_approval_summary`,
    - `recent_error_summary` if cheap.
- [ ] Decide cache key and invalidation.
  - Candidate key: `(str(db.path), thread_id, max_event_seq)`.
  - Cache must be process-local and rebuildable.
- [ ] Add golden tests before replacing multiple callers.
- [ ] Update this plan with design decisions before implementation.

### 2.2 Implement reducer behind existing APIs

- [ ] Add reducer in the smallest appropriate module.
  - Prefer `eggthreads/eggthreads/tool_state.py` only if it avoids a new public API.
  - If a new internal module is necessary, keep it private and ask user first if scope grows.
- [ ] Migrate one caller at a time.
  - Start with `discover_runner_actionable_cached()` / `thread_state()` because they are hottest.
  - Then migrate web/tool/status routes if needed.
- [ ] Keep old behavior covered by tests.
- [ ] Benchmark or at least compare number of SQL queries/scans before and after.
- [ ] Update this plan after each migrated caller.

### 2.3 Bulk scheduler queries

- [ ] Inspect `SubtreeScheduler.run_forever()` in `eggthreads/eggthreads/runner.py`.
- [ ] Cache subtree membership or fetch it with a recursive CTE.
- [ ] Batch per-loop state:
  - max event seq by thread,
  - active open streams,
  - scheduling priorities/settings.
- [ ] Replace `q.pop(0)` BFS with `collections.deque` if still applicable.
- [ ] Preserve sticky scheduling semantics and lease-expiration behavior.
- [ ] Run scheduler tests.
- [ ] Update this plan.

## Phase 3 — Incremental snapshots and token stats

### 3.1 Make snapshots incremental for append-only message events

- [ ] Document current snapshot semantics from tests.
  - skipped messages,
  - provider-specific fields,
  - timestamps,
  - token stats,
  - `short_recap` extraction.
- [ ] Implement an incremental path for normal `msg.create` after `snapshot_last_event_seq`.
- [ ] Keep full rebuild for:
  - `msg.edit`,
  - `msg.delete`,
  - `control.interrupt` / continue,
  - unknown/corrupt state,
  - tests that require exact rebuild.
- [ ] Ensure snapshots remain faithful enough for provider request reconstruction.
- [ ] Run snapshot, continue, model inheritance, and integration workflow tests.
- [ ] Update this plan.

### 3.2 Cache per-message token counts

- [ ] Inspect `eggthreads/eggthreads/token_count.py` and snapshot token stats.
- [ ] Ensure old messages are not re-tokenized when only new tail events arrive.
- [ ] Cache live streaming token counts incrementally from deltas.
- [ ] Preserve approximate/cost semantics.
- [ ] Run token-count tests and UI stats tests.
- [ ] Update this plan.

## Phase 4 — Event-driven UI/web behavior

### 4.1 Web backend shared event fanout

- [ ] Inspect `eggw/eggw/routes/events.py` and current `EventWatcher` usage.
- [ ] Design one watcher per active thread/root with subscriber queues for SSE/WebSocket.
- [ ] Keep reconnect/replay semantics intact:
  - if stream is in progress, replay from stream open,
  - otherwise start at current max seq.
- [ ] Avoid one SQLite polling loop per browser tab.
- [ ] Run web API/e2e tests where possible.
- [ ] Update this plan.

### 4.2 Web route costs

- [ ] Replace `/messages` fresh full snapshot rebuild with cached snapshot + tail reconciliation.
- [ ] Replace 1-second `threadSettings` polling with SSE invalidation on relevant events.
- [ ] Ensure multiple components do not independently trigger the same expensive stats route.
- [ ] Update this plan.

### 4.3 TUI dirty/event-driven panels

- [ ] Identify which panel values need which events.
  - token stats: `stream.delta`, `stream.close`, `msg.create`, snapshot changes.
  - sandbox status: `sandbox.config`.
  - tool approvals: `tool_call.*`, relevant `msg.create`.
  - tree: child/thread creation/deletion events or commands.
- [ ] Replace per-tick DB recomputation with cached values invalidated by watcher/commands.
- [ ] Keep immediate input echo and stream rendering.
- [ ] Run TUI tests and manually check interactive feel if possible.
- [ ] Update this plan.

### 4.4 Incremental terminal rendering

- [ ] Inspect `eggdisplay/eggdisplay/renderers.py` full-screen stream buffer and paint logic.
- [ ] Avoid reparsing the entire accumulated stream buffer on every flush.
  - Maintain incremental wrapped rows with current style/column state.
  - Rebuild only on terminal-width changes.
- [ ] Avoid copying full scrollback on each paint; slice visible rows by index.
- [ ] Render only dirty panels where feasible.
- [ ] Run `eggdisplay` tests and TUI streaming tests.
- [ ] Update this plan.

## Phase 5 — Optional Go sidecars only after profiling

Do not start this phase until Phases 1–4 are measured and CPU remains a real problem.

- [ ] Decide whether Go is needed based on measurements.
- [ ] If yes, prefer isolated sidecars over a rewrite.

### Candidate sidecar A: event fanout daemon

- [ ] Read SQLite `events` only; no mutation initially.
- [ ] Serve SSE/WebSocket with one watcher per active thread.
- [ ] Preserve replay and ordering semantics.
- [ ] Easy rollback: web can switch back to Python route.

### Candidate sidecar B: process/tool supervisor

- [ ] Python runner sends command specs to Go supervisor.
- [ ] Go handles subprocess, timeout, process group kill, Docker stop, stdout/stderr streaming.
- [ ] Python still owns event semantics and tool-call state.

### Candidate sidecar C: LLM streaming router

- [ ] Go owns provider HTTP/SSE and cancellation.
- [ ] Python `eggllm` stays as compatibility client.
- [ ] Avoid per-token IPC overhead unless the router writes batched events directly or streams efficiently.

### Avoid until last: Go scheduler/runner

- [ ] Only consider after golden event-log tests are strong.
- [ ] Highest risk area: RA1/RA2/RA3, leases, continue/recovery, tool approvals, sandbox/session semantics.

## Brittleness reduction checklist

Before changing brittle core paths, make sure at least one test covers each touched behavior:

- [ ] No duplicate LLM calls after crash/retry/continue.
- [ ] Interrupted streams do not leave permanent active leases.
- [ ] Expired leases can be taken over.
- [ ] Tool calls progress correctly through TC1–TC6.
- [ ] Denied tools publish the correct tool message.
- [ ] Long tool output is stashed and previewed correctly.
- [ ] User tool calls and assistant tool calls remain distinct.
- [ ] `no_api`, `keep_user_turn`, and local tool messages remain respected.
- [ ] Model switching and provider-specific fields round-trip.
- [ ] Persistent REPL/session tool calls still work.
- [ ] Web/TUI stream replay from mid-stream still works.
- [ ] Snapshot cache and event log remain consistent after continue/edit/delete.

## Measurement notes to fill in later

Record results here as work proceeds.

- TUI idle CPU baseline: not measured yet.
- Web idle CPU baseline: not measured yet.
- Long stream CPU baseline: not measured yet.
- Scheduler many-thread baseline: not measured yet.
- After Phase 1 results: not measured yet.
- After Phase 2 results: not measured yet.
- After Phase 3 results: not measured yet.
- After Phase 4 results: not measured yet.

## Known risks / open questions

- Some current tests may implicitly rely on exact event ordering and snapshot timing; preserve or explicitly document any change.
- Current runtime has read-only `/workspace/.egg`; Docker session tests that create `.egg/rlm_sessions` under repo root can fail for environment reasons. Prefer tests that use `tmp_path`/memory provider unless verifying Docker directly.
- Event batching must not hide deltas long enough to hurt perceived streaming latency.
- Derived caches must never become authoritative; always support full rebuild from event log.
- Web and TUI can run against the same DB from different processes, so in-process notifications are not enough by themselves.
- Adding indexes can improve reads but slow stream-heavy writes; measure before and after.
- Go sidecars may add IPC latency and deployment complexity; only introduce them after Python-side fixes are insufficient.
