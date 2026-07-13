# Semantic refactor implementation TODO — 2026-07-09

Implement items 1, 2, 4, and 5 from `review-20260709.md` final prioritization.

## Non-negotiable constraints

- Keep `review-20260709.md` untracked and uncommitted.
- Preserve the stable EggThreads SQLite schema unless a migration is explicitly justified and reviewed.
- Make root-cause fixes; do not hide races with sleeps/polling.
- Keep every commit coherent and run focused tests before committing.
- Preserve existing inspectability, event history, raw-output recovery, and cross-client semantics.
- Update this file with commit/test/next-step notes before every implementation commit.

## Phase 1 — Secure EggW defaults and API authorization

Status: complete

Acceptance criteria:

- EggW binds to loopback by default; non-loopback/public binding is explicit.
- A high-entropy API token is required for every non-health HTTP mutation/read and for SSE/WebSocket connections.
- EggW launcher creates/passes a token when one is not supplied.
- The browser client authenticates REST, SSE, and WebSocket requests without logging the token.
- CORS defaults to the actual local frontend origin(s), not `*`; explicit configured origins remain possible.
- Auth behavior is centralized, not repeated per route.
- Tests cover unauthenticated denial, authenticated success, health access, SSE, and WebSocket auth.

## Phase 2 — Fail-closed tool policy

Status: complete

Acceptance criteria:

- Raw tool output is masked by default, matching documentation.
- Missing explicit policy remains usable with safe defaults.
- DB/payload read failures are distinguishable from “no policy” and fail closed for tool exposure/execution/raw publication.
- Descendant tool capability semantics match `invariants.md`: ordinary descendants cannot widen beyond effective ancestors.
- Child policy initialization is not best-effort advisory state.
- Corrupt-policy, read-failure, and ancestor-restriction tests exist.

## Phase 3 — Lease-fenced invocation writes

Status: complete

Acceptance criteria:

- Invocation-owned event writes atomically verify `(thread_id, invoke_id)` and unexpired lease in the same transaction as append.
- Stale LLM/tool owners cannot append terminal messages, lifecycle events, approvals, publication, deltas, or stream close after interrupt/takeover/expiry.
- Lease loss is represented by a typed result/error and does not depend on cooperative cancellation.
- Deterministic two-connection/barrier tests cover late LLM and late tool completion.

## Phase 4 — Transactional tool-output finalization

Status: complete

Acceptance criteria:

- One backend operation owns TC4 output-decision finalization.
- It validates expected state/version and applies explicit precedence; user cancellation/omit cannot be overwritten by automatic policy.
- Terminal Egg and runner use the same operation rather than appending competing events.
- Publication/artifact metadata remains inspectable and provider sanitization remains intact.
- Race and append-failure tests cover manual/automatic/cancellation paths.

## Phase 5 — Canonical watermark-based thread projection core

Status: complete

Acceptance criteria:

- A canonical projection at a fixed event watermark applies create/edit/delete/continue and preserves provider-specific fields.
- Snapshot is an optional acceleration only, never a semantic prerequisite.
- Snapshot publication is monotonic/CAS-safe under concurrent builders.
- Projection contracts are typed enough to keep raw `payload_json`/SQLite rows internal.
- Tests cover no snapshot, stale snapshot, edits/deletes, and concurrent publication.

## Phase 6 — Migrate high-risk projection consumers

Status: complete

Acceptance criteria:

- RA1 provider context uses the canonical projection and cannot omit system/history/queued turns due to missing snapshot.
- Thread duplication/forking uses effective projected state and does not resurrect edited/deleted messages.
- Existing compaction/provider-field behavior remains intact.
- Differential/behavioral tests cover provider context and duplication.

## Phase 7 — Cursor-resumable EggW synchronization

Status: complete

Acceptance criteria:

- Message snapshot response exposes its event cursor/watermark without breaking pagination compatibility.
- SSE accepts explicit `after_seq` and browser `Last-Event-ID`, emits SSE `id`, and uses canonical event envelopes.
- Snapshot-to-live connection has no omission gap.
- Reconnect is duplicate-safe; active work comes from unexpired leases, not unmatched historical opens.
- Nonexistent threads return `404`.
- Tests cover snapshot/connect race, reconnect, duplicates, expired lease, and missing thread.

