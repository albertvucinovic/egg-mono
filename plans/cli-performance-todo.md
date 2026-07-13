# Egg CLI input and scrolling performance TODO

## Current implementation handoff (authoritative; revalidated 2026-07-13)

The older investigation/status sections below are retained as measurement and
history notes only.  Some checklist text predates commits `4a9e98c`, `c396b8d`,
and `373cf2d` and is stale.  Do not implement an old checklist literally or
revert the current canonical projection/UI architecture.

Current tracked state is `8df2ba5`.  Input wakeup/latest-wins completion,
watcher-driven panel state, metadata-only Children reads, `ChunkedText`, and
incremental wrapped stream rows are already committed and must remain intact.
The real-thread measurements in the follow-up section remain useful, but each
candidate fix must be re-proven against current code and invariants before it is
implemented.

Fresh code review confirms the first root cause remains real:

- `create_snapshot()` still calls `load_thread_projection()` even at an equal
  watermark.
- A versioned snapshot seed is parsed, converted into deep-copied
  `ProjectedMessage` objects, validated by materializing all public messages,
  copied again as `base_snapshot`, copied into a new projection, and finally
  materialized again for publication.
- Existing tests named "incrementally" only monkeypatch
  `project_event_records()`, which the current `load_thread_projection()` path
  does not call; they therefore do not guard the regression.

Implementation order, subject to fresh verification before every slice:

1. Add a current-format coherent-snapshot decoder/validator that does not
   materialize or deep-copy the full projection.  Use it for an equal-watermark
   no-op and for a narrowly safe raw append/ignored-tail extension.  Keep the
   canonical projector as fallback for malformed/legacy snapshots, edits,
   deletes, and continue semantics.  Preserve monotonic publication races.
2. Re-profile snapshot boundaries, then deduplicate watcher snapshot requests
   and move only genuinely cold canonical fallback work off the UI loop if it
   still blocks interaction.
3. Re-prove the min-scrollback repeated-query profile against current code;
   if present, bind immutable snapshot watermark/token metadata to the
   `TranscriptScrollbackSource` rather than querying per hidden message.
4. Re-prove hidden-summary publication and header token-cost profiles before
   changing them.  Preserve min-mode live visibility and explicit `/cost`
   correctness.
5. Bound watcher SQL pages only if current scheduling measurements still show
   an unbounded suffix stall after the higher-impact fixes.

For every slice: add cost-shape tests that instrument the actual current call
path, compare accelerated output with event-only canonical replay, run focused
and broader suites, update this handoff, and commit one coherent change.

## Full-screen display-verbosity transition follow-up (2026-07-13)

Fresh read-only profiling at tracked `d0ceb3a` used the active real thread
`01KXB6NWEXBFTDAAC1HYEP4C3B` at the live 157x116 terminal size.  The final
pre-fix sample was a 46,196,117-byte compatibility snapshot with 6,761
messages and 226,589 events / 89,416,968 bytes of event payload JSON.  Exact
max-to-medium command transitions rebuilt `TranscriptScrollbackSource` on the
UI loop: 160.948 ms median in one 10-run set and 168.328 ms median in a
cross-thread scaling set.  Same-level medium-to-medium still rebuilt and cost
154.107 ms median.  A profiled run attributed 157 ms to source construction,
including 111 ms JSON decode and 30 ms loading the snapshot row; first lazy
medium viewport rendering itself was only 11.609 ms.  Across real 5.7/29.6/
46.2/74.3 MB snapshots, transition medians were 35.595/119.535/168.328/
226.023 ms.  The source's existing `(width, verbosity)` caches could not help
because every command discarded the source.  The old reset sequence also
painted the old source under the new verbosity before installing the new one.

The approved first CLI slice is intentionally narrow:

1. make same-level `/displayVerbosity` a truthful no-op;
2. reuse the installed source only when renderer identity, exact thread,
   snapshot watermark, and app-local semantic transcript generation all match;
3. advance that generation for watcher semantic batches and local message/
   compaction publication, so any local tail, edit, delete, compaction, or
   uncertain build race fails closed to the existing fresh-source path;
4. atomically clear local rows/reset offset/install the source without an
   intermediate paint, then let the normal live update produce one final paint.

Thread and mode switches, manual redraws, inline rendering, snapshot-watermark
changes, and every failed coherence check continue to construct a fresh source.
No append-only source mutation or async fallback is included in this slice.
Required validation is cost-shape (no blob read/decode/build when coherent),
old-source no-paint, output parity at max/medium/min, local-tail and watermark
fallbacks, bounded lazy reachability, full Egg/EggDisplay suites, and the full
EggThreads suite because the shared display command is touched.  After those
pass, remeasure the same real thread and record the committed result below.

### Implemented CLI slice and validation

Implemented the approved fail-closed reuse slice in the current worktree:

- `display_input.py` now returns the truthful `Display verbosity already
  <level>.` result without redraw for a same-level request.
- `TranscriptScrollbackSource` captures thread, snapshot watermark, and the
  app-local semantic transcript generation before load. Full-screen verbosity
  redraw reuses it only when those values and renderer identity still match;
  watcher semantic batches plus local message/compaction publication advance
  the generation. Every mismatch rebuilds through the unchanged safe path.
- `FullScreenDiffRenderer.reset_scrollback_source()` atomically resets offset,
  local history, source-count state, and the diff baseline without painting;
  the caller's normal `update()` performs one final paint. Manual redraw,
  thread/mode changes, and inline behavior remain unchanged.
- Added command/no-op, coherent no-blob-read/no-build, generation/build-race,
  local-tail and watermark fallback, max/medium/min output parity, semantic
  watcher invalidation, atomic old-source no-paint, and bounded lazy
  reachability tests.

Fresh post-fix measurement used this worktree explicitly via `PYTHONPATH` and
the same live thread at 157x116, now a 46,483,901-byte snapshot. Ten coherent
max-to-medium transitions measured **7.029 ms median** (6.121-18.146 ms; the
first uncached medium viewport was 18.146 ms), versus 160.948/168.328 ms
pre-fix medians. Ten same-level medium requests measured **0.230 ms median**
(0.222-0.257 ms). The probe made one metadata read per coherent transition,
kept the identical source, and was instrumented to fail on `get_thread()` blob
read or source rebuild; neither occurred. Snapshot publication attempts were
zero.

