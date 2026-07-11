# EggW Streaming Continuity, Chronology, and Performance TODO

Last updated: 2026-07-11 00:10 UTC
Branch: `refactor20260709`
Baseline commit: `5a6a629` (`Complete semantic refactor integration`)

## Purpose

Implement the fixes identified in the 2026-07-10 EggW investigation while treating UI responsiveness as a hard invariant. This file is the durable manager/worker handover: update it before every implementation commit with files changed, tests run, decisions, caveats, and the exact next task.

## User-visible requirements

1. A tool call that has appeared in EggW must not disappear during live-to-durable handoff, attach, reconnect, or runner-invocation transitions.
2. Every completed tool call remains represented at every `/displayVerbosity` level, matching Egg semantics:
   - `max`: full call/result detail;
   - `medium`: stable call identity and compact preview/collapsed detail;
   - `min`: stable hidden-activity execution/result summary.
3. Local command results (for example, `Display verbosity set to max`) remain in chronological position after every authoritative transcript refresh.
4. Streaming must not make `MessageInput` unresponsive. High-rate deltas must not cause per-delta React/Zustand transcript or page renders.
5. Fix root causes. Do not add polling, arbitrary delay-based masking, broad store subscriptions, unbounded retained state, or whole-transcript sorting on a streaming hot path.

## Repository invariants

- Preserve the thread-scoped ownership introduced by `ec631bb`; repair its hot paths rather than reverting the architecture.
- Keep mutable, thread-keyed streaming buffers and requestAnimationFrame DOM flushing for high-rate text/reasoning/tool-output chunks.
- Advance a live-event cursor only for event frames actually consumed. A message-projection watermark cannot acknowledge tool/stream events.
- Reconcile durable messages by stable message ID and live tools by `tool_call_id`.
- Keep transcript pagination, optimistic-send rollback, navigation isolation, attachments, get-user mode, approvals, interruption, and reconnect behavior intact.
- Reuse shared helpers; do not duplicate timestamp merge, transcript upsert, or event cursor logic.
- Bound retained completed-tool state and inactive thread buffers; no memory leak across navigation.
- Do not modify the pre-existing untracked `review-20260709.md`.
- Commit one coherent phase at a time. Tracked files must be clean after each phase commit.

## Confirmed baseline defects and evidence

### A. Per-event state publication defeats direct-DOM streaming

- `eggw/frontend/src/hooks/useSSE.ts:234-243` publishes `setThreadConnection(...)` and `patchThreadStreaming(...invokeId...)` for every accepted SSE event.
- Whole per-thread streaming-object selectors in `ThreadPage`, `ChatPanel`, and `MessageInput` then rerender consumers.
- Tool arguments also concatenate a growing string and clone Zustand maps on every delta in `store.ts`.
- Database measurements found approximately 4.21M tool-argument deltas, with a measured peak near 1,090 deltas/sec; individual runs included 3,241 and 11,672 deltas.

### B. Live-to-durable tool handoff is non-atomic

- `msg.create` only starts an asynchronous transcript refetch.
- `stream.close` synchronously clears the buffer, tool maps, and `isStreaming` before the fetched durable message is installed.
- Observed run: stream close event 6311139; four durable tool messages at 6311144–6311147, roughly one second later.
- Tool finalization/publication can span runner invocations, so one invocation's close is not a valid boundary for deleting every tool.

### C. Initial attach and reconnect can skip active events

- Initial SSE starts after the message snapshot cursor even though that cursor covers non-message events absent from the transcript.
- Reconnect calls `advanceCursor(messageSnapshotCursor)` before queued response frames are consumed.
- Primary regression: `1234557`; reconnect amplifier: `ec631bb`.

### D. Command chronology reconciliation is incomplete

- Initial command insertion uses `finished_at` and timestamp placement.
- `reconcileTranscriptTail` later appends preserved client-owned command cards after fetched items.
- `ec631bb` dropped the timestamp reinsertion from `157672e`; `5a6a629` restored initial insertion only.

