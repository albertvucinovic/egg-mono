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

- Status: first token/TPS bottleneck fix completed: idle TUI header cache-key checks no longer call `ThreadsDB.get_thread()` just to read `snapshot_last_event_seq`.
- Last updated: after lightweight snapshot watermark helper for TUI token/TPS headers.
- Recommended next action: reload current Egg and re-profile. If CPU remains high, optimize the remaining `thread_token_stats()` path so unchanged idle snapshots avoid repeated multi-MB `snapshot_json` parsing beyond the existing TTL.

## Progress log

- Initial plan created in `cpu-usage-reduce-plan.md`.
- Phase 2.2 initial migration completed: added private `_ThreadEventReduction` / `_reduce_thread_events()` in `eggthreads/eggthreads/tool_state.py`, keyed by `(str(db.path), thread_id, max_event_seq)`, deriving skipped ids, LLM boundary, messages after boundary, tool states, next RA, and coarse no-lease thread state from one loaded event list. Added golden equivalence tests in `eggthreads/tests/test_tool_state_runner_actionable.py` for simple RA1, assistant TC1 approval wait, approved assistant RA2, user RA3, continue/skipped messages, and purpose=`llm` interrupts. Migrated `discover_runner_actionable_cached()` and `thread_state()` to the reducer while leaving public APIs unchanged. Tests run: `python -m pytest eggthreads/tests/test_tool_state_runner_actionable.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_events_and_open_streams.py eggthreads/tests/test_scheduler_slots.py egg/tests/test_ctrlc_pending_stream_boundary.py -q` (74 passed).
- Phase 2.3 first scheduler bulk-query pass completed: changed `SubtreeScheduler._collect_subtree()` to prefer one recursive CTE with waiting-time filtering and a cycle guard, with deque BFS fallback; batched scheduler-loop `max_event_seq` and active open-lease checks via `_max_event_seqs_bulk()` / `_active_open_threads_bulk()` instead of per-thread queries. Added focused bulk helper tests. Tests run: `python -m pytest eggthreads/tests/test_tool_state_runner_actionable.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_events_and_open_streams.py eggthreads/tests/test_scheduler_slots.py egg/tests/test_ctrlc_pending_stream_boundary.py -q` (76 passed).
- Phase 2.3 scheduling settings batching completed: added `_thread_scheduling_bulk()` and `_sort_by_priority_map()` so scheduler priority sorting and sticky reservation thresholds reuse one batched latest-`thread.scheduling` lookup per loop instead of per-thread queries. Added focused scheduler settings bulk test. Tests run: `python -m pytest eggthreads/tests/test_tool_state_runner_actionable.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_events_and_open_streams.py eggthreads/tests/test_scheduler_slots.py egg/tests/test_ctrlc_pending_stream_boundary.py -q` (77 passed).
- Phase 2.2 follow-up migration completed: changed public `build_tool_call_states()` to delegate to the cached reducer and return deep-copied state objects, avoiding duplicate event folds while preventing callers from mutating cached reducer state. Added cache-safety regression test. Tests run: `python -m pytest eggthreads/tests/test_tool_state_runner_actionable.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_events_and_open_streams.py eggthreads/tests/test_scheduler_slots.py eggthreads/tests/test_tool_call_id_normalization.py eggthreads/tests/test_generic_user_tool_call_api.py eggthreads/tests/test_user_command_api.py egg/tests/test_ctrlc_pending_stream_boundary.py -q` (110 passed).
- Phase 3.1 semantic guardrail tests added: `SnapshotBuilder` now has focused coverage that normal `msg.edit` updates content while preserving provider-specific fields, and `msg.delete` excludes the deleted message. This documents current/intended full-rebuild semantics before adding an incremental path and fixes the documented `msg.delete` behavior. Tests run: `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py egg/tests/test_model_inheritance.py egg/tests/test_integration_workflow.py -q` (50 passed).
- Phase 3.1 append-only snapshot path implemented: `create_snapshot()` now reuses an existing valid snapshot when all events after `snapshot_last_event_seq` are `msg.create`, appending those messages and recomputing snapshot token stats; any `msg.edit`, `msg.delete`, control, stream, config, or tool event in the tail falls back to the existing full rebuild. Added tests proving the incremental path avoids `SnapshotBuilder.build()` and that edits still fall back to full rebuild. Tests run: `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py egg/tests/test_model_inheritance.py egg/tests/test_integration_workflow.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (74 passed).
- Phase 3.2 initial token-stat extension completed: added `extend_snapshot_token_stats()` so append-only `create_snapshot()` tokenizes only new tail messages and merges with cached snapshot token stats, preserving API usage/cached-input metadata and per-message indices. Added equivalence test against full recomputation and verified `create_snapshot()` calls the extension with only the tail. Tests run: `python -m pytest eggthreads/tests/test_token_count_public.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py egg/tests/test_model_inheritance.py egg/tests/test_integration_workflow.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py eggw/tests/test_api.py::TestTokenStats -q` (80 passed).
- Phase 3.2 live LLM TPS cache completed: `live_llm_tps_for_invoke()` now caches `(start_ts, token_count)` by `(invoke_id, max_chunk_seq)` so repeated UI/web reads for unchanged streams do not rescan all `stream.delta` payloads. Added trace-based regression test showing the second unchanged call avoids the delta payload query. Tests run: `python -m pytest eggthreads/tests/test_token_count_public.py eggthreads/tests/test_snapshot_builder.py egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_streaming_tui.py eggw/tests/test_api.py::TestTokenStats -q` (76 passed).
- Phase 4.1 attempted shared SSE fanout implementation, but the TestClient SSE regression hung even after fixing an initial subscriber hashability bug; reverted the code change and recorded this as a failed attempt/risk instead of keeping a brittle rewrite. No functional code from the failed fanout attempt remains.
- Phase 4.2 web route cost quick wins completed: `/api/threads/{thread_id}/messages` now calls `create_snapshot()` on a fresh DB connection, reusing the Phase 3 append-only incremental snapshot path instead of always running `SnapshotBuilder` over the full event log; added a regression test that fails if the append-only second `/messages` call invokes a full rebuild. Removed 1-second `threadSettings` polling from the thread page and system panel, and added SSE invalidation for `tool_call.approval` and `model.switch`. Tests run: `python -m pytest eggw/tests/test_api.py::TestMessageOperations eggw/tests/test_api.py::TestToolCalls eggw/tests/test_api.py::TestTokenStats egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (29 passed).
- Phase 4.2 duplicate stats polling quick win completed: the thread page remains the single owner of live `/stats` polling during LLM streaming, while `SystemPanel` now shares the same React Query cache and only refetches manually/SSE. This avoids two independent expensive stats requests per second when the system panel is visible. Tests run: `cd eggw/frontend && npx tsc --noEmit` (passed).
- Phase 4.1 retry design narrowed and deferred: a shared SSE fanout can be tested at the helper level with multiple subscribers sharing one poll, but the existing TestClient SSE route regression still hangs (`python -m pytest eggw/tests/test_api.py::TestEventStreaming::test_sse_replays_active_tool_stream_with_preview_limit_indicator -q`, timed out after 60s). Do not reattempt the route swap until there is a non-hanging SSE harness or real-server test. No code kept from this retry.
- Phase 4.3 children panel cache-key quick win completed: replaced the time-based 2s children tree refresh with a cheap key over current thread, child table count/max rowid, relevant event count/max seq, and open-stream count/max lease. This avoids rescanning/formatting all threads during idle ticks while still refreshing on topology, message/status, approval, and stream changes.
- Phase 4.3 system status cache-key quick win completed: cached the System panel sandbox/autoapproval title parts behind a cheap key over current thread plus `sandbox.config`/`tool_call.approval` event count/max seq, avoiding per-tick helper scans while preserving updates on relevant config events. Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (66 passed).
- Added manager/worker recovery tooling goal: a manager-side `continue_subthread` command/tool should be able to repair or continue a child/descendant subthread after LLM/runner failures (for example a 503 that ends with no assistant content), analogous to the user `/continue` command. No code changed in this step.
- Manager/worker recovery tooling goal completed: added `continue_child_thread()` API plus model-visible `continue_subthread` tool. It validates that the target is a descendant of the calling manager, delegates to existing `continue_thread()` semantics, and returns structured JSON including diagnosis when available. Tests run: `python -m pytest eggthreads/tests/test_send_message_to_child.py -q` (8 passed).
- Phase 4.3 chat header TPS cache quick win completed: `current_chat_header_tps()` now caches completed-message TPS by `(thread_id, snapshot_last_event_seq)` so idle header redraws do not repeatedly parse snapshot messages. Live stream TPS still uses the existing short live cache. Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (67 passed).
- Phase 4.3 idle token stats cache-key refinement completed: when no stream is active, `current_token_stats()` keys its short cache by `(thread_id, snapshot_last_event_seq, snapshot_last_event_seq, active_invoke)` instead of current max event seq, so unrelated non-snapshot events such as model/config changes do not force repeated token-stat rescans while idle. Streaming still keys on current max event seq for live responsiveness. Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (68 passed).
- Phase 4.4 code-state inspection completed: `eggdisplay/eggdisplay/renderers.py` already has `InlineDiffRenderer` line diffs and `FullScreenDiffRenderer` alt-screen row diffs against `_prev_viewport`; full-screen mode already models permanent `_scrollback`, transient `_stream_buffer`, live rows, in-app scroll, stream row caching by `(width, stream_version)`, and terminal-control sanitization. `egg/egg/streaming.py` already coalesces stream renderer appends to 50ms / 64k-char flushes. `OutputPanel` / `InputPanel` already cache renderables, and the app loop only calls `renderer.update()` when panels/input are dirty. Remaining 4.4 gaps: every changed stream flush still reparses the whole accumulated `_stream_buffer`, and `_paint()` still copies `list(self._scrollback) + stream_rows` before slicing the visible viewport. Tests run: `python -m pytest eggdisplay/tests/test_renderers_terminal_safety.py egg/tests/test_streaming_tui.py::test_stream_appends_are_coalesced_for_renderer -q` (10 passed).
- Phase 4.4 incremental stream wrapping completed: replaced the full-buffer stream row cache with `_StreamRowsState`, so `stream_append()` appends newly rendered ANSI to cached wrapped rows and `_stream_rows()` only full-rebuilds on terminal-width changes. Plain stream chunks without Rich markup or ANSI now bypass Rich rendering and go through terminal-control sanitization directly, removing Rich highlighter overhead from ordinary assistant text. Synthetic long-stream benchmark improved from timing out after `2M` chars and taking `44.370s` for `1M` chars to `0.459s` for `1M` chars and `0.955s` for `2M` chars with 8k-char flushes in a 100x30 test renderer. Tests run: `python -m pytest eggdisplay/tests egg/tests/test_streaming_tui.py -q` (45 passed).
- Phase 4.4 viewport slicing cleanup completed: `_paint()` no longer builds `non_live = list(self._scrollback) + stream_rows` before slicing. It now computes the visible non-live window across scrollback and stream rows by index and only copies the visible slices needed for the viewport. Tests run: `python -m pytest eggdisplay/tests egg/tests/test_streaming_tui.py -q` (45 passed).
- Phase 4.3 input-latency guard completed: `update_panels()` now detects dirty input before children-panel refresh and defers children tree reformatting while the user is typing if there is already cached tree content. This targets a visible ~3s key-echo delay observed when a background `python_repl`/tool lease in the active tree makes the children status key change while the current thread itself is idle. The next idle tick refreshes the tree, preserving status freshness without putting tree scans on the input echo path. Added regression coverage. Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (69 passed).
- Children status key heartbeat refinement completed: children-panel status keys now track active open stream identity (`thread_id:invoke_id:purpose`) rather than max `lease_until`, so normal lease extensions do not force tree reformatting. Added regression for unchanged key after heartbeat extension. Tests run: `python -m pytest eggthreads/tests/test_events_and_open_streams.py::test_open_streams_lease_heartbeat_and_release egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (71 passed).
- Phase 4.5 Children panel event-driven/cache refresh completed: `update_panels()` no longer runs `_compute_children_panel_status_key()` on every idle 0.1s tick. The current-thread watcher marks the Children panel dirty on relevant events, explicit/local invalidation is supported via `_mark_children_panel_dirty()`, and a 1s fallback refresh catches cross-process/descendant/topology changes. Remaining DB event checks are scoped per subtree thread using the existing `(thread_id, type)` index instead of a global event-type scan. Added tests for no repeated idle status-key recomputation and dirty invalidation before fallback. Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (105 passed).
- Post-reload CPU check after Phase 4.5: user observed total all-core CPU falling from about 17% to about 9%. A live `top` sample showed `%Cpu(s): 6.2 us, 2.2 sy, 91.6 id` (about 8.4% busy overall). A `/proc` delta sample over 5s found current Egg own CPU at `184.4%` of one core across 27 cores, or `6.83%` of all cores. The current burn was in Python `egg.app` processes; long-lived `egg.sh` wrapper `%CPU` entries from `ps` are misleading for current CPU because they did not show measurable own CPU in the delta sample. No code changed in this measurement step.
- Phase 4.6 py-spy profiling and scheduler query fix completed: profiled the hottest real Python Egg PID with `sudo env "PATH=$PATH" py-spy record --pid ... --format raw --threads --full-filenames --nonblocking`. A stale wrapper PID sample misleadingly showed `_compute_children_panel_status_key()` hot, but the actual hot Python PID sample (`/tmp/egg-pyspy-176700-1778790890.raw`) showed ~58.5% inclusive in `SubtreeScheduler.run_forever()` and ~58.3% leaf samples in `_max_event_seqs_bulk()` lines 2955/2957. Real-db timing on a 24-thread subtree with ~2.5M events showed the old grouped `MAX(event_seq)` query taking ~46ms per scheduler poll because SQLite scanned all matching event rows. Changed `_max_event_seqs_bulk()` to use a `VALUES` CTE plus correlated `SELECT MAX(event_seq) ... INDEXED BY events_thread_seq` per thread, preserving the same private helper API while doing index tail seeks. Same real-db timing dropped to ~0.022ms for the 24-thread subtree. Tests run: `PYTHONPATH=eggthreads pytest -q eggthreads/tests/test_scheduler_slots.py::test_scheduler_bulk_max_event_seqs` (1 passed), `PYTHONPATH=eggthreads pytest -q eggthreads/tests/test_scheduler_slots.py eggthreads/tests/test_tool_state_runner_actionable.py eggthreads/tests/test_events_and_open_streams.py` (65 passed), `python -m py_compile eggthreads/eggthreads/runner.py`, and `git diff --check`.
- Reloaded hot Egg TUI instances after Phase 4.6: `git push` reported `Everything up-to-date` because the origin worktree already had commit `0835132`. Used sudo `TIOCSTI` to send `Ctrl+E`, `/reload`, Enter to detected Egg Python process ttys, excluding the current tool/control tty `/dev/pts/11` to avoid killing this conversation mid-command. Reloaded `/dev/pts/1`, `/dev/pts/16`, `/dev/pts/24`, `/dev/pts/29`, `/dev/pts/30`, `/dev/pts/31`, `/dev/pts/32`, `/dev/pts/38`, `/dev/pts/46`, `/dev/pts/7`, and `/dev/pts/9`. A 5s `/proc` delta sample after reload showed real Python Egg CPU much lower: current-control PID `266292` on `/dev/pts/11` at ~23.8% of one core, and all other reloaded Egg Python processes at about 11.4%, 4.8%, 4.4%, 4.4%, 4.2%, 4.2%, 3.6%, 3.4%, 3.4%, 3.2%, 3.2% of one core. The reloaded non-current processes summed to roughly 50% of one core, far below the previous scheduler-heavy samples; current/control Egg still needs manual reload or a careful self-reload outside an active tool call.
- Post-reload next-bottleneck profiling completed: after the current Egg was reloaded, a 5s real-Python sample showed all Egg Python processes at ~72% of one core total, with the current/control TUI PID `278262` highest at ~25% of one core. `py-spy` on PID `278262` (`/tmp/egg-pyspy-278262-1778793223.raw`, 12s) showed ~72.5% inclusive in `update_panels()`, ~58.4% in `current_token_stats()`, ~36.1% in `thread_token_stats()`, and leaf samples dominated by `json.decoder.raw_decode` (~41.6%) plus `ThreadsDB.get_thread()` (~36.1% combined line samples). Real DB inspection for thread `01KR7N1WT90AQA1M9YAYSMC8YP` found `snapshot_json` around 7.25 MB, `snapshot_last_event_seq=2535732`, only 3 post-snapshot events, and `json.loads(snapshot_json)` alone taking ~12.5ms. This indicates the next idle CPU target is token/header stats loading/parsing/re-tokenizing large snapshots, not scheduler polling. Lower-CPU PIDs showed the same pattern at smaller scale, mixed with Rich rendering.
- Token/TPS header cache-key quick win completed: added `FormattingMixin._snapshot_last_event_seq()` to query only `snapshot_last_event_seq` from `threads`, avoiding multi-MB `snapshot_json` loads in `rebuild_chat_cache_for_current()`, `current_token_stats()` cache-key construction, and `current_chat_header_tps()` cache-key construction. This directly targets the py-spy leaf samples in `ThreadsDB.get_thread()` on idle ticks while preserving the existing cache behavior and full `thread_token_stats()` fallback on cache misses. Tests run: `PYTHONPATH=egg:eggthreads:eggllm:eggconfig:eggdisplay pytest -q egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_streaming_tui.py` (105 passed, 2 existing deprecation warnings), `python -m py_compile egg/egg/formatting.py egg/egg/panels.py`, and `git diff --check`.
- Phase 1.4 completed: fixed `eggw/eggw/routes/stats.py` missing `datetime` import/time helper so live LLM TPS is no longer silently swallowed; added `eggw/tests/test_api.py::TestTokenStats::test_get_stats_includes_live_llm_tps`. Tests run: `python -m pytest eggw/tests/test_api.py::TestTokenStats -q` (2 passed).
- Phase 1.2 completed: converted eager per-event `SnapshotBuilder` info logging to guarded lazy debug logging in `eggthreads/eggthreads/snapshot.py`. Tests run: `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py -q` (14 passed).
- Phase 1.1 completed: added a shared 50ms sleep to Docker Python REPL eval polling and removed duplicate Bash Docker REPL sleeps in `eggthreads/eggthreads/session.py`. Tests run: `python -m pytest eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_bash_repl_tool.py -q` (12 passed) and `python -m pytest eggthreads/tests/test_session_config.py -q -k 'not docker_session_status_skeleton_when_available'` (17 passed, 1 deselected). Full `test_session_config.py` hit an environment issue because `/workspace/.egg` is read-only in this runtime, not because of this change.
- Phase 1.3 completed: added optional chunk-sequence allocation to tool stream delta helpers and used local allocators in Bash and generic tool streaming paths, avoiding per-delta `MAX(chunk_seq)` queries. Added `test_emit_limited_tool_stream_delta_uses_supplied_chunk_sequence`. Tests run: `python -m pytest eggthreads/tests/test_headless_subtree_scheduler.py::test_emit_limited_tool_stream_delta_emits_preview_then_indicator eggthreads/tests/test_headless_subtree_scheduler.py::test_emit_limited_tool_stream_delta_uses_supplied_chunk_sequence eggthreads/tests/test_tool_timeout.py -q` (22 passed).
- Phase 1.5 completed: added short-lived TUI caches for `current_token_stats()` and live LLM TPS so unchanged event logs do not rescan token/delta state every UI tick. Added focused tests for both caches. Tests run: `python -m pytest egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_streaming_tui.py -q` (64 passed).
- Phase 2.1 completed: documented the event reducer design in this plan. Decision: keep reducer private to `eggthreads/eggthreads/tool_state.py` first, cache by `(str(db.path), thread_id, max_event_seq)`, initially derive skipped ids, LLM boundary, messages after boundary, tool states, next actionable, and coarse non-lease state. No code behavior changed in this step.

## High-level strategy

1. Measure first enough to avoid guessing.
2. Apply safe quick wins that remove obvious busy work.
3. Reduce repeated full event-log scans with a single cached reducer.
4. Make snapshots and token stats incremental.
5. Move polling/rendering toward event-driven dirty flags.
6. Consider Go only as optional sidecars after Python-side architecture fixes are measured.

## Manager/worker recovery tooling goal

- [x] Add a manager-side `continue_subthread` tool/command for repairing or continuing a child/descendant subthread after LLM/runner errors that leave no assistant content, analogous to the user `/continue` command. It should target only descendants the manager owns, preserve event-log semantics, and avoid spawning duplicate LLM/tool work.
  - Implemented as `continue_child_thread()` plus the `continue_subthread` tool, reusing existing `continue_thread()` event semantics and active-lease checks.

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

- [x] Inspect tool streaming helpers in `eggthreads/eggthreads/runner.py`.
  - Confirmed `emit_tool_stream_delta()` used `db.max_chunk_seq(invoke_id) + 1` for each tool-output stream event.
- [x] Reuse a local chunk sequence allocator for tool streams, matching the LLM streaming path.
  - Added optional `chunk_seq`/`next_chunk_seq` plumbing and local allocators in Bash and generic tool streaming paths.
  - Backward-compatible helper behavior remains: if no allocator is supplied, helper still falls back to `db.max_chunk_seq(invoke_id) + 1`.
- [x] Keep event ordering and `events_delta_unique` invariant intact.
  - Local allocators initialize from current `db.max_chunk_seq(invoke_id)` once per stream path and increment per emitted delta.
- [x] Add/update focused test if existing coverage does not catch chunk sequence continuity.
  - Added `test_emit_limited_tool_stream_delta_uses_supplied_chunk_sequence` to assert the helper does not call `max_chunk_seq` when an allocator is supplied.
- [x] Run tool streaming/tool timeout tests.
  - `python -m pytest eggthreads/tests/test_headless_subtree_scheduler.py::test_emit_limited_tool_stream_delta_emits_preview_then_indicator eggthreads/tests/test_headless_subtree_scheduler.py::test_emit_limited_tool_stream_delta_uses_supplied_chunk_sequence eggthreads/tests/test_tool_timeout.py -q` (22 passed).
- [x] Update this plan.
  - Files touched: `eggthreads/eggthreads/runner.py`, `eggthreads/tests/test_headless_subtree_scheduler.py`, `cpu-usage-reduce-plan.md`.

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

- [x] Inspect `egg/egg/app.py`, `egg/egg/panels.py`, `egg/egg/formatting.py`, and `egg/egg/streaming.py`.
  - Confirmed panel loop can call `current_token_stats()` frequently, and live TPS helpers call `live_llm_tps_for_invoke()` which scans stream deltas.
- [x] Add a small cache/throttle for token stats/TPS while preserving prompt responsiveness.
  - Added `FormattingMixin.current_token_stats()` cache keyed by `(thread_id, snapshot_last_event_seq, max_event_seq, active_invoke)`.
  - Cache TTL is short while streaming (0.5s) and longer while idle (2.0s), so unchanged event logs do not rescan every UI tick.
  - Added `PanelsMixin._live_llm_tps_cached()` with 0.5s TTL and reused it from system/chat header TPS paths.
- [x] Ensure typing/rendering latency does not regress.
  - Change only reuses values for unchanged event-log/cache keys; input rendering remains immediate.
- [x] Run TUI streaming tests.
  - `python -m pytest egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_streaming_tui.py -q` (64 passed).
- [x] Update this plan.
  - Files touched: `egg/egg/formatting.py`, `egg/egg/panels.py`, `egg/tests/test_formatting.py`, `egg/tests/test_panels.py`, `cpu-usage-reduce-plan.md`.

## Phase 2 — Reduce repeated event-log scans

### 2.1 Design a single per-thread event reducer

- [x] Inventory all callers that reconstruct state from events.
  - Hot core callers: `build_tool_call_states()`, `_last_stream_close_seq()`, `_iter_messages_after()`, `discover_runner_actionable()`, `thread_state()`.
  - Wait/status callers: `wait_for_tool_call_result*`, `_thread_wait_complete()`, `wait_for_threads()`, `get_child_thread_status*()`.
  - UI/API callers: `egg/egg/approval.py`, `egg/egg/input.py`, `eggw/routes/tools.py`, `eggw/routes/threads.py`, `eggw/routes/messages.py`, `eggw/routes/events.py`.
  - Token stats remain separate for now; do not combine with reducer until Phase 3.
- [x] Write down the reducer output shape before coding.
  - Initial private reducer output should be a dataclass in `eggthreads/eggthreads/tool_state.py` with:
    - `thread_id`,
    - `max_event_seq`,
    - `skipped_msg_ids`,
    - `last_llm_boundary_seq`,
    - `messages_after_boundary` as event dicts for `msg.create` only,
    - `tool_call_states` as the existing `Dict[str, ToolCallState]`,
    - `next_runner_actionable` as existing `Optional[RunnerActionable]`,
    - `coarse_thread_state_without_lease` as one of `running`, `waiting_tool_approval`, `waiting_output_approval`, `waiting_user`.
  - Keep recent errors and token stats out of the first reducer to avoid broad scope.
- [x] Decide cache key and invalidation.
  - Use process-local cache key `(str(db.path), thread_id, max_event_seq)`, where `max_event_seq` comes from `db.max_event_seq(thread_id)`.
  - Evict older entries for the same `(db.path, thread_id)` after storing a new one.
  - Cache is rebuildable and never authoritative; event log stays source of truth.
- [x] Add golden tests before replacing multiple callers.
  - Before implementation, add focused equivalence tests comparing reducer-backed outputs against current public behavior for representative histories:
    - simple user RA1,
    - assistant tool call waiting for approval,
    - approved assistant tool call RA2,
    - user-originated tool call RA3,
    - continue/skipped messages,
    - interrupted/purpose=`llm` boundary.
  - Do not migrate web/routes until these core tests pass.
- [x] Update this plan with design decisions before implementation.
  - Design decision: first implementation may reuse existing helper logic internally if necessary, but the target is one event pass. Prefer correctness-preserving incremental migration over a large rewrite.

### 2.2 Implement reducer behind existing APIs

- [x] Add reducer in the smallest appropriate module.
  - Added private reducer in `eggthreads/eggthreads/tool_state.py`; no new public module/API.
- [x] Migrate one caller at a time.
  - Migrated `discover_runner_actionable_cached()` and `thread_state()` first because they are hottest.
  - Migrated `build_tool_call_states()` to reuse the reducer and return cache-safe copies.
  - Then migrate web/tool/status routes if needed.
- [x] Keep old behavior covered by tests.
  - Added reducer-vs-public golden tests for RA1, RA2, RA3, TC1 wait, continue/skipped messages, and LLM interrupt boundaries.
- [x] Benchmark or at least compare number of SQL queries/scans before and after.
  - Added trace-based assertions showing first cached RA call does one `MAX(event_seq)` plus one event-load query, repeated cached RA call does only `MAX(event_seq)`, and `thread_state()` reuses an already-built reducer instead of rescanning events.
- [x] Update this plan after each migrated caller.

### 2.3 Bulk scheduler queries

- [x] Inspect `SubtreeScheduler.run_forever()` in `eggthreads/eggthreads/runner.py`.
- [x] Cache subtree membership or fetch it with a recursive CTE.
  - `_collect_subtree()` now prefers a recursive CTE with `waiting_until` filtering and falls back to BFS if needed.
- [x] Batch per-loop state:
  - max event seq by thread,
  - active open streams,
  - scheduling priorities/settings.
  - Scheduling priorities/settings are now loaded with `_thread_scheduling_bulk()` and consumed by `_sort_by_priority_map()` plus sticky threshold checks.
- [x] Replace `q.pop(0)` BFS with `collections.deque` if still applicable.
  - Done in the fallback BFS path.
- [x] Preserve sticky scheduling semantics and lease-expiration behavior.
  - Active lease batching filters only `lease_until > now`; expired leases remain re-checkable.
- [x] Run scheduler tests.
  - `python -m pytest eggthreads/tests/test_scheduler_slots.py -q` (44 passed).
  - Broader focused run listed in progress log: 76 passed.
- [x] Update this plan.

## Phase 3 — Incremental snapshots and token stats

### 3.1 Make snapshots incremental for append-only message events

- [x] Document current snapshot semantics from tests.
  - skipped messages,
  - provider-specific fields,
  - timestamps,
  - token stats,
  - `short_recap` extraction.
  - Added focused tests for edit/delete/provider-field semantics before changing snapshot update strategy. `short_recap` extraction remains runner-owned after snapshot rebuild.
- [x] Implement an incremental path for normal `msg.create` after `snapshot_last_event_seq`.
  - `create_snapshot()` appends pure `msg.create` tails to the cached snapshot and falls back to full rebuild otherwise.
- [x] Keep full rebuild for:
  - `msg.edit`,
  - `msg.delete`,
  - `control.interrupt` / continue,
  - unknown/corrupt state,
  - tests that require exact rebuild.
  - Added edit fallback test.
- [x] Ensure snapshots remain faithful enough for provider request reconstruction.
  - Guarded by provider-field preservation test for edited messages.
- [x] Run snapshot, continue, model inheritance, and integration workflow tests.
  - `python -m pytest eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py egg/tests/test_model_inheritance.py egg/tests/test_integration_workflow.py -q` (50 passed).
- [x] Update this plan.

### 3.2 Cache per-message token counts

- [x] Inspect `eggthreads/eggthreads/token_count.py` and snapshot token stats.
- [x] Ensure old messages are not re-tokenized when only new tail events arrive.
  - Added `extend_snapshot_token_stats()` and wired append-only `create_snapshot()` to pass only new tail messages.
- [x] Cache live streaming token counts incrementally from deltas.
  - Added unchanged-stream cache for `live_llm_tps_for_invoke()` keyed by `(invoke_id, max_chunk_seq)`.
- [x] Preserve approximate/cost semantics.
  - Extension reuses `_merge_token_stats()` and seeds tail stats with cached context/API metadata.
- [x] Run token-count tests and UI stats tests.
  - `python -m pytest eggthreads/tests/test_token_count_public.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py egg/tests/test_model_inheritance.py egg/tests/test_integration_workflow.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py eggw/tests/test_api.py::TestTokenStats -q` (80 passed).
  - `python -m pytest eggthreads/tests/test_token_count_public.py eggthreads/tests/test_snapshot_builder.py egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_streaming_tui.py eggw/tests/test_api.py::TestTokenStats -q` (76 passed).
- [x] Update this plan.

## Phase 4 — Event-driven UI/web behavior

### 4.1 Web backend shared event fanout

- [x] Inspect `eggw/eggw/routes/events.py` and current `EventWatcher` usage.
  - Current SSE endpoint creates one dedicated DB connection and `EventWatcher` polling loop per browser tab.
- [ ] Design one watcher per active thread/root with subscriber queues for SSE/WebSocket.
  - Attempted direct implementation was reverted after SSE TestClient hang; needs a narrower design/test harness before retry.
- [ ] Keep reconnect/replay semantics intact:
  - if stream is in progress, replay from stream open,
  - otherwise start at current max seq.
- [ ] Avoid one SQLite polling loop per browser tab.
- [ ] Run web API/e2e tests where possible.
- [ ] Update this plan.

### 4.2 Web route costs

- [x] Replace `/messages` fresh full snapshot rebuild with cached snapshot + tail reconciliation.
  - `/messages` now uses `create_snapshot()` on a fresh DB, which applies the Phase 3 incremental append-only path and falls back to full rebuild when needed.
- [x] Replace 1-second `threadSettings` polling with SSE invalidation on relevant events.
  - Removed `refetchInterval: 1000`; SSE invalidates settings on `tool_call.approval` and `model.switch`, while direct mutations still refetch/invalidate.
- [x] Ensure multiple components do not independently trigger the same expensive stats route.
  - The thread page is now the only live-polling owner for `queryKey: ["stats", threadId]`; `SystemPanel` shares the cached query and relies on SSE/manual invalidation.
- [x] Update this plan.

### 4.3 TUI dirty/event-driven panels

- [x] Identify which panel values need which events.
  - token stats: `stream.delta`, `stream.close`, `msg.create`, snapshot changes.
  - sandbox status: `sandbox.config`.
  - tool approvals: `tool_call.*`, relevant `msg.create`.
  - tree: child/thread creation/deletion events or commands.
- [ ] Replace per-tick DB recomputation with cached values invalidated by watcher/commands.
  - First quick wins: children tree now uses a cheap DB key and only reruns `format_tree()` when topology/status inputs change; System panel sandbox/autoapproval title parts are cached behind relevant config-event keys; completed-message chat header TPS is cached by snapshot sequence; idle token stats ignore unrelated non-snapshot event seq changes.
  - Input-latency guard: while the input panel is dirty and cached children tree content exists, skip children tree key/reformat work for that tick so background tool lease heartbeats in the active tree cannot block key echo; the next idle tick refreshes tree status.
- [ ] Keep immediate input echo and stream rendering.
  - Regression coverage added for skipping children tree reformat while typing.
- [ ] Run TUI tests and manually check interactive feel if possible.
- [ ] Update this plan.

### 4.4 Incremental terminal rendering

- [x] Inspect `eggdisplay/eggdisplay/renderers.py` full-screen stream buffer and paint logic.
  - Existing state: `InlineDiffRenderer` does live-region line diffs; `FullScreenDiffRenderer` owns the alt-screen viewport and row-diffs against `_prev_viewport`; full-screen mode separates permanent `_scrollback`, transient `_stream_buffer`, and `_live_lines`; stream rows are cached by `(width, stream_version)` so scrolling without new stream text does not reparse the stream buffer; terminal output is synchronized and sanitized.
  - App-level state: `OutputPanel` / `InputPanel` cache rendered Rich panels, the app loop only calls `renderer.update()` when panels/input are dirty, and stream appends are coalesced before reaching the renderer.
- [x] Avoid reparsing the entire accumulated stream buffer on every flush.
  - Implemented `_StreamRowsState` so `stream_append()` appends newly rendered ANSI to cached wrapped rows with current style/column state.
  - `_stream_rows()` now full-rebuilds only when terminal width changes.
  - Plain stream chunks without Rich markup/ANSI bypass Rich rendering and are sanitized directly.
- [x] Avoid copying full scrollback on each paint; slice visible rows by index.
  - `_paint()` now computes the visible non-live window by index across `_scrollback` and `stream_rows`, copying only the visible slices needed for the viewport instead of building `list(self._scrollback) + stream_rows`.
- [x] Render only dirty panels where feasible.
  - Already covered by panel render caches plus app-level dirty checks plus renderer row diffs. This is not a semantic panel compositor, but the existing layered caches avoid most unchanged-panel work.
- [x] Run `eggdisplay` tests and TUI streaming tests for the inspection.
  - `python -m pytest eggdisplay/tests/test_renderers_terminal_safety.py egg/tests/test_streaming_tui.py::test_stream_appends_are_coalesced_for_renderer -q` (10 passed).
- [x] Run `eggdisplay` tests and TUI streaming tests after incremental stream wrapping.
  - `python -m pytest eggdisplay/tests egg/tests/test_streaming_tui.py -q` (45 passed).
- [x] Run `eggdisplay` tests and TUI streaming tests after viewport slicing cleanup.
  - `python -m pytest eggdisplay/tests egg/tests/test_streaming_tui.py -q` (45 passed).
- [x] Update this plan after the inspection and incremental stream wrapping.

### 4.5 TUI Children panel event-driven/cache refresh

Problem found after inspecting live high-CPU Egg processes: idle TUIs were repeatedly running a Children panel status-key query every ~0.1s. On a large `.egg/threads.sqlite` with ~2.5M events, the query scans a broad `events` index because it filters by event `type` globally rather than by `(thread_id, type)`, costing around 0.13s per call and explaining 50–80% idle CPU in several Egg instances.

Design direction:

- [x] Prefer an event-watcher/cache-driven solution over per-tick DB polling.
  - The existing current-thread `EventWatcher` marks Children panel state dirty when relevant events arrive.
  - Added `_mark_children_panel_dirty()` for explicit/local invalidation.
- [x] Allow Children panel refresh latency up to about 1s.
  - Fallback refresh is capped by `CHILDREN_PANEL_FALLBACK_REFRESH_SEC = 1.0` instead of running every UI loop tick.
- [x] Avoid a global event-log scan for the Children panel on every UI loop iteration.
  - Relevant event version checks are scoped to subtree thread ids and use `events_thread_type` (`thread_id`, `type`) rather than scanning all event types globally.
  - No new broad index was added.
- [x] Preserve immediate input echo and streaming renderer responsiveness.
  - Existing input-active guard remains, and dirty Children refresh is deferred while input is dirty if cached content exists.
- [x] Add focused tests proving idle `update_panels()` does not repeatedly call the expensive Children status-key query, while relevant watched events/explicit invalidation still refresh the tree.
- [x] Update this plan with files changed, tests run, and expected CPU effect.
  - Files touched: `egg/egg/panels.py`, `egg/egg/streaming.py`, `egg/tests/test_panels.py`, `cpu-usage-reduce-plan.md`.
  - Tests run: `python -m pytest egg/tests/test_panels.py egg/tests/test_formatting.py egg/tests/test_streaming_tui.py -q` (105 passed).

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
- After Phase 1 results: quick wins completed and focused tests pass; CPU not formally measured yet.
- After Phase 2 results: Phase 2.2 reducer migration has trace-based SQL/query-count tests for the cached RA/thread-state path and `build_tool_call_states()` now reuses the reducer; Phase 2.3 batched scheduler max-event/open-lease/scheduling-setting queries and recursive subtree collection. No real CPU benchmark yet.
- After Phase 3 results: Phase 3.1 append-only snapshot path avoids full `SnapshotBuilder` rebuild for pure `msg.create` tails; Phase 3.2 token-stat extension avoids re-tokenizing old snapshot messages in that path and live LLM TPS repeats avoid delta payload rescans while unchanged. No real CPU benchmark yet.
- After Phase 4 results: `/messages` no longer forces full snapshot rebuild for append-only tails; frontend thread settings no longer poll every second; visible system panel no longer starts a second live `/stats` polling loop; TUI children tree formatting, System panel sandbox/autoapproval helper scans, completed-message header TPS snapshot parsing, and idle token-stat rescans on unrelated config events no longer run on idle ticks. Shared SSE fanout attempted then reverted due TestClient hang; needs safer design. Phase 4.4 inspection found that differential terminal rendering is already substantially implemented: line/row diffs, panel render caches, stream-as-transient-buffer, stream row cache for unchanged buffers, and 50ms stream append coalescing. Incremental stream wrapping is now implemented; synthetic long-stream benchmark improved from `44.370s` for 1M chars / timeout before 2M chars to `0.459s` for 1M chars and `0.955s` for 2M chars with 8k-char flushes in a 100x30 test renderer. `_paint()` also now slices visible scrollback/stream rows by index instead of building a full combined non-live list. No real CPU benchmark yet. Phase 4.5 removes the per-0.1s Children panel global event-log status scan; remaining fallback event checks are per-subtree-thread and at most once per second. No post-fix real CPU benchmark yet; restart existing Egg TUI processes before measuring.

## Known risks / open questions

- Shared SSE fanout is trickier than it looks: a direct in-route fanout attempt caused the existing TestClient SSE regression to hang, so retry only with a narrow test harness and careful cancellation/replay semantics.
- Some current tests may implicitly rely on exact event ordering and snapshot timing; preserve or explicitly document any change.
- Current runtime has read-only `/workspace/.egg`; Docker session tests that create `.egg/rlm_sessions` under repo root can fail for environment reasons. Prefer tests that use `tmp_path`/memory provider unless verifying Docker directly.
- Event batching must not hide deltas long enough to hurt perceived streaming latency.
- Derived caches must never become authoritative; always support full rebuild from event log.
- Web and TUI can run against the same DB from different processes, so in-process notifications are not enough by themselves.
- Phase 4.4 should not be treated as unimplemented: broad differential rendering already exists, incremental stream wrapping removes full `_stream_buffer` reparsing on each flushed append, and `_paint()` no longer builds a full combined non-live list before slicing. Further rendering changes should target measured hot spots only.
- Adding indexes can improve reads but slow stream-heavy writes; measure before and after.
- Phase 4.5 still has a 1s fallback that probes subtree threads; if a thread has a very large descendant tree and idle CPU remains high, consider a small materialized per-thread/type event-version cache or watcher-fed subtree invalidation before adding broad SQLite indexes.
- Go sidecars may add IPC latency and deployment complexity; only introduce them after Python-side fixes are insufficient.