Validation with current-worktree imports: focused CLI/renderer/command suite 26
passed; full Egg CLI **551 passed**; full EggDisplay **66 passed**; full
EggThreads **1,003 passed**. `git diff --check` passed for the explicit CLI
paths. Remaining risk is intentionally bounded: a semantic tail or watermark
change pays the existing full safe rebuild; no incremental source extension or
async fallback was attempted. Next action only if measured fallback lag remains:
profile those incoherent transitions and consider a separately guarded async
fresh-source build, not broader renderer changes.

## Archived investigation and status notes

## Goal
Eliminate interactive input latency and scrolling stalls in the terminal Egg CLI, especially during streaming, without weakening transcript visibility, bounded rendering, history reachability, or live-edge behavior.

## Required constraints
- Read `plans/analysis/invariants.md` and applicable `plans/analysis/found-invariants.md` sections before changing behavior.
- Long-thread work must stay incremental, cached, and bounded where possible.
- Streaming tokens/tool data remain visible in real time, including minimum verbosity.
- Initial/transitional rendering stays bounded while ordinary upward scrolling reaches complete history automatically and preserves the viewport anchor (INV-085/INV-088).
- Streaming follows only at the live edge; scrolling upward must not be yanked back (INV-089).
- Reproduce/instrument the real latency path and add focused regression tests at the layer where the issue occurs (INV-098/INV-099).
- Fix root causes rather than hiding lag with lower refresh rates or disabled functionality.
- Commit coherent completed changes and leave tracked state clean.

## Investigation
- [x] Determine why editor key handling lags despite the dedicated input reader thread.
- [x] Determine whether completion runs synchronously on the UI/input/render path and design a principled stale-safe solution if so.
- [x] Determine why scrolling can lag/stall during streaming and identify the recent regression/refactor.
- [x] Add measurements or focused tests that reproduce both classes of latency. (Temporary profiling only in this investigation slice; permanent regression tests belong with implementation.)

## Implementation
- [x] Make input processing remain responsive while expensive completion/render/update work is pending.
- [x] Make scroll processing remain responsive during streaming without violating live-edge semantics.
- [x] Add regression tests for input/completion and streaming scroll responsiveness.
- [x] Run focused and broader relevant suites for the input/completion slice.
- [ ] Update status and commit. (Input/completion `4a9e98c` and panel-state `c396b8d` committed; long-stream slice ready to commit.)

## Investigation findings (2026-07-13, no tracked product/test changes)

### Architecture and primary input root cause

- The "dedicated input thread" only reads terminal bytes and enqueues them:
  - `eggdisplay/eggdisplay/eggdisplay.py:1093-1149`, `RealTimeEditor._input_worker`, calls `input_queue.put(key)`.
  - It does **not** mutate the editor, dispatch scroll, run completion, or render.
- All meaningful key work is synchronous on the asyncio/UI thread:
  - `egg/egg/app.py:918-956` polls/drains the queue, calls `InputMixin.handle_key`, then `update_panels`, `render_group`, and `renderer.update`.
  - `egg/egg/input.py:193-237` invokes `renderer.scroll(...)` synchronously.
  - `egg/egg/input.py:443-444` delegates ordinary typing to the editor synchronously.
- The UI loop unconditionally sleeps 100 ms (`egg/egg/app.py:985-987`) and the reader thread does not wake it. Thus even an idle key waits 0-100 ms before dispatch (about 50 ms average), before any processing/render cost. Queue draining is also unbounded, so bursts can monopolize a tick.
- The optional `AsyncRealTimeEditor` does not solve this in `EggDisplayApp`: `app.run` always starts `editor._input_worker` (`app.py:872-881`), which only exists on `RealTimeEditor`; the main loop also assumes synchronous `queue.Queue.get_nowait`. `EGG_IO_MODE=async` is therefore not a usable alternate path in this app.

### Completion is synchronous and stale-unsafe

- The app adapter directly calls `get_autocomplete_items` (`egg/egg/app.py:278-294`).
- Tab calls it inline from `TextEditor._handle_tab` (`eggdisplay/.../eggdisplay.py:393-448`). Once the popup is active, every insertion, deletion, or cursor movement calls `_refresh_completion` inline (e.g. lines 192-200, 259-314, 450-498). No task, worker, cancellation, generation, or stale-result check exists.
- Expensive completion paths include:
  - Plain conversation words: `egg/egg/completion.py:619-652` loads `ThreadsDB.get_thread` (which executes `SELECT *`, including the entire snapshot blob), JSON-decodes the whole snapshot, then only inspects the last 200 messages.
  - `/continue` and `/duplicateThread`: lines 908-1009 likewise load/decode the full snapshot to produce at most 30 rows.
  - Thread selectors: lines 671-720 correctly start with lightweight `list_threads`, but then call `db.get_thread(tid)` twice per candidate solely for status even though `r.status` is already present. This reloads large snapshot blobs for up to 50 candidates.
  - Filesystem, skill, tool, registry, provider/model catalog and artifact completion also execute inline and may perform filesystem/DB/catalog work.
- Measured with the real 5.4 GB `.egg/threads.sqlite` and the largest current snapshot (74.3 MB):
  - plain unmatched word: median **185.6 ms**;
  - `/continue `: **176.7 ms**; `/duplicateThread ... msg_id=`: **178.3 ms**;
  - `/thread `: **126.3 ms**; `/waitForThreads `: **119.9 ms**; `/deleteThread `: **123.2 ms**;
  - direct `SELECT *` for that thread: median **51.3 ms** by itself.
- Controlled synthetic snapshots confirm size causality for plain completion: 0.1/1/5/10/20 MB snapshots took median **3.1/28.3/140.0/278.9/558.3 ms**. This work blocks subsequent key/scroll dispatch because it runs inside `handle_key`.

### Streaming/scroll stalls share the same UI thread

- The event watcher is an asyncio task, not a worker. `watch_thread` calls `ingest_event_for_live` on the same event loop (`egg/egg/streaming.py:268-371`). `await sleep(0)` improves fairness only between batches/each 100 events; it cannot preempt synchronous ingest, panel, completion, or renderer work.
- Stream flushing is also synchronous on that loop: delayed task -> `_flush_stream_render_buffer_now` -> `renderer.stream_append` (`streaming.py:680-750`). A 50 ms coalescing interval reduces frequency but does not bound an individual flush.
- Scroll events sit in the same polled input queue and call `FullScreenDiffRenderer.scroll` synchronously. Consequently any long watcher/flush/update/completion call delays both wheel dispatch and repaint despite the reader continuing to enqueue bytes.