### E. Composer/autocomplete/render amplification

- Every keystroke publishes the draft into global Zustand.
- Autocomplete runs for every nonempty input after 100 ms, has no cancellation/latest-wins guard, and can scan filesystem/transcript data for ordinary prose.
- `flattenTranscript` returns a new array on each `ChatPanel` render, defeating top-level static transcript memoization.
- A measured 300-message/2.02 MB transcript created ~3,960 DOM nodes; at 4x CPU, typing p95 reached 15.7 ms for prose and 21.9 ms for slash input.

## Implementation plan

### Phase 1 — Remove high-rate events from React/Zustand hot paths

Status: [x] complete (2026-07-10)

Deliverables:

- Keep event sequence/cursor progress in refs/transport state; do not publish connection state for every event.
- Publish connection status only when the semantic status changes (`connecting`, `connected`, `reconnecting`, `disconnected`).
- Patch `invokeId` only on invocation transitions, not every frame.
- Accumulate tool-call argument chunks in the thread mutable buffer without growing-string concatenation per delta.
- Add a bounded/coalesced preview notification path (at most one per animation frame; slower is acceptable if UX remains useful).
- Replace whole `streamingByThread[threadId]` selectors in hot components with stable primitive/narrow selectors or stable derived state.
- Memoize flattened transcript data by query-data identity so unrelated live state does not rebuild it.
- Add focused tests proving high-rate deltas do not publish per-frame connection/invoke state and preserve streamed argument display.

Acceptance:

- Text/reasoning/tool-output deltas cause zero React state publications beyond their direct buffer/RAF path.
- Tool-argument bursts do not copy the complete accumulated argument string or rerender the page/composer per delta.
- Existing unit suite passes.

### Phase 2 — Atomic live-to-durable tool continuity

Status: [x] complete (2026-07-10)

Deliverables:

- Add one canonical transcript-tail upsert helper keyed by `msg_id`, preserving infinite-query pages and pagination.
- On `msg.create`, synchronously install the canonical event envelope in the React Query transcript before any live representation can be removed.
- Keep a targeted background refetch only when required for normalized metadata/token counts.
- Retain live tool entries by `tool_call_id` across `stream.close`; clear assistant text/reasoning independently.
- Remove a retained tool entry only after its corresponding assistant/tool durable representation is installed, or an explicit terminal lifecycle event proves it requires no durable card.
- Bound retained settled entries and define reconnect/interruption cleanup semantics.

Acceptance tests:

- Tool delta -> assistant `msg.create` -> immediate `stream.close` -> delayed HTTP response: call is never absent.
- Tool finish -> `stream.close` -> later tool-role `msg.create` in another invocation: call remains represented.
- No duplicate call/result after durable upsert plus refetch.
- Continuity holds at `max`, `medium`, and `min`.

### Phase 3 — Correct initial active replay and reconnect cursors

Status: [x] complete (2026-07-10)

Deliverables:

- Expose/reuse a distinct live replay cursor:
  - active invocation: immediately before that invocation's `stream.open`;
  - idle thread: authoritative snapshot cursor.
- Initialize both SSE transport and the frontend reducer from the same replay cursor.
- Remove message-snapshot `advanceCursor(...)` acknowledgement on reconnect.
- Advance Last-Event-ID only as ordered frames are consumed.
- Preserve stale-sequence and stale-invocation fencing without skipping a newly active invocation.

Acceptance tests:

- Initial attach with snapshot cursor later than active stream/tool frames replays the active frames exactly once.
- Reconnect with queued frames cannot fast-forward past them.
- New invocation while disconnected is adopted without duplicate old frames.
- Idle attach remains efficient and does not replay unnecessary history.

### Phase 4 — Restore command chronology across reconciliation

Status: [x] complete (2026-07-11)

Deliverables:

- Use one stable timestamp merge helper for initial command insertion and authoritative tail reconciliation.
- Preserve optimistic operation behavior and deduplication by message/operation ID.
- Keep work bounded to the newest transcript page; do not sort all loaded scrollback.
- Define deterministic tie behavior for equal/missing timestamps.

