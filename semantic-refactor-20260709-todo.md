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

Status: pending

Acceptance criteria:

- A canonical projection at a fixed event watermark applies create/edit/delete/continue and preserves provider-specific fields.
- Snapshot is an optional acceleration only, never a semantic prerequisite.
- Snapshot publication is monotonic/CAS-safe under concurrent builders.
- Projection contracts are typed enough to keep raw `payload_json`/SQLite rows internal.
- Tests cover no snapshot, stale snapshot, edits/deletes, and concurrent publication.

## Phase 6 — Migrate high-risk projection consumers

Status: pending

Acceptance criteria:

- RA1 provider context uses the canonical projection and cannot omit system/history/queued turns due to missing snapshot.
- Thread duplication/forking uses effective projected state and does not resurrect edited/deleted messages.
- Existing compaction/provider-field behavior remains intact.
- Differential/behavioral tests cover provider context and duplication.

## Phase 7 — Cursor-resumable EggW synchronization

Status: pending

Acceptance criteria:

- Message snapshot response exposes its event cursor/watermark without breaking pagination compatibility.
- SSE accepts explicit `after_seq` and browser `Last-Event-ID`, emits SSE `id`, and uses canonical event envelopes.
- Snapshot-to-live connection has no omission gap.
- Reconnect is duplicate-safe; active work comes from unexpired leases, not unmatched historical opens.
- Nonexistent threads return `404`.
- Tests cover snapshot/connect race, reconnect, duplicates, expired lease, and missing thread.

## Phase 8 — Per-thread frontend state and reconciliation

Status: pending

Acceptance criteria:

- Persisted transcript pages are owned per thread (prefer React Query/infinite query), not one global active-thread array.
- Every async mutation is bound to originating `threadId` and operation/optimistic ID.
- Optimistic send success replaces the temporary ID; failure removes it and restores draft/attachments.
- SSE connection state is separate from thread run state.
- Reconnect performs authoritative reconciliation and rejects stale/duplicate event sequences.
- Unit/component tests cover cross-thread pagination, mutation navigation races, optimistic rollback, and reconnect.

## Phase 9 — Integration, documentation, and final verification

Status: pending

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