## Phase 8 — Per-thread frontend state and reconciliation

Status: complete

Acceptance criteria:

- Persisted transcript pages are owned per thread (prefer React Query/infinite query), not one global active-thread array.
- Every async mutation is bound to originating `threadId` and operation/optimistic ID.
- Optimistic send success replaces the temporary ID; failure removes it and restores draft/attachments.
- SSE connection state is separate from thread run state.
- Reconnect performs authoritative reconciliation and rejects stale/duplicate event sequences.
- Unit/component tests cover cross-thread pagination, mutation navigation races, optimistic rollback, and reconnect.

## Phase 9 — Integration, documentation, and final verification

Status: complete

Acceptance criteria:

- Egg and EggW preserve the same core thread/tool/approval semantics.
- Full Python suite, frontend typecheck/build, and relevant browser tests pass.
- Security launch/configuration is documented.
- No generated test artifacts are committed.
- `review-20260709.md` remains untracked and uncommitted.

## Status notes

- 2026-07-09: Plan created. No implementation commits yet. Next: Phase 1.
- 2026-07-09: Phase 1 complete and committed. Added one centralized ASGI authorization/origin boundary for REST, SSE, and WebSocket; kept `/health` public; made the launcher generate and privately pass a high-entropy token; defaulted backend binding to loopback with `EGGW_PUBLIC=1` required for non-loopback; restricted CORS/origins; authenticated browser REST, fetch-based SSE, WebSocket subprotocols, and protected artifact/attachment fetches without token query parameters or logging. Added behavioral backend and launcher coverage and updated frontend test launch configuration/documentation. Verification: `PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests` (135 passed, 2 skipped); `cd eggw/frontend && npx tsc --noEmit --pretty false` (passed); `cd eggw/frontend && npm run build` (passed); `python -m compileall -q eggw/eggw eggw/tests`, `bash -n eggw/eggw.sh`, and `git diff --check` (passed). Next: manager review, then Phase 2 only when assigned.
- 2026-07-09: Phase 1 security review repair implemented. Removed the bearer token from all `NEXT_PUBLIC_*` build-time configuration. Loopback-only launcher mode still auto-generates a token but provisions it through a local-client-only, no-store runtime endpoint; public mode now requires an explicit operator token and disables private bootstrap. Public/manual browser users enter the token into runtime session state, which is used by REST/fetch-SSE/WebSocket auth and kept only in memory plus tab-scoped `sessionStorage` (never URL, logs, cookies, or `localStorage`). Added launcher assertions proving public mode neither generates nor passes a frontend/build token, source-policy coverage, and a production-build scan proving server tokens are absent from browser static assets. Verification: `PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests` (137 passed, 2 skipped); `cd eggw/frontend && npx tsc --noEmit --pretty false` and `npm run build` (passed); `python -m compileall -q eggw/eggw eggw/tests`, `bash -n eggw/eggw.sh`, and `git diff --check` (passed). Next: separate Phase 1 repair commit and manager review; no Phase 2 work.
- 2026-07-09: Phase 2 complete. Tool policy now defaults to secret-masked provider output and computes effective capability as the intersection of validated local policy across every live ancestor, so child allowlists/enables/raw-output settings cannot exceed any ancestor or escape later ancestor restrictions. Missing policy remains usable; ancestry/DB/decode/validation failures return a typed, distinguishable fail-closed config, deny RA1 exposure and RA2/RA3 execution, mask raw publication, and append deduplicated `tools.policy_error` diagnostics when the DB permits. Ordinary child creation resolves policy before insert, revalidates it inside the creation savepoint, and commits the child/link/initial policy event atomically; policy changes or append failures roll the child back. Tool status surfaces policy errors explicitly. Behavioral coverage includes safe defaults, ancestor restriction after creation, child widening attempts, raw-output inheritance, corrupt parent/child policy, DB read failure, RA1 exposure denial, execution denial, diagnostics, and transactional rollback. Verification: `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q eggthreads/tests` (952 passed); `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q egg/tests` (506 passed); `PYTHONPATH=eggw:eggthreads:eggconfig:eggllm pytest -q eggw/tests` (137 passed, 2 skipped); `python -m compileall -q eggthreads/eggthreads eggthreads/tests eggw/eggw` and `git diff --check` (passed). Next: Phase 2 commit and manager review; do not start Phase 3 without assignment.
- 2026-07-09: Phase 3 complete. Added a typed `LeaseLost` failure and one `InvocationEventWriter` authority whose append, stream-close, and release operations atomically require exact `(thread_id, invoke_id)` ownership plus an unexpired lease. All invocation-owned runner events now use that authority, including stream open/delta/close, provider-start/final/error/partial assistant messages, tool stream/status/lifecycle/completion/output-decision/publication events; cancellation and lease loss terminate without stale fallback writes, while cooperative cancellation still closes/releases a live owned lease and can never remove a takeover lease. Heartbeats cannot revive expired leases, expired takeover plus its interrupt boundary is transactional, and active interrupt deletion plus `control.interrupt` is transactional. Added deterministic two-connection barriers proving late LLM completion after interrupt and late tool completion after expiry/takeover append no stale events, plus direct expired/taken-over writer, release, heartbeat, and cancellation cleanup coverage. The stable schema is unchanged. Verification: `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q eggthreads/tests` (957 passed); `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q egg/tests` (506 passed); `PYTHONPATH=eggw:eggthreads:eggconfig:eggllm pytest -q eggw/tests` (137 passed, 2 skipped, same two pre-existing warnings); focused lease/cancellation/tool suites (116 passed); `python -m compileall -q eggthreads/eggthreads eggthreads/tests` and `git diff --check` (passed). Next: Phase 3 commit and manager review; do not start Phase 4 without assignment.
- 2026-07-09: Phase 4 complete. Added one typed `finalize_tool_output` authority for TC4-to-TC5 decisions. It obtains the SQLite writer lock, reconstructs current tool-call state, validates a per-call lifecycle `state_event_seq`, and appends through a SQL state/version CAS; runner callers additionally require the live invocation lease in that insert. Automatic/manual duplicate finalizers are idempotent/first-commit, while explicit user cancellation and omit have highest precedence over any unpublished automatic decision; persisted source priorities keep legacy competing logs deterministic. Terminal Egg, EggW REST/WebSocket/interrupt flows, normal automatic policy, and synthetic policy-denied/cancelled outputs all use the authority rather than directly appending output approvals. Partial publication requires a recoverable thread-owned artifact; policy/artifact/state/lease/append failures are typed/detectable and leave TC4 plus raw `tool_call.finished` output retriable, without clearing Terminal prompts or silently continuing. Publication plans preserve artifact paths, optimizer channels, audit metadata, raw-output inspectability, and existing provider sanitization. Interrupt/closed-stream reconstruction now stops at TC4 until the authority commits a decision. Deterministic coverage includes auto versus cancel/omit in both orders, auto versus manual, duplicate schedulers on independent DB connections, stale lease, stale lifecycle watermark, append rollback, artifact/policy failure, and Terminal/EggW retry UX. Verification: `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q eggthreads/tests` (965 passed); `PYTHONPATH=egg:eggthreads:eggconfig:eggllm pytest -q egg/tests` (507 passed); `PYTHONPATH=eggw:eggthreads:eggconfig:eggllm pytest -q eggw/tests` (138 passed, 2 skipped, same two pre-existing warnings); `python -m compileall -q eggthreads/eggthreads eggthreads/tests egg/egg egg/tests eggw/eggw eggw/tests` and `git diff --check` (passed). Next: Phase 4 commit and manager review; do not start Phase 5 without assignment.
- 2026-07-09: Phase 5 complete. Added typed `ThreadProjection`/`ProjectedMessage` views loaded through an explicit event-sequence watermark behind one projection/event-store boundary, so raw SQLite rows and `payload_json` decoding stay internal. The canonical reducer applies `msg.create`, ordinary edits, delete, explicit continue-skip edits, and continue control boundaries (including `preserve_on_continue`) while retaining full provider payloads plus create/update event IDs, timestamps, and sequence metadata. A coherent versioned snapshot can seed identical tail replay; missing, stale, malformed, legacy, newer-than-target, or inconsistent snapshots fall back to full replay, and snapshots are optional semantics. `SnapshotBuilder` now delegates to this core. `create_snapshot` captures a watermark, consumes the projection, keeps token stats isolated as derived snapshot metadata (including append-only incremental extension), and publishes monotonically with conditional updates; an N builder cannot overwrite N+1, and losing writers reload/return the newer snapshot. Equal-watermark legacy/invalid cache repair is compare-and-set safe. No RA1 or duplication consumer migration was done. Added no-snapshot/bounded-watermark/provider-field tests; snapshot-plus-tail versus full-replay equivalence across edit/delete/continue/preserved messages; stale/legacy repair; future-watermark rejection; and a deterministic two-connection publication barrier proving no regression and stale-builder reload. The stable schema is unchanged. Verification: `PYTHONPATH=eggthreads:eggconfig:eggllm pytest -q eggthreads/tests` (970 passed); `PYTHONPATH=egg:eggthreads:eggconfig:eggllm pytest -q egg/tests` (507 passed); `PYTHONPATH=eggw:eggthreads:eggconfig:eggllm pytest -q eggw/tests` (138 passed, 2 skipped, same two pre-existing warnings); `python -m compileall -q eggthreads/eggthreads eggthreads/tests egg/egg egg/tests eggw/eggw eggw/tests` and `git diff --check` (passed). Next: Phase 5 commit and manager review; do not start Phase 6 without assignment.