Acceptance tests:

- Before message -> local command -> after message remains ordered after one and repeated refetches.
- Multiple local commands remain stably ordered.
- Missing/equal timestamp behavior is deterministic.
- Pagination overlap and optimistic-send tests still pass.

- 2026-07-11 00:10 UTC — Phase 4 complete. Files: `eggw/frontend/src/lib/transcript.ts` replaces one-off insertion with shared `mergeMessagesByTimestamp(...)`, used for both initial local command insertion and authoritative newest-tail reconciliation; `eggw/frontend/src/lib/transcript.test.ts` covers one/repeated refetch, multiple/equal/missing/invalid timestamps, stable IDs and operation IDs, command lifecycle cards versus optimistic sends, and paginated newest-tail metadata/optimistic preservation; `eggw/frontend/e2e/flows.spec.ts` makes the command-order scenario trigger and observe an authoritative refetch before asserting chronology. Decisions: authoritative newest-page order wins equal-timestamp ties; local entries retain stable input order for equal timestamps; missing/invalid local timestamps remain at the local tail in deterministic order; timestamped commands are placed before the first later valid authoritative timestamp without sorting authoritative messages. Deduplication always keys command cards by `client_operation_id`, including pending shell/image cards without `command_name`, but namespaces command and optimistic operations and distinguishes pending from response lifecycle slots because one shell/image operation intentionally emits both. Work is bounded to the fetched newest page and its preserved local entries; older pages, `pageParams`, pagination cursors, overlap authority, event-installed messages, and optimistic operations remain unchanged. This restores the local chronology behavior without backend command persistence or canonical-command changes. Verification: focused transcript/message-operation Vitest (19 passed); final full frontend unit suite (43 passed); TypeScript (passed); focused command-order Playwright (2 passed); full frontend Playwright (38 passed before the final dedup-key refinement, whose frontend unit/focused browser suites were rerun); `git diff --check` (passed). No backend files or routes changed, so backend tests were not run. Caveat: command pending/response lifecycle classification follows actual construction: pending shell and image-generation cards omit `command_name`, while completed/error responses always set it through the response field or `commandNameFromText(...)`. `review-20260709.md` remains untouched and untracked. Exact next task: Phase 5 — keep immediate textarea edits local to `MessageInput`, persist thread drafts only on safe/coalesced boundaries, then gate and latest-wins autocomplete requests without weakening navigation, rollback, or send behavior.

### Phase 5 — Isolate composer and autocomplete hot state

Status: [ ] not started

Deliverables:

- Keep immediate textarea edits local to `MessageInput`; coalesce persistence into thread draft state on safe boundaries (thread switch, blur, send, and/or idle flush).
- Preserve draft isolation and rollback across navigation and failed sends.
- Restrict autocomplete requests to completion-eligible contexts.
- Add request cancellation or a monotonically increasing latest-wins guard; stale responses must never update UI.
- Ensure at most one active request and bounded request frequency during continuous typing.
- Remove or slow unrelated idle polling where SSE invalidation already supplies correctness, if covered by tests.
- Add bounded eviction for inactive thread streaming buffers/state if it can be done without weakening resume behavior.

Acceptance tests:

- Rapid typing updates the textarea without a global store publication per character.
- Navigation, blur, send success, and send failure preserve/restore the correct thread draft.
- Delayed autocomplete responses returned in reverse order cannot overwrite current suggestions.
- Normal prose does not generate filesystem/transcript autocomplete work unless explicitly eligible.

### Phase 6 — Large transcript rendering and integration/performance gates

Status: [ ] not started

Deliverables:

- Profile after Phases 1–5 before choosing virtualization/windowing; do not add complexity without measured need.
- If required, introduce a bounded transcript render window that preserves pagination, scroll anchoring, stick-to-bottom, search/copy usability, and min-summary correctness.
- Add a deterministic mocked SSE burst test based on approximately 1,100 deltas/sec.
- Add browser tests for continuity, attach/reconnect, command order, stale autocomplete, navigation drafts, and composer responsiveness.
- Run full frontend unit/Playwright suites and focused backend event/projection tests.
- Update EggW documentation only for user-visible semantics or architecture that changed.

