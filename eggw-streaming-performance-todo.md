# EggW Streaming Continuity, Chronology, and Performance TODO

Last updated: 2026-07-11 01:42 UTC
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

Status: [x] complete (2026-07-11)

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

- 2026-07-11 00:38 UTC — Phase 5 complete. Files: `eggw/frontend/src/components/MessageInput.tsx` now owns immediate textarea state locally and uses `lib/composerDraft.ts` as the single thread-keyed synchronization seam for hydration plus blur/send/thread-switch/unmount/500 ms idle flushes; external shortcut/edit-answer/paste/rollback writes hydrate local state and race-safe flushes never overwrite a newer external draft. Optimistic message and command callbacks keep source-thread ownership, successful operations clear their source, and failures merge submitted text ahead of any newer local draft while restoring source attachments. `lib/autocomplete.ts` gates requests to slash, shell, and explicit `./`/`../`/`~/` path contexts; `api.ts` threads `AbortSignal` through `apiFetch`; one coordinator aborts the prior request and applies a sequence fence, with the existing 100 ms debounce bounding request rate. `app/[threadId]/page.tsx` removes the one-second settings poll; local settings mutations, command completion, and SSE `user_command.finished` now invalidate the shared settings query. `lib/streamingBuffer.ts`, `lib/threadEphemeral.ts`, `lib/store.ts`, and `lib/liveToolContinuity.ts` add explicit inactive-thread eviction: connect sweeps all known buffer/store owners and disconnect removes only non-current, non-streaming, disconnected state with no retained Phase 2 tools; active/current/connecting/connected/reconnecting/tool-retained owners are fenced. Tests: new composer/autocomplete/ephemeral unit suites prove 200 edits cause zero global publications until flush, thread switch hydration, external-write races, latest-wins cancellation, ordinary-prose gating, and real container/store eviction; message-operation and live-tool tests cover async merged rollback and retained-tool ownership; browser coverage exercises 200-character navigation/back, reverse-order autocomplete, async send failure with a newer draft, command-error restoration, and settings invalidation without polling. Decisions/caveats: an explicit 500 ms coalesced draft flush bounds crash-loss exposure while safe boundaries flush immediately; ordinary prose no longer receives conversation-word autocomplete, and filesystem completion is intentionally explicit-path-only. Inactive eviction runs on transport ownership transitions rather than timers and preserves all resume-critical states. Verification: final full frontend unit suite (54 passed); TypeScript (passed); focused composer/edit-answer/navigation/live-tool browser suite (7 passed); final full frontend Playwright (41 passed); `git diff --check` (passed). No backend API behavior changed, so backend tests were not run. `review-20260709.md` remains untouched and untracked. Exact next task: Phase 6 — profile the completed Phases 1–5 first, then add deterministic integration/performance gates and introduce transcript windowing only if measurements prove it necessary.

### Phase 6 — Large transcript rendering and integration/performance gates

Status: [x] complete (2026-07-11)

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

- 2026-07-11 01:26 UTC — Phase 6 and the implementation plan complete. Measurement first: the reusable production-build `npm run profile:transcript` harness serves deterministic 0/100/300 mixed Markdown/code/math/tool-output fixtures, reports payload bytes, mounted DOM nodes, verified composer input length, textarea-bound per-key round-trip p95, browser Event Timing, and long tasks, and supports CDP throttling with `EGGW_CPU_RATE=4`. The exact Phase-5 baseline was rebuilt in a detached worktree at `38d4903` with the corrected harness. Corrected baseline results (normal / 4x CPU): 0 messages — payload 2 bytes, 97/96 DOM nodes, ready 207.3/722.2 ms, composer p95 3.4/11.6 ms, Event Timing p95 24/16 ms, max Event Timing 24/24 ms, max long task 109/440 ms; 100 messages — payload 75,294 bytes, 13,525/13,526 nodes, ready 675.7/2,828.4 ms, composer p95 8.8/49.0 ms, Event Timing p95 16/32 ms, max Event Timing 24/48 ms, max long task 363/1,574 ms; 300 messages — payload 2,320,014 bytes, 40,627/40,626 nodes, ready 2,130.1/9,311.3 ms, composer p95 25.2/138.2 ms, Event Timing p95 24/80 ms, max Event Timing 32/104 ms, max long task 1,307/5,834 ms. These figures supersede the invalid pre-follow-up latency figures, whose key presses were not explicitly textarea-bound. The corrected baseline fails the interaction and long-task gates, so windowing was required. Implementation: `lib/transcriptWindow.ts` keeps all React Query pages authoritative but mounts the newest 5 messages (tuned down from 10 after corrected 4x measurement), preserves the mounted start across tail append, and expands loaded history in 60-message chunks; `ChatPanel.tsx` adds explicit expansion and expands newly fetched older pages while retaining backend pagination, scroll anchoring, stick-to-bottom, command chronology, and live cards. Minimum verbosity expands back to the latest visible conversation boundary and aggregates hidden reasoning/tool details from the remaining unmounted prefix, so hidden execution summaries are not lost. Mounted content retains normal browser copy/search; unmounted loaded content requires expansion. Corrected post-window results (normal / 4x CPU): 0 messages — payload 2 bytes, 96/95 nodes, ready 210.0/945.9 ms, composer p95 3.4/11.0 ms, Event Timing p95 24/24 ms, max Event Timing 32/32 ms, max long task 110/471 ms; 100 messages — payload 75,294 bytes, 766/764 nodes, ready 264.6/1,000.8 ms, composer p95 3.9/15.8 ms, Event Timing p95 32/24 ms, max Event Timing 32/24 ms, max long task 108/433 ms; 300 messages — payload 2,320,014 bytes, 768/771 nodes, ready 317.8/1,061.8 ms, composer p95 4.3/15.4 ms, Event Timing p95 24/16 ms, max Event Timing 32/24 ms, max long task 124/437 ms. Every run asserted exactly 200 `x` characters in the focused textarea. Thus the corrected 300-message 4x p95 improved from 138.2 to 15.4 ms and no measured input event exceeded 32 ms. At normal CPU the 300-message max long task is 14 ms above the empty-thread 110 ms floor; at 4x it is 34 ms below the empty 471 ms floor. The remaining long-task maxima exceed the absolute 50/100 ms target even for an empty thread and are therefore dominated by framework/hydration work rather than transcript size; the harness reports all sizes/rates and this floor comparison instead of asserting an invalid CI budget. Production builds intentionally report `developmentCounters: null`; React Profiler/flush counters are collected only in development browser gates and labeled accordingly. Those deterministic development gates prove typing 200 characters with 300 loaded messages causes zero `ChatPanel`/static-transcript commits and a 1,100-delta burst causes zero static-transcript commits, no body-driven panel commit (only an independently scheduled elapsed-time tick is allowed), and one coalesced body flush per channel in the measured burst. Tool argument full bodies remain RAF/direct-DOM; a separate 100 ms `IntervalCoalescer` limits compact previews to <=10/sec and is unit-tested with 1,100 notifications over one second. `npm run test:regression` reuses existing continuity/reconnect/command/draft/autocomplete scenarios plus both performance gates rather than duplicating fixtures. `eggw/README.md` documents active replay cursor, synchronous durable/live handoff, buffer ownership, bounded transcript rendering, and profiling commands. Verification before the evidence follow-up: frontend unit 58 passed; TypeScript and production build passed; integrated regression Playwright 9 passed; full Playwright 43 passed; focused backend event/state/message 19 passed (95 deselected, one warning); full `eggthreads/tests eggw/tests` with ambient EggW security variables cleared 1,130 passed, 1 skipped, two warnings. An initial full backend run inherited operator-shell EggW origin/token variables and failed two security-origin tests (1,128 passed, 1 skipped); the clean-environment rerun passed completely. Follow-up verification is recorded in the next log entry. `review-20260709.md` remains untouched and untracked. Remaining open item: empty-thread framework/hydration long-task floor exceeds the absolute target and is not a transcript regression; no further implementation phase is planned.