### Per-tick active-tool scan is a major streaming stall

- Every `update_panels()` begins with `_update_get_user_message_input_mode` (`egg/egg/panels.py:615-617,849-891`). That calls `get_active_get_user_message_waiting_note` every 100 ms.
- For any live **tool** lease, the helper calls `build_tool_call_states` and scans/deep-copies the thread's reduced tool-call map (`eggthreads/eggthreads/api.py:4565-4651`; `tool_state.py:690+`). This is unrelated to whether get-user state changed and is not event-cached at the UI layer.
- Real-state reproduction on thread `...HYEP4C3B` (38.3 MB snapshot, ~146k events, active tool lease): after warm-up, `update_panels()` took about **36-49 ms every nominal 100 ms tick**. With Children forced dirty it took **64-75 ms**. A cProfile run attributed **162/198 ms** (profiling overhead included) to get-user state, chiefly deep-copying 2,891 tool-call states. A cold call measured **845.6 ms**, warm calls about **30 ms**.
- This consumes a large fraction of the UI loop continuously during tool streaming and explains periodic scroll/input stalls. It should be recomputed on relevant events/thread switch, as approval state already is, not every render tick.

### Recent Children-panel regression

History/blame identifies the July 13 CLI refactor:

1. `958598d` replaced per-descendant indexed `MAX(event_seq)` invalidation with one set-based query in `PanelsMixin._compute_children_panel_status_key` (`panels.py:535-600`). The new `COUNT(e.event_seq)` + `MAX` visits all matching events in the subtree. "One query" is not equivalent to bounded work.
   - Controlled DB, 1,000 descendants / 1,000,000 relevant events: current query median **64.3 ms**; the pre-`958598d` equivalent indexed per-thread MAX loop was **7.0-7.2 ms**.
   - Real DB subtree `...Z63EC6TG` (39 threads, 1,196,426 events): cold **69.1 ms**, warm **3.6-6.1 ms**. Cache warmth hides but does not remove the full-range work.
   - Existing `test_children_status_key_uses_one_set_based_query_for_large_subtree` proves only SQL statement count, not visited rows/latency, and therefore blessed the regression mode.
2. `1f75758` added current thread name/description to the Children surface. `format_children_panel` (`egg/egg/formatting.py:225-387`) calls `db.get_thread(root_tid)` at line 248, loading `snapshot_json` although it needs only `name`/`short_recap`.
   - On the real 74.3 MB thread, formatter calls measured **50.7-59.6 ms**; nearly all is the blob fetch. Other 59.9/38.3/29.6 MB snapshots cost about **40/26/20 ms**.
   - Children is marked dirty by watcher events including `stream.open`, `stream.close`, and `msg.create` (`streaming.py:273-301`; relevant types in `panels.py:33-42`), so this cost lands directly in streaming transitions/final-message handling.
3. `4f616eb` refactored `OutputPanel` visual-row measurement/wrapping. Inspection found no comparable unbounded transcript scan: panel heights/content are bounded and cached. Keep it covered, but evidence points to the DB/blob changes above rather than wrapping as the primary stall.

### Long-stream scaling hazards

- `ingest_event_for_live` repeatedly concatenates immutable strings for assistant content, reasoning, reasoning summary, tool output and tool-call args (`egg/egg/streaming.py:425-472`). This is O(total stream length) copying per delta.
- `FullScreenDiffRenderer.stream_append` also does `_stream_buffer += ansi` (`eggdisplay/.../renderers.py:610-638`) while separately maintaining incremental wrapped rows. It defers paint while scrolled up (good INV-089 behavior from `789a31f`) but still copies the entire stream buffer on each append.
- Synthetic per-delta live-state append of 1 KB cost median **0.19/1.39/2.55/5.68/14.77 ms** at existing stream sizes 1/5/10/20/50 MB. Renderer 1 KB appends at 10/25/50 MB cost **4.32/10.09/25.01 ms**; scrolling itself was **1.28/4.14/12.97 ms** because current bookkeeping repeatedly takes/copies the full `stream_rows` list (`_stream_rows` returns `list(state.rows)` and is called in max-offset/compose/clean-key paths).
- Tool preview is bounded to 8,000 chars by `ToolStreamPreviewLimiter`, but long LLM/reasoning streams are not. This scaling issue is secondary at ordinary stream sizes but is a real stall source for long reasoning and violates the incremental long-thread requirement.

### Existing tests and gaps

- Focused baseline is green: **130 passed in 3.41s** for `egg/tests/test_completion.py`, `test_input.py`, `test_streaming_tui.py`, `eggdisplay/tests/test_input_panel_typing.py`, and `test_renderers_terminal_safety.py`.
- Existing tests cover stream coalescing, event-loop yields, deferring flush while input is dirty, deferred paints while scrolled up, incremental stream wrapping, lazy history pagination, anchor preservation, and live-edge behavior.
- Missing literal regressions:
  - no app-loop test that a queued key wakes processing instead of waiting for the 100 ms poll;
  - no test that slow completion does not block a later key/scroll, nor stale-result rejection/coalescing;
  - completion tests assert values only, not that snapshots/thread rows are loaded once/cached/bounded;
  - no test that get-user state is event-driven rather than recomputed every UI tick;
  - Children test checks one SQL statement, not bounded/index-seeking work or avoidance of snapshot blobs;
  - renderer tests check reuse/incremental parsing but not that scroll bookkeeping avoids copying all accumulated stream rows.

### Candidate minimal design (for manager approval before implementation)