- 2026-07-09: Phase 6 complete. RA1 now captures one post-lease event watermark, loads the canonical `ThreadProjection` once, validates the discovered trigger against that view, and constructs the complete provider context from all effective messages instead of relying on `snapshot_json` plus one trigger tail. Snapshot presence/currentness is therefore acceleration only; compaction, pre-boundary system instructions, context-only turns, all eligible queued turns, provider-opaque fields, attachment lowering, tools protocol, and local usage/raw-output sanitization continue through the existing provider boundary. Tool policy exposure and output masking are also resolved through the same watermark. `duplicate_thread` and `duplicate_thread_up_to` now emit clean root histories from canonical effective messages and selected-watermark effective working-directory/sandbox/model/tool configuration; ordinary edits are materialized, deleted/continue-skipped messages are omitted, stale stream/control/tool-lifecycle events are not copied, pending effective tool declarations restart from their message state, and completed transcripts receive only a clean idle boundary. Destination creation/message/config/snapshot writes are atomic under one savepoint and the schema is unchanged. Added differential no/stale/current-snapshot RA1 coverage, captured-watermark/queued-turn/provider-field/usage tests, compaction/context-only/system/tool-protocol behavior, canonical duplicate edit/delete/continue/provider/config behavior, inherited child config, up-to watermark isolation, stale lifecycle omission, pending-tool reset, and completed-idle coverage. Verification: focused Phase 6 suites (150 passed); full EggThreads (977 passed); Egg (507 passed); EggW (138 passed, 2 skipped, same two pre-existing warnings); EggFlow (80 passed, 1 skipped); `python -m compileall -q eggthreads/eggthreads eggthreads/tests egg/egg egg/tests eggw/eggw eggw/tests eggflow/eggflow eggflow/tests` and `git diff --check` (passed). Next: Phase 6 commit and manager review; do not start Phase 7 without assignment.

