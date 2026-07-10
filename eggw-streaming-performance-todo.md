# EggW Streaming Continuity, Chronology, and Performance TODO

Last updated: 2026-07-10 19:24 UTC
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

Status: [ ] not started

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

Status: [ ] not started

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

Status: [ ] not started

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