1. **Wake and prioritize input:** bridge the reader thread to the running asyncio loop with `loop.call_soon_threadsafe`/an `asyncio.Event`; wait on input or a short UI timer instead of unconditional 100 ms polling. Process a bounded batch per turn, prioritizing control/navigation so floods do not starve watcher/render tasks. Keep all editor mutation on the UI thread.
2. **Stale-safe async completion:** add one latest-request-wins completion worker path. Capture immutable `(generation, text, row, col, thread_id/snapshot watermark)`; perform completion off the UI thread using a worker-owned `ThreadsDB` connection (SQLite connections are thread-affine); apply results on the UI loop only if generation/text/cursor/thread still match and completion is still active. Coalesce superseded refreshes. Preserve immediate accept/navigation semantics. Also remove avoidable work (`r.status`, metadata-only thread reads) and cache/index recent conversation completion data by thread + snapshot watermark.
3. **Event-drive expensive panel state:** cache get-user input mode and recompute only at thread switch and relevant tool/message/control events. Do not call full tool-state projection on every paint. Keep watcher-driven correctness plus a cheap fallback only if cross-process changes require it.
4. **Undo Children unbounded reads without reverting behavior:** select root name/recap explicitly (never `SELECT *`); replace `COUNT(all matching events)` invalidation with monotonic/event-driven versions and an indexed cheap fallback. Preserve adaptive panel semantics and cross-process visibility.
5. **Keep scrolling/rendering incremental:** retain the current lazy transcript source, scrolled-up paint deferral, and offset compensation. Store stream chunks/row state without immutable whole-buffer concatenation; expose row count/tail slices without copying the full row list. At the live edge, paint only bounded viewport rows; while scrolled up, append below the anchor and repaint only on user action/live-region necessity.

### Tests needed with implementation

- App-loop async test with an injected slow panel/stream task: enqueue key/wheel from the reader side and assert prompt wake + bounded dispatch order without timing-flaky wall-clock thresholds (events/barriers/call ordering).
- Editor/completion tests with a blocking fake callback: subsequent typing/scroll dispatch proceeds; request B supersedes A; A cannot reopen/overwrite/insert stale popup; thread/cursor change rejects stale output; worker uses a separate DB connection.
- Completion cost-shape tests: plain completion indexes only recent messages once per snapshot watermark; `/thread` uses lightweight rows and does not call `get_thread`; `/continue` cache invalidates at snapshot change.
- Panel test: repeated active-tool `update_panels` does not call `build_tool_call_states`; relevant event invalidation does, and get-user title still transitions correctly.
- Children tests: metadata formatting must not read `snapshot_json`; invalidation query must not count/scan all historical matching events; cross-process fallback and name/recap changes still refresh.
- Renderer tests using an instrumented sequence: appending/scrolling after a very large logical stream does not iterate/copy all prior rows; preserve existing viewport-anchor and live-edge tests (INV-085/088/089).
- End-to-end TUI integration test combining active streaming, popup completion, wheel-up, continued deltas, and typing, asserting no yank-to-bottom and that control events are serviced between bounded stream work (INV-099).

## Status notes
- 2026-07-13: Task opened from user report. Repository began clean at `4f616eb`. Relevant invariants read: canonical long-thread bounded/incremental performance and real-time streaming visibility; found INV-002/004/008, INV-085/088 bounded-but-reachable scrollback, INV-089 live-edge-only streaming follow, INV-098 real-state causal investigation, and INV-099 layer-appropriate regression coverage.
- 2026-07-13: Investigation slice complete. No tracked product/test edits and no commit. Temporary profiling was run inline only. Root causes/design/tests recorded above.

## Input responsiveness + async completion implementation (2026-07-13)

- Replaced the main loop's unconditional 100 ms sleep/poll with a reader-thread callback that uses `loop.call_soon_threadsafe(asyncio.Event.set)`. The loop now waits on input-or-periodic-tick, closes the clear/queue race, and dispatches at most 32 queued keys per turn while leaving the event set for remaining work (`egg/egg/app.py`, `eggdisplay/.../eggdisplay.py`). Editor mutation remains exclusively on the UI thread.
- Added `AsyncCompletionWorker` with one running request plus one replaceable pending request. It opens/closes its own `ThreadsDB` inside the worker thread, invokes the existing shared completion semantics there, and posts results to the UI loop (`egg/egg/completion.py`).
- Added immutable completion identity: generation, line, row, column, thread ID, and snapshot watermark. The editor rejects stale text/cursor/generation results; the app rejects stale thread/snapshot results. Editing while a request is running immediately issues a newer generation and clears old selectable items. Escape/accept/newline/set-text invalidate outstanding results.
- Preserved Tab behavior: an active popup Tab accepts; multiple async results open navigable selection; Enter accepts; a single Tab result inserts immediately. Completion-result callbacks wake the UI render wait so popups do not wait for the periodic tick.
- Removed `/thread`/`/waitForThreads`/`/deleteThread` completion's duplicate `db.get_thread()` snapshot-blob reads by using the status already present on lightweight `list_threads()` rows.
- Added deterministic tests in `egg/tests/test_input_responsiveness.py` for thread-safe wakeup, bounded queue dispatch, blocked-worker nonblocking typing, latest-request coalescing, stale generation/text/cursor/thread/snapshot rejection, separate worker DB connection, and Tab/navigation/single-result acceptance. Added a focused completion shape test proving thread selectors do not call `get_thread()`.
- Validation:
  - focused input/completion/streaming: 107 passed;
  - complete `egg/tests`: 530 passed in 27.53s;
  - complete `eggdisplay/tests`: 60 passed.
- Remaining performance work is deliberately outside this coherent slice: event-driven get-user/Children state and long-stream/scroll row-copy fixes.

## Streaming panel-state regression implementation (2026-07-13)

- Made get-user input-mode projection watcher-driven. `start_watching_current()` computes initial/thread-switch state once; watcher batches refresh only for message/edit/delete, stream lifecycle, control interrupt, and relevant tool lifecycle/approval events. Ordinary `update_panels()` now applies a cached boolean/style only and never calls `build_tool_call_states`. Stream-delta-only batches do not invalidate it. Existing explicit compatibility callers can still request a refresh.
- Added `ThreadsDB.get_thread_metadata()` and changed `format_children_panel()` to use it, selecting all `ThreadRow` metadata but `NULL AS snapshot_json`. Current name/description rendering no longer fetches multi-megabyte snapshot blobs.
- Replaced Children invalidation's `COUNT(e.event_seq)` full-history aggregate with one CTE whose per-subtree-thread correlated `MAX(event_seq)` is forced through `events_thread_type`. The key retains deterministic per-thread event heads, topology row/count, active stream identity (excluding lease heartbeat timestamps), and current name/recap, preserving cross-process fallback detection without work scaling with all historical events.
- Added deterministic cost-shape/state tests:
  - SQLite VM progress steps remain constant after growing relevant history from 10 to 5,010 events;
  - formatter fails if it touches full `get_thread()` and proves metadata snapshot is `None`;
  - descendant message and active-stream identity alter the cross-process fallback key;
  - repeated `update_panels()` uses cached get-user state;
  - initial load and relevant watcher event refresh state, while `stream.delta` does not;
  - DB metadata accessor excludes the snapshot blob.