- 2026-07-10: Phase 7 complete. Added a shared typed `ThreadEventFeed`/`ThreadEventEnvelope` cursor boundary over canonical `event_seq`, with strict cursor parsing/precedence, bounded batches, canonical event identity (`event_id`, `event_seq`, `type`, `ts`, `msg_id`, `invoke_id`, `chunk_seq`, decoded payload), typed missing-thread errors, and active replay derived only from an unexpired lease plus its exact invocation. EggW message snapshots now expose the exact projection watermark represented by their data through `{items, snapshot_cursor, next_before}` with `envelope=true`; legacy array pagination remains compatible and receives the same cursor in `X-Egg-Event-Cursor`. SSE accepts explicit `after_seq` over `Last-Event-ID`, emits `id: event_seq` for every canonical frame, starts cursorless live replay only for an exact active lease, returns `404` for absent threads, and retains bounded responsive polling on a dedicated cross-process database connection. The authenticated fetch-SSE frontend first establishes the transcript cursor, connects from it, tracks the last delivered SSE ID, resumes via `Last-Event-ID`, suppresses duplicate/out-of-order IDs, and no longer treats transport errors as durable stream completion. Updated mocked SSE/browser contracts and docs. Actual HTTP/SSE coverage proves the snapshot/connect race, legacy/envelope cursor responses, precedence, reconnect without duplicates/omissions, live exact-invoke replay, expired unmatched-open handling, canonical concurrent invoke/chunk identity, invalid cursors, and missing-thread `404`; focused EggThreads feed tests cover typed envelopes, bounded batching, cursor validation, and lease authority. Stable SQLite schema unchanged. Verification: full EggThreads (981 passed); full EggW (145 passed, 1 skipped, same two pre-existing warnings); Egg (507 passed); frontend `npx tsc --noEmit --pretty false` and `npm run build` (passed; stale Browserslist notice only); focused Playwright live-tool SSE flow (1 passed); `python -m compileall -q eggthreads/eggthreads eggthreads/tests egg/egg egg/tests eggw/eggw eggw/tests` and `git diff --check` (passed). Next: Phase 7 commit and manager review; do not start Phase 8 without assignment.
- 2026-07-10: Phase 8 complete. Persisted EggW transcripts now have one React Query infinite-query authority keyed by thread, with per-page cursors, overlap deduplication, authoritative tail reconciliation, and thread-isolated scrollback pagination; the global Zustand message array is removed. Composer drafts, staged attachments, stream/tool metadata, invocation ownership, SSE connection status, and mutable streaming buffers are thread-keyed, so navigation no longer clears or cross-contaminates source state. Async sends/uploads/images/commands/interrupts/approvals/settings/tree mutations carry source thread and operation IDs; send success replaces the exact optimistic ID with the backend ID, while failure removes only that operation and restores its original draft/attachments even after navigation. The authenticated SSE transport exposes reconnect state separately from run state, resumes from its durable `Last-Event-ID`, routes every canonical frame through a typed `{threadId, invokeId, eventSeq}` reducer before UI mutation, rejects malformed/stale/duplicate/foreign-invocation frames there, and refreshes the authoritative transcript/cursor before lease-backed run reconciliation on reconnect. Added Vitest infrastructure and focused behavioral tests for cross-thread pagination, overlap, mutation/navigation affinity, optimistic success/rollback, staged-input restoration, separated connection/run state, canonical event/invocation rejection, and transport reconnect dedupe; updated the live-tool Playwright fixture to canonical envelopes. Verification: frontend `npm run test:unit` (14 passed), `npx tsc --noEmit --pretty false`, and `npm run build` passed (stale Browserslist notice only); focused Playwright per-thread pagination and live-tool SSE flows (2 passed); EggW `TestEventStreaming` backend contract suite (7 passed, existing warnings only); `git diff --check` passed. Next: Phase 8 commit and manager review; do not start Phase 9 without assignment.
- 2026-07-10: Phase 9 complete. Added cross-client integration contracts proving EggW transcript envelopes match the canonical fixed-watermark EggThreads projection and Terminal Egg plus EggW converge through the shared transactional tool-output finalizer. Hardened launcher deployment semantics: private bootstrap now rests on an explicit loopback-only Next listener; public mode keeps upstream listeners loopback unless deliberately overridden and fails closed without an operator token, exact allowed origins, and an explicit browser-facing HTTPS API URL. Documented credential rotation, trusted-local-host limits, reverse-proxy/TLS requirements, projection/synchronization ownership, cursor recovery, and thread-scoped frontend state. Extended CI path detection and integration execution; removed checked-in Playwright output and ignored future generated reports. The full browser run exposed and fixed two real integration regressions: the deterministic MockLLM now implements the runner model-selection contract, and thread-scoped transcript caching preserves timestamp placement for local command results. Verification: full Python suite `PYTHONPATH=egg:eggw:eggthreads:eggconfig:eggdisplay:eggllm pytest -q eggllm/tests eggthreads/tests eggdisplay/tests eggflow/tests egg/tests eggw/tests integration_tests` (1,884 passed, 2 skipped; two existing warnings); frontend `npm run test:unit` (15 passed), `npx tsc --noEmit --pretty false`, `npm run build`, and full `npm test` Playwright suite (37 passed); `bash -n eggw/eggw.sh`, `python -m compileall -q eggthreads/eggthreads eggthreads/tests egg/egg egg/tests eggw/eggw eggw/tests integration_tests`, and `git diff --check` passed. Stable EggThreads SQLite schema unchanged. `review-20260709.md` remains untracked and excluded from the commit. Next: commit Phase 9, then final manager summary.