Performance gates:

- Replay 1,100 deltas/sec with zero React commits for text/reasoning/tool-output chunks.
- Tool-argument preview: no more than 10 React updates/sec.
- Composer interaction p95 <=16 ms at 4x CPU, target <=8 ms; no event >50 ms.
- Typing 200 characters with 300 loaded messages causes zero transcript/page commits.
- At most one autocomplete request active; stale result never mutates current UI.
- No initial render long task >50 ms desktop or >100 ms at 4x CPU after any transcript-windowing work.

## Required verification commands

Use the repository's exact available commands; record real results rather than assuming success.

```bash
cd eggw/frontend && npm run test:unit
cd eggw/frontend && npm test
# Focused backend tests selected for changed event/message routes:
pytest -q eggw/tests eggthreads/tests
```

The full backend suites may be split into focused then full runs if runtime requires it; document both. Always run `git diff --check` and inspect `git status --short` before committing.

## Phase status log

- 2026-07-10 18:40 UTC — Manager created this handover from the completed investigation. Baseline `5a6a629`; tracked tree clean; pre-existing untracked `review-20260709.md` must remain untouched. Next: Phase 1 only.
- 2026-07-10 19:24 UTC — Phase 1 complete. Files: `eggw/frontend/src/hooks/useSSE.ts` keeps cursor progress in `syncStateRef`, publishes connection status only on status boundaries, and patches `invokeId` only when the reducer changes invocation; `eggw/frontend/src/lib/store.ts` narrows connection state to semantic status, makes unchanged connection/streaming patches no-ops, and stores only live tool-call identity/name; `eggw/frontend/src/lib/streamingBuffer.ts` retains tool arguments as thread-owned chunks and provides a cancellable RAF coalescer; new `eggw/frontend/src/lib/streamingDelta.ts` centralizes the high-rate buffer-only reducer and emits only first-header/name/suppression metadata transitions; `eggw/frontend/src/components/ChatPanel.tsx` uses narrow primitive/map selectors, memoizes `flattenTranscript` by query-data identity, and imperatively appends tool arguments / updates a bounded 320-character preview at most once per RAF; `eggw/frontend/src/components/MessageInput.tsx` and `eggw/frontend/src/app/[threadId]/page.tsx` replace whole streaming-object subscriptions with narrow selectors; `eggw/frontend/src/lib/streamingBuffer.test.ts`, `eggw/frontend/src/lib/streamingDelta.test.ts`, and `eggw/frontend/src/lib/messageOperations.test.ts` cover 1,100-delta buffer bursts, one-preview-flush-per-frame coalescing, streamed argument preservation, semantic-only tool/output notification, and no-op connection/invocation publication. Decisions: cursor sequence is transport/ref state rather than Zustand state; tool-call bodies never enter Zustand; full expanded args append only new chunks, while medium preview scans at most 320 characters; authoritative execution-start args replace the chunk array at a lifecycle boundary; tool-output header and suppression each publish once. Verification: focused Vitest `npx vitest run src/lib/streamingBuffer.test.ts src/lib/streamingDelta.test.ts src/lib/messageOperations.test.ts` (9 passed); full frontend unit `npm run test:unit` (21 passed); TypeScript `npx tsc --noEmit` (passed); focused Playwright `npx playwright test -g 'Live Tool Streaming'` (1 passed); full frontend Playwright `npm test` (37 passed); `git diff --check` (passed). Caveats: the first full Playwright attempt exposed an unbound browser RAF (`TypeError: Illegal invocation`), fixed by window-bound wrappers; the second attempt exposed loss of existing bash `$ script` formatting for authoritative args, fixed and verified; the final full run passed. No backend files/routes changed, so backend suites were not run. `review-20260709.md` remains untouched and untracked. Exact next task: Phase 2 — add the canonical `msg_id` transcript-tail upsert seam, synchronously install `msg.create` envelopes, then retain and reconcile live tools by `tool_call_id` across `stream.close` with bounded terminal cleanup.
- 2026-07-10 19:52 UTC — Phase 2 complete. Files: `eggw/frontend/src/lib/transcript.ts` adds the single `msg_id`-keyed newest-tail upsert and retains consumed event messages only while an HTTP snapshot cursor still lags their `event_seq`, preserving optimistic/client entries, overlap authority, pages, page params, and pagination cursors; new `eggw/frontend/src/lib/messageEvents.ts` maps the actual canonical event-feed shape (`msg_id`, payload, `ts`, `event_seq`) without inventing projection-derived fields; new `eggw/frontend/src/lib/liveToolContinuity.ts` reconciles assistant `tool_calls` and tool-role `tool_call_id`, retains tools across invocation close, removes call-only live state at assistant durability, removes output at matching tool-result durability, supports explicit terminal cleanup, and bounds retention to 100 tools per thread and 20 LRU thread registries; `eggw/frontend/src/hooks/useSSE.ts` synchronously upserts `msg.create` before later frames, retains tool state across `stream.close`, clears assistant text separately, performs immediate targeted metadata refetches without timers, and applies authoritative reconnect cleanup; `eggw/frontend/src/lib/store.ts` and `streamingBuffer.ts` add separate assistant/tool-call/tool-output removal seams plus explicit interruption cleanup; `ChatPanel.tsx` renders retained live tools after streaming stops; `MessageInput.tsx` and `[threadId]/page.tsx` explicitly clear retained state after successful user interruption; focused unit tests cover synchronous upsert before delayed HTTP completion, pagination/optimistic preservation, stale-refetch continuity, deduplication after covered refetch, matching assistant/result boundaries including later invocation publication, explicit cleanup, and bounds; `eggw/frontend/e2e/flows.spec.ts` covers immediate `msg.create`/`stream.close`, stale HTTP refetch, and max/medium/min representation. Decisions: canonical SSE payload is installed exactly, while HTTP remains targeted normalization for `content_text`, token/TPS, and optimizer metadata; assistant durability hides a duplicate live call only when no live output remains; a matching durable tool result removes retained output; `stream.close` is never a tool-deletion boundary; authoritative idle/replaced-invocation reconciliation and successful explicit interruption clear retained state; no polling, delay masking, replay-cursor change, or command-order change was added. Verification: focused Vitest `npx vitest run src/lib/transcript.test.ts src/lib/messageEvents.test.ts src/lib/liveToolContinuity.test.ts src/lib/messageOperations.test.ts` (20 passed at focused run); final full frontend unit `npm run test:unit` (34 passed); TypeScript `npx tsc --noEmit` (passed); focused Playwright `npx playwright test -g 'Atomic Live Tool Continuity|Live Tool Streaming'` (2 passed); full frontend Playwright `npm test` (38 passed); focused backend canonical-envelope/projection-route tests (2 passed, one Starlette deprecation warning); `git diff --check` (passed). Caveats: no backend code changed; the retained-tool hard bound evicts oldest entries only as a fail-safe after 100 per thread, while normal matching durability/interruption/reconnect cleanup removes them earlier. `review-20260709.md` remains untouched and untracked. Exact next task: Phase 3 — expose/reuse a distinct active live replay cursor, initialize transport and reducer from it, remove reconnect message-watermark acknowledgement, and prove queued active frames cannot be skipped.
- 2026-07-10 19:59 UTC — Phase 2 follow-up fixes manager-reviewed global retention ownership. `eggw/frontend/src/lib/liveToolContinuity.ts` now returns every evicted thread ID plus all retained `tool_call_id`s from the bounded LRU owner instead of silently discarding registry bookkeeping; its pure `cleanUpEvictedLiveTools` callback seam keeps external ownership explicit. `useSSE.ts` consumes every eviction synchronously and removes matching tool cards from Zustand plus matching argument/output chunks from that thread's mutable buffer. `liveToolContinuity.test.ts` seeds registry, Zustand, and buffer state across more than the configured owner limit, refreshes the current thread's LRU age, then proves the evicted thread's external tool state is removed while current-thread state remains. Decision/caveat: Phase 2 now fully bounds the retained tool subset it owns (registry plus corresponding store/buffer entries); the general thread-keyed `StreamingBuffer` container can still retain non-tool/empty inactive-thread buffers, whose broader eviction remains explicitly Phase 5 scope. Verification: focused continuity unit test file (9 passed); final full frontend unit suite (35 passed); TypeScript (passed); focused atomic/live-tool browser tests (2 passed); `git diff --check` (passed). No backend or user-visible rendering path changed in this follow-up. Exact next task remains Phase 3 — expose/reuse a distinct active live replay cursor, initialize transport and reducer from it, remove reconnect message-watermark acknowledgement, and prove queued active frames cannot be skipped.
- 2026-07-10 20:22 UTC — Phase 3 complete. Files: `eggthreads/eggthreads/event_feed.py` adds the canonical `ThreadReplayCursor`/`replay_cursor(...)` contract backed by the existing exact live-lease `stream.open` lookup (active returns `open_seq - 1`, idle returns the supplied authoritative snapshot cursor), exports it through `eggthreads/__init__.py`, and `eggw/eggw/routes/events.py` reuses it for connection defaults; `eggw/eggw/routes/threads.py` extends `/state?snapshot_cursor=...` with `live_replay_cursor`, `streaming_invoke_id`, and kind from one fresh event-feed connection; `eggw/frontend/src/lib/api.ts` types that contract; `useSSE.ts` fetches the durable message snapshot first, resolves the live replay contract from that cursor, then initializes both `AuthenticatedEventSource` and `ThreadEventSyncState` from the same replay cursor. Reconnect no longer advances transport/reducer from a message projection or pre-adopts `/state`; durable transcript refetch remains independent, Last-Event-ID advances only in `AuthenticatedEventSource.consumeSSE`, and a replacement invocation is adopted only by its ordered `stream.open`. Invocation replacement clears assistant state but preserves Phase 2 retained tools. Removed obsolete public `advanceCursor` and projection-watermark reducer reconciliation. Tests: `eggthreads/tests/test_event_feed.py` covers active-vs-idle replay resolution and exact active frames despite a later snapshot; `eggw/tests/test_api.py` covers the `/state` contract; frontend `eventSync.test.ts` covers exact-once replay below a later snapshot plus ordered replacement/stale-invocation fencing; `sse.test.ts` proves reconnect uses the last consumed frame and cannot skip queued frames; browser reconnect coverage adopts a new invocation without duplicate text, and live-tool attach asserts snapshot cursor 10 still opens transport at replay cursor 0. Decisions: message snapshot remains only durable normalization; replay cursor is a distinct event-feed contract; no reconnect watermark acknowledgement, broad replay, polling, command chronology, or composer changes. Verification: focused backend `pytest -q eggthreads/tests/test_event_feed.py eggw/tests/test_api.py -k 'EventStreaming or get_thread_state'` (9 passed, 105 deselected, one Starlette deprecation warning); final full frontend unit `npm run test:unit` (37 passed); TypeScript `npx tsc --noEmit` (passed); focused browser reconnect/live-tool/Phase 2 continuity (3 passed); full frontend Playwright `npm test` (38 passed); `git diff --check` (passed). Caveat: `/state` without `snapshot_cursor` remains backward compatible and uses current event max as its idle cursor; initial EggW synchronization always supplies the coherent message snapshot cursor. `review-20260709.md` remains untouched and untracked. Exact next task: Phase 4 — implement one stable timestamp merge helper for initial command insertion and authoritative newest-tail reconciliation, preserving optimistic IDs/pagination and defining deterministic equal/missing timestamp ties.