- Real-state remeasurement on active thread `...HYEP4C3B`: warm `update_panels()` fell from the investigation's ~37-49 ms to **5.22 ms median**; Children status key to **0.17 ms median**; Children formatting to **5.24 ms median** (formerly ~26 ms on this 38 MB snapshot).
- Validation:
  - focused panel/get-user/streaming and DB tests: 148 passed;
  - complete `egg/tests`: 535 passed in 27.29s;
  - complete `eggdisplay/tests`: 60 passed;
  - complete `eggthreads/tests`: 987 passed in 95.43s.
- Long-stream immutable buffer/row-list copying is intentionally still deferred to the next scroll slice.

## Long-stream and scroll scaling implementation (2026-07-13)

- Added canonical `eggdisplay.ChunkedText`, an append-only/coalesced text representation. Live assistant content, reasoning, reasoning summaries, tool output, tool-call arguments, and the renderer's ANSI replay buffer now append without copying all prior text. Exact full materialization remains available for explicit replay/final presentation; chunk iteration preserves order and styles.
- Full-screen wrapping remains incremental in `_StreamRowsState`, but `_stream_rows()` now returns a zero-copy sequence view over completed rows plus the current row. Scroll/max-offset/clean-key paths use O(1) length and bounded visible slices instead of `list(state.rows)` / `state.rows + [current]` copies. Width changes replay canonical ANSI chunks into a fresh parser state; ordinary append does not replay old chunks.
- Inline mode uses `ChunkedText.tail()` to materialize only the newest bounded text needed by its already-bounded panel. This does not truncate stored/replayable content or affect full-screen visibility/history; all chunks remain available and stream finalization semantics are unchanged.
- Consolidated app initialization and Ctrl+C reset through `_make_live_state()` so all live text fields consistently use the canonical representation.
- Preserved existing behavior/tests for ANSI/grapheme wrapping, stream replay, stream end/final message replacement, lazy transcript pagination, width changes, viewport anchor compensation, scrolled-up paint deferral, and live-edge follow.
- Added deterministic cost-shape tests proving:
  - appending to million-character `ChunkedText` does not iterate old blocks;
  - renderer append does not replay old ANSI chunks;
  - scroll over a million logical stream rows never iterates the sequence and reads only bounded visible rows;
  - bounded tail materialization touches only newest blocks;
  - live-state per-delta append does not materialize old content;
  - inline panel does not full-join accumulated chunked content.
- Added actual-layer integration coverage: SGR wheel-up through the app input queue, continued live delta/flush, and subsequent typing preserve the viewport anchor, keep the renderer scrolled up, and service input.
- Synthetic repro after fix (10/25/50 MB existing streams):
  - renderer 1 KB append: **~0.40/0.39/0.39 ms median** (formerly ~4.32/10.09/25.01 ms);
  - scroll: **~0.01 ms median** at all sizes (formerly ~1.28/4.14/12.97 ms);
  - live-state 1 KB append: **~0.003 ms median** at all sizes (formerly ~2.55/~7/~14.77 ms at 10/25/50 MB).
- Validation:
  - complete `egg/tests`: 538 passed in 28.00s;
  - complete `eggdisplay/tests`: 64 passed;
  - no EggThreads product changes in this slice; its full 987-test suite was green in the preceding committed slice.

## Follow-up real-state investigation: remaining long-thread min-streaming lag (2026-07-13, no tracked code/test changes)

### Scope and state inspected

- Investigated the reported remaining lag on the real active thread `01KXB6NWEXBFTDAAC1HYEP4C3B`, not only synthetic `ChunkedText` fixtures (INV-098/INV-099).
- Repository remained at tracked `8df2ba55e8f8c16a339acdffc4114dc9f350ea0f`; this investigation changed no tracked product/test code and made no commit.
- The DB was ~5.56 GB plus ~294 MB WAL. During measurement the chosen thread was ~5.8k effective messages, ~158k events / ~64 MB event JSON, and its snapshot was ~40 MB. The compatibility snapshot is duplicated internally: `messages` was ~18.6 MB (46.5%) and `_thread_projection.message_states` ~20.0 MB (49.9%), plus ~0.9 MB token stats. A parse was ~83-102 ms.
- Relevant constraints remain: bounded but fully reachable history (INV-085/088), live-edge-only following (INV-089), and real-time assistant/reasoning/tool visibility at min (INV-090 plus the global streaming-visibility invariant).

### End-to-end findings and measured root causes

#### 1. Canonical projection refactor regressed every snapshot publication to O(full snapshot)

- Regression history: `d38153f` (`Add canonical watermark thread projection`, 2026-07-10) replaced the old append-only snapshot fast path from `32adb68`/`7b1312d`.
- `eggthreads/eggthreads/api.py:3299-3358 create_snapshot()` now always calls `load_thread_projection()` and `_snapshot_from_projection()`, JSON-encodes the whole result, and, on an equal watermark, loads the projection a **second** time before returning the already-current persisted snapshot.
- `eggthreads/eggthreads/projection.py:46-68, 123-136, 211-262, 264-377, 403-451` parses the ~40 MB snapshot, reconstructs/deep-copies every projected message/state, validates by materializing all public messages, deep-copies the base snapshot again, then snapshot publication materializes the messages/states again. The token-stats extension itself is incremental, but the enclosing representation is not.
- Real-state cProfile:
  - current/equal-watermark `create_snapshot()`: **4.261 s**, 21.6M calls; two `load_thread_projection()` calls 3.372 s; `deepcopy` 3.545 s.
  - one appended tiny `msg.create`: **2.496 s**; projection load 1.661 s; deep-copy 2.039 s; `_snapshot_from_projection` 0.575 s; JSON encode ~0.115 s.
  - a single valid projection+snapshot+encode pass separately measured **2.352 s** (projection 1.672 s, snapshot materialization 0.563 s, encode 0.117 s).