- 2026-07-11 01:42 UTC — Phase 6 evidence follow-up fixes manager-reviewed composer measurement validity. `scripts/profile-transcript.mjs` now focuses and clears `[data-testid=message-input]`, verifies focus, awaits each textarea `pressSequentially('x')`, and fails unless the final value is exactly 200 characters. Production output no longer implies Profiler attribution: it emits `developmentCounters: null` and directs long-task interpretation to the measured empty-thread floor. Rebuilding exact Phase 5 and current Phase 6 at normal/4x produced the corrected measurements above; those values supersede the earlier invalid latency evidence. Corrected current 4x p95 at a 10-message initial window was 17.4 ms for 100 and 16.6 ms for 300, just over the <=16 ms gate, so the initial window was tuned to 5; final corrected 4x p95 is 15.8/15.4 ms while max long tasks remain at/below the empty-thread floor. Files changed: harness, transcript-window constant/tests, performance browser expectations, and durable TODO evidence only. Follow-up verification: focused performance Playwright 2 passed; full frontend unit 58 passed; TypeScript passed; production build passed; `git diff --check` passed. No full Playwright rerun was required because behavior changed only from the already-tested initial-window constant 10 to 5 plus harness/TODO evidence; the focused performance test covers mounting, expansion, min summaries, typing commits, and burst bounds. No Phase 7 or unrelated behavior was started.

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

## Post-plan scroll intent follow-up

Status: [x] complete (2026-07-13)

- 2026-07-13 — Replaced coordinate-inferred transcript scrolling with two explicit state machines. `eggw/frontend/src/lib/chatScrollState.ts` owns `following`/`detached` live-edge intent and serialized `idle`/`revealing`/`fetching` top demand with one coalesced pending intent; its focused unit tests prove that content/programmatic scrolling cannot detach, user intent wins, and loaded history is always selected before a network page. `ChatPanel.tsx` removes the single programmatic-target heuristic, drives detachment/reattachment only from wheel/touch/key/scrollbar intent, and uses a cancelable RAF convergence pump restarted by `ResizeObserver`, so simultaneous durable tool-result insertion/live-card reconciliation cannot strand the viewport. Top demand now runs even when upward input is clamped and emits no `scroll` event, serializes reveal/fetch work, and restores the first visible stable message ID plus pixel offset (with height-delta fallback) after React commits. Existing buttons use the same coordinator; thread changes reset both machines. `flows.spec.ts` deterministically covers three loaded-before-network upward demands (including clamped top), stable reveal anchoring, rapid canonical results followed by later growth, and synchronous user-up detachment. Pagination pages/cursors and transcript ownership remain unchanged. Verification: focused scroll-state Vitest (6 passed); full frontend unit suite (74 passed); TypeScript (passed); focused existing/new scroll and min-history Playwright (4 passed), with focused reruns during anchor refinement; full frontend Playwright suite (77 passed); `git diff --check` (passed). No backend or non-EggW files changed by this follow-up. Concurrent non-EggW worktree edits were left untouched and excluded from staging. Exact next task: none for this follow-up; retain the new scroll regressions in the EggW suite.