- This work is synchronous. In the TUI watcher, one normal final `msg.create` + `stream.close` batch invokes it twice: `watch_thread()` at `egg/egg/streaming.py:341-349` and `ingest_event_for_live(stream.close)` at `563-575`. A focused fake-watcher probe confirmed **2 snapshot calls for one final batch**. The in-process `SubtreeScheduler` also runs `ThreadRunner.run_once()` on this same asyncio loop (`egg/egg/app.py:639-671`, `eggthreads/eggthreads/runner.py:3985-4001`), and the runner itself synchronously snapshots after every invocation at `runner.py:1560-1565`. These multi-second calls prevent input and scroll dispatch even though the reader thread and queue wakeup are correct.
- This explains why the symptom is strongest “while streaming”: assistant and tool invocations repeatedly cross message/close boundaries, and the currently running watcher/runner duplicate snapshot work around each boundary. It is not a min-only semantic path, but min workloads with many tool call/result turns trigger it frequently.

#### 2. Min lazy history still does one SQLite watermark query per hidden message

- `TranscriptScrollbackSource._ensure_rows()` (`egg/egg/panels.py:180-236`) correctly walks backward lazily and preserves reachability/anchors, but every hidden message calls `_static_transcript_message_renderables()` -> `_static_transcript_message_token_counts()` (`panels.py:1394-1423`) -> `_snapshot_last_event_seq()` (`formatting.py:65-76`) even after the per-message token map is warm.
- Real bottom load (100 columns, 24 rows): source construction ~104-124 ms from one snapshot load; max ~141 ms, medium ~170 ms, **min ~288 ms**. The min tail walked 29 blocks and materialized 242 cache rows to aggregate a hidden run.
- Fresh min bottom cProfile: 0.335 s; token helper 0.282 s; 30 SQLite executes 0.174 s; 29 watermark checks 0.157 s; one token-map snapshot parse 0.121 s / JSON parse 0.090 s.
- Ordinary upward pagination reproduced a **793 ms** cliff at bottom offset 240: 151 hidden blocks, 151 watermark SELECTs, 0.772 s in SQLite execute. The same operation is ~0 ms while still inside already-rendered cached rows. This is the literal remaining scrolling stall; it violates the intended cost shape even though history remains reachable.
- Cost-shape evidence: each watermark SELECT is only 13 SQLite VM steps (so the query is indexed/constant), but N hidden messages perform `13*N` VM steps and N Python/SQLite crossings. The source already captures one immutable snapshot and should not revalidate it per block.

#### 3. Min finalization redraws the growing hidden summary once per hidden message

- The full-screen min static path intentionally keeps completed assistant tool calls/results/reasoning visible by aggregating one consecutive hidden run. `console_print_message()` (`panels.py:1843-1860`) updates that summary after every hidden message through `_update_full_screen_static_min_summary()` -> `_replace_full_screen_static_min_summary()` -> `FullScreenDiffRenderer.replace_recent_scrollback()` (`eggdisplay/.../renderers.py:572-616`). Each update rebuilds the complete `Tools: ...` string, Rich-renders/sanitizes it, and repaints.
- The real thread had 168 hidden runs / 5,554 hidden messages; median run 14.5, p90 112.1, p99 216.8, max 232. On the real 232-message run, min finalization took **1.507 s**. Profile after token-map priming: 232 watermark SELECTs 1.206 s plus 232 full summary replacements 0.315 s (232 Rich renders). Scaling was linear in message count (10/20/40/80/232: ~57/113/220/452/1507 ms).
- Max/medium also pay per-message final rendering, but min’s unique repeated replacement means it does 232 renders to produce one 19-row aggregate. Coalescing the watcher’s already-collected batch/run to one summary replacement preserves real-time streaming (the transient renderer already showed the deltas) and preserves final min visibility; it does not justify hiding or dropping details.

#### 4. Full token/cost accounting is a separate cold-cache multi-second UI-thread stall

- `update_panels()` always calls `current_token_stats()` (`egg/egg/panels.py:647-710`; `egg/egg/formatting.py:760-851`). Warm state is correctly cached and measured only ~5-7 ms for panel/group work and ~6-9 ms including Rich live-region render/paint, across max/medium/min and idle/LLM/tool states. A 5 MB canonical live-state buffer did not change this (~6-7 ms), so `OutputPanel`, `render_group`, Rich, and the recent chunked stream implementation are not the warm steady-state root cause.
- A cold real `current_token_stats()` is **4.628 s** / 32.4M calls. `thread_token_stats()` took 4.618 s; `total_token_stats()` 4.270 s; `_epoch_usage_token_stats()` 3.640 s; `_full_usage_by_compaction_epoch_stats()` 3.489 s; ~6.8k message tokenizations / 17.5M character predicates consumed ~3.7 s. It also parsed the large snapshot repeatedly through `_load_completed_thread_messages()` and provider-context accounting.
- The cached snapshot token stats are complete/current (5,850 messages == 5,850 per-message entries and current usage shape), but compaction-epoch cost (`token_count.py:956-1057`) re-tokenizes each epoch’s message sets on every cache miss rather than reducing already-cached per-message/API usage metadata. Provider context also re-tokenizes the compacted suffix.
- Active-stream caching now deliberately reuses a same-snapshot value and warm idle state is stable, so this is not paid on every delta. It is paid on first panel update/thread switch, after snapshot-watermark invalidation, and immediately after stream end because `stream.close` clears `active_invoke` and snapshots advance. Since it is synchronous on the UI thread, it causes a visible multi-second key/scroll freeze at exactly those transitions.
- Inline-only chat cache rebuild is another O(history) transition cost: after a snapshot change, `rebuild_chat_cache_for_current()` walks all messages. Real measurements after token-stat priming: max ~147 ms / 15.5M text chars, medium ~128 ms / 1.44M, min ~236-287 ms / ~425k. Full-screen avoids this body rebuild, so it is secondary to the reported full-screen min issue but should reuse loaded snapshot/token metadata rather than parse the blob twice.

#### 5. What is already bounded / not the dominant cause

- Watcher query itself is indexed (`events_thread_seq`), and delta ingestion is cheap enough when batches are finite: replaying the largest real 9,971 tool-call-argument deltas took ~0.264 s with a no-op renderer and ~0.908 s including the real incremental renderer; 2,112 assistant text deltas ~20 ms; 537 tool-output deltas ~25 ms; synthetic 10k reasoning deltas ~0.39 s. The watcher yields every 100 rows, and renderer appends are 50 ms/64 KB coalesced.
- However, `EventWatcher.aiter()` has no batch limit (`SELECT ... event_seq>? ORDER BY ...`), so an attach/catch-up query can fetch an unbounded list before the existing every-100 ingestion yields. On this DB, the largest real catch-up suffix tested was 84,151 rows / ~106-122 ms and ~10,939 progress callbacks at 100 VM steps; `LIMIT 256` was 256 rows / 36 callbacks and warm ~0.2-0.4 ms. This is a smaller but principled remaining ingestion bound.
- Renderer stream append/wrapping, live-edge following, and scrolled-up deferral remain incremental after `373cf2d`. Input queue dispatch is bounded to 32 and event-driven after `4a9e98c`. The observed stalls arise before those mechanisms get event-loop time.

### Minimal root-cause implementation design (manager approval required)

1. **Restore a canonical incremental snapshot fast path without weakening projection semantics.**
   - In `create_snapshot()`, first metadata-read the thread and return a decoded current valid coherent snapshot immediately when its watermark already equals the target. Do not project/encode twice merely to check whether a repair might be needed; validate the cheap version/thread/watermark shape and reserve canonical event replay for malformed/legacy repair.
   - For a valid coherent base plus append-only/ignored tail, apply the tail to canonical projected state and public messages incrementally: reuse unchanged snapshot subtrees, append only new `msg.create` projected states/messages, extend token stats with the existing helper, and publish with the existing monotonic CAS. Full canonical replay remains the fallback for edits/deletes/continue semantics, malformed snapshots, or unsupported tail types. Preserve exact opaque payloads and `_thread_projection` coherence.
   - Deduplicate ownership: a final watcher batch must not snapshot once for `msg.create` and again for `stream.close`; runner/watcher should not both synchronously rebuild the same watermark. Prefer one publication at the semantic message/close boundary. If snapshot work can still be large (fallback repair/edit), schedule it off the UI loop using a dedicated DB connection and monotonic CAS, never sharing a SQLite connection across threads.
2. **Make min lazy rows snapshot-local.** Capture snapshot watermark and per-message token map once when `TranscriptScrollbackSource` loads its snapshot; pass/use that immutable metadata while rendering blocks. Remove `_snapshot_last_event_seq()` from the per-message helper hot path. Cache invalidation stays at source replacement/thread/verbosity/snapshot transitions, not each block. Reuse one decoded snapshot to build blocks and token metadata.
3. **Coalesce min final-summary publication at watcher batch/run boundaries.** Accumulate all completed hidden messages in a watcher batch in the existing `MinHiddenActivitySummary`, then publish/replace once (and flush before a visible item). Do not lower transient stream refresh, hide tool/reasoning output, or change aggregate semantics. If a huge batch is chunked for fairness, publish at bounded human-visible frames, not every hidden message.
4. **Separate fast header stats from explicit full historical `/cost`.** Build the panel header from the snapshot’s cached token stats plus bounded post-snapshot/live-tail data and cached compaction-provider metadata; reduce cost from cached per-message/API usage instead of re-tokenizing every epoch. Keep `thread_token_stats()` exact-enough and full for explicit diagnostics, but never run a multi-second full-history rescan synchronously in the periodic panel path. Compute rare cold/fallback stats in a worker/dedicated connection and apply by `(thread_id, snapshot_seq, active_invoke)` identity so stale results cannot overwrite current state.
5. **Bound watcher fetch batches** with a modest SQL `LIMIT` (e.g. 256) while retaining immediate follow-up polling and the existing every-100 event-loop yield. This changes scheduling shape only, not event order or visibility.

### Required implementation tests / measurements

- `eggthreads` snapshot tests:
  - current coherent equal-watermark `create_snapshot()` must not call canonical projection or encode/publish;
  - appending 1 message after 10 vs 10,000 historical messages has constant tail replay/tokenization work (instrument calls/SQLite progress rather than wall-clock alone);
  - ignored stream/control tails advance watermark without copying/projecting all old messages;
  - edits/deletes/continue and malformed legacy snapshots still take canonical fallback and exactly match no-snapshot replay;
  - monotonic two-writer CAS/newer-writer-wins coverage remains green.
- Watcher integration: one batch containing final `msg.create` + `stream.close` causes at most one snapshot publication; large fallback snapshot work does not block a queued key/scroll; EventWatcher batches never exceed the bound and preserve sequence/order without idle delay between nonempty pages.
- Min scrollback cost shape: rendering/paging 10 vs 1,000 hidden messages performs O(1) snapshot-watermark/token-map DB reads, preserves one aggregate per hidden run, reaches the root through ordinary scrolling, and preserves the viewport anchor/live-edge behavior.
- Min finalization: N hidden messages in one batch cause O(1) `replace_recent_scrollback`/Rich summary renders (or bounded frame-count independent of N), while assistant text, reasoning, tool-call arguments, and tool outputs were visible in the transient stream and final aggregate counts/names/tokens match existing semantics.
- Header stats: panel update on a large compacted snapshot must not call `_token_stats_for_messages` over historical messages or parse the snapshot repeatedly; stale async completion is rejected on thread/snapshot/invoke changes; explicit `/cost` correctness tests for compaction epochs remain unchanged.
- Re-run the real ~40 MB thread profile: target warm input/update <10 ms; min hidden-run pagination without the ~0.8 s cliff; one-message snapshot publication proportional to tail/encoding policy rather than 2.5-4.3 s projection; no multi-second cold header stall on the UI thread; max/medium/min and idle/assistant/reasoning/tool paths all checked.

### Current handoff status

- 2026-07-13: Revalidated the stale handoff against current code and implemented
  the first current-architecture snapshot slice.  Added raw coherent-snapshot
  validation shared by seed loading and publication, equal-watermark return
  without projection/encoding, fail-closed append/no-op tail extension of both
  public messages and `_thread_projection`, incremental token extension, and a
  cheap winner return after a lost publication CAS.  Unknown events, edits,
  deletes, continue interrupts, malformed snapshots, and legacy snapshots stay
  on canonical projection.  Fixed the old performance tests so they fence the
  actual `load_thread_projection()` path rather than unused
  `project_event_records()`, and added history-cost-shape/unknown-event/output-
  equivalence coverage.  Tests: focused 44 passed; full `eggthreads/tests` 989
  passed (with local checkout forced through `PYTHONPATH`, because the active
  venv otherwise resolves a different editable clone).  Synthetic 10k-message
  snapshot: equal-watermark median ~0.053s and one-message tail ~0.121s versus
  ~0.3s cold canonical build; copied 74 MB snapshot equal-watermark median
  ~0.212s.  Next: review/commit this slice, then freshly profile watcher
  duplicate ownership before changing it.
- 2026-07-13: Fresh watcher review confirmed two independent current issues.
  `ingest_event_for_live(stream.close)` still snapshotted, then the enclosing
  semantic batch snapshotted again; and `EventWatcher` still fetched its entire
  suffix before yielding.  Implemented one batch-level snapshot owner, with a
  metadata-watermark guard that reuses a snapshot already published by the
  in-process runner, and bounded watcher pages (default 256) that immediately
  continue while nonempty.  Added final-batch single-owner/current-runner reuse
  integration tests and a paging/order/no-inter-page-sleep watcher test.  Tests:
  focused 26 passed; relevant Egg/EggThreads integration set 163 passed; full
  `eggthreads/tests` 990 passed.  Next: commit, then re-profile the min lazy
  scrollback token lookup against current code before changing it.
- 2026-07-13: Fresh min-scrollback review confirmed the stale report's repeated
  watermark lookup still existed: every lazy block called
  `_static_transcript_message_token_counts()`, which queried the same snapshot
  sequence even though its token map was cached.  `TranscriptScrollbackSource`
  now decodes its snapshot once, captures that exact watermark and per-message
  token map, and passes the immutable map through lazy rendering.  Other static
  render call sites retain their existing cache path.  Added a 100-hidden-block
  cost-shape test that fails any per-message watermark read.  Tests: panel suite
  102 passed; relevant panel/formatting/streaming/renderer suites 203 passed.
  Next: commit, then independently verify whether live min-summary publication
  still replaces Rich output once per hidden message.
- 2026-07-13: Fresh live-summary review confirmed the current full-screen path
  intentionally replaced the growing aggregate once for every hidden completed
  message (`[0, 1, 1, ...]`).  Added a watcher-only deferred mode: completed
  hidden messages in one watcher batch accumulate in the existing
  `MinHiddenActivitySummary`, then publish once after the batch.  Direct calls
  keep immediate behavior, visible messages still flush the prior run before
  printing, and transient assistant/reasoning/tool streams remain unchanged.
  Added a 50-tool-result integration cost-shape test requiring one replacement
  with the exact final count.  Tests: panel+streaming 128 passed; broader
  panel/formatting/streaming/integration/renderer set 235 passed.  Next: commit,
  then freshly profile header token/cost accounting; do not change `/cost`
  semantics without proof.
- 2026-07-13: Fresh header profile confirmed `current_token_stats()` still
  called full `thread_token_stats()`: on the real active thread it took ~1.93s
  cold versus ~0.14s for the new bounded path, with both reporting identical
  current/full context counts (226,209 / 4,183,856 in that sample).  Added
  `header_token_stats()`, which decodes the snapshot once, reuses cached
  per-message/token/API metadata, scans only the post-snapshot live tail, and
  sums current compaction context from cached per-message counts.  It does not
  run historical compaction-epoch accounting or retokenize the provider view;
  explicit `/cost` and context-limit paths still call `thread_token_stats()`
  unchanged.  Added exact-compaction-equivalence, no-retokenization, and
  no-epoch-accounting tests; updated header-cache mocks to the new helper.
  Tests: focused 72 passed; full EggThreads 992 passed; broader token/
  compaction/header/streaming suite 290 passed.  Next: commit and run final
  real-state/end-to-end checks; investigate only measured residual stalls.
- 2026-07-13 final validation: tracked worktree clean after five commits. Full
  suites: EggThreads 992 passed, Egg CLI 542 passed, EggDisplay 64 passed.
  Copied real 74.26 MB / 8,541-message snapshot: equal-watermark snapshot median
  0.214s; one-message safe-tail publication 0.768s; bounded header 0.197s versus
  exact historical stats 4.622s, with identical current/full context values.
  Remaining snapshot time is linear JSON parse/encode of the compatibility blob,
  not canonical replay/deep-copy. The in-process runner now publishes once and
  the watcher reuses its watermark, so this work is no longer duplicated on the
  UI watcher loop. No further speculative architectural rewrite is justified in
  this pass. If real interactive lag remains, capture a new profile after these
  commits before changing representation or moving publication to a worker.
- 2026-07-13 runner follow-up: final review found two redundant O(snapshot)
  operations still adjacent to the one required runner publication.  Recap
  extraction walked the returned full snapshot; it now reads the invoking
  runner's final `msg.create` payload directly and preserves recap behavior.
  Auto-compaction checking also called `create_snapshot()` again immediately
  after the runner's successful publication; removed that equal-watermark
  decode.  Added a runner recap integration test.  Focused runner/compaction
  tests 77 passed; full EggThreads suite 993 passed.  This leaves one required
  safe-tail publication per invocation rather than publication plus repeated
  full JSON decodes on the runner/UI transition path.
- 2026-07-13 completion: combined final suite passed 1,599 tests
  (`eggthreads/tests`, `egg/tests`, `eggdisplay/tests`).  Tracked worktree clean.
  Implemented commits: `fe486cd`, `84f259c`, `d5cc830`, `4a30a0a`, `6158dca`,
  `446f2ff`.  No broad revert or old-code transplant was used; historical code
  informed only the current-architecture fast paths.  Reprofile real use after
  these commits before opening any new performance slice.
- 2026-07-13 async publication completion: the copied 74 MB thread still needed
  ~0.77s for unavoidable safe-tail JSON parse/encode.  Added
  `create_snapshot_async()`, which uses `asyncio.to_thread` plus a worker-owned
  SQLite connection and the unchanged monotonic `create_snapshot()` authority;
  normal runner finalization now awaits it without blocking the scheduler/UI
  loop. In-memory compatibility falls back synchronously. Added a connection/
  worker-thread ownership test. A 10k-message ticker probe observed 45 ticks in
  0.5s with max ~35ms gap during publication. Combined final suite: 1,600 tests
  passed. Next: commit; implementation complete pending real user reprofile.
