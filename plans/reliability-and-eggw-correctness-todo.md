# Reliability and EggW Correctness Follow-up TODO

Status: in progress (Phase 1 repaired; awaiting independent re-review)
Created: 2026-07-15
Branch baseline: `af7b2e9` (`Merge branch 'main' into refactor20260709`)

## Scope and governing invariants

Implement the user-reported reliability, lifecycle, synchronization, and presentation fixes from 2026-07-15 as small reviewed commits. Correct the canonical state/lifecycle authority before UI symptoms.

- Preserve all invariants in `plans/analysis/invariants.md` and `plans/analysis/found-invariants.md`, especially INV-023/027, INV-083, INV-085/088/089/090/099.
- A preserve-turn tool behaves like a normal durable tool call with explicit extra behavior; call/result pairing is exact-ID based.
- Bounded transcript rendering must never reduce history reachability or erase already loaded pages.
- Streaming follows the live edge only while the user remains there; programmatic scroll/timer updates must not create visual jumps.
- Provider errors retain actionable status/cause/retry information. Retry classification is conservative, bounded, and deterministic.
- State changed in Egg or EggW must converge through canonical events/refetch, not client-local assumptions.
- Commit every coherent slice, update this handoff, and keep tracked work clean.

## Phase 1 — Get-user wait authority and EggW lifecycle

Observed screenshot: two `get_user_message_while_preserving_llm_turn` cards are simultaneously shown as live, at roughly 2,243s and 585s, each with an 86,400s limit and a continuously ticking full tool card.

- [x] Reproduce multiple outstanding get-user waits in one thread, including manager/child messaging and reload.
- [x] Define and enforce which waiting call one normal User message answers; do not let one reply ambiguously satisfy multiple calls.
- [x] Supersede/cancel or otherwise terminalize obsolete waits through durable canonical tool lifecycle events; never merely hide them in EggW.
- [x] Ensure exact tool result/output remains in the transcript and provider protocol remains valid.
- [x] Make active waiting presentation compact and stable in EggW; do not render a large 24-hour countdown card that repaints every second.
- [x] Cover pending, answered, interrupted, duplicate/concurrent, manager/child, reload, max/medium/min, and unrelated sibling tools.

## Phase 2 — Web-search quota fallback

Current diagnosis: default auto search builds Tavily → SearXNG when a Tavily key exists, but Tavily marks only HTTP 429/5xx retriable. HTTP 432 “plan usage limit exceeded” is non-retriable, so `SearchOrchestrator` stops before SearXNG. Therefore the reported 432 case does **not** currently fall back.

- [ ] Classify Tavily plan/quota exhaustion responses (including status 432 and bounded response detail) as fallback-eligible without treating them as same-provider retryable.
- [ ] Preserve terminal behavior when Tavily is explicitly pinned as the only provider.
- [ ] Prefer a clear distinction between “try next configured provider” and “retry this provider” if the existing `retriable` bit conflates them.
- [ ] Add chain tests proving Tavily 432 falls through to SearXNG and diagnostics retain Tavily status/message.
- [ ] Audit analogous Tavily extract behavior, but do not route fetch through SearXNG (which is search-only).

## Phase 3 — Egg launcher reload hang

CI failure: `test_egg_wrapper_preserves_argv_across_reload` timed out after 15s while invoking `egg/egg.sh` with quoted arguments. Local success alone is insufficient.

- [ ] Reproduce or identify the CI-only hang from launcher process/env semantics.
- [ ] Audit recursive `exec "$SCRIPT_DIR/egg.sh"`, reload-state cleanup, inherited reload variables, venv activation, `.env`, and child process ownership.
- [ ] Replace recursion/ambiguous inherited state with a bounded, explicit reload loop if that is the root cause, preserving exact argv and cwd.
- [ ] Add deterministic tests for one reload, repeated reload bounds/failure, quoting, cleanup, and no leaked temp file/process.
- [ ] Run the full Egg test suite.

## Phase 4 — Auto-continue error policy

- [ ] Add exact conservative classification for OpenAI’s “An error occurred while processing your request. You can retry” server response and retry once.
- [ ] Classify “remote connection failure” and closely equivalent upstream connection/reset failures as transport errors.
- [ ] Choose reasonable bounded default delays and honor explicit Retry-After without loops; keep the existing one-attempt cap and recovery fences.
- [ ] Preserve full provider error detail and distinguish retry scheduled/applied/stopped.
- [ ] Add table-driven classifier and runner integration tests for positive and nearby permanent/ambiguous negative cases.

## Phase 5 — EggW transcript monotonicity and scroll/timer stability

- [ ] Reproduce React transcript state reverting to fewer messages/pages after refetch, SSE initialization, route switch, or stale snapshot.
- [ ] Make loaded-page retention monotonic unless an explicit authoritative deletion/compaction invalidates content; never replace multi-page cache with only a fresh tail.
- [ ] Reproduce live-edge flicker caused by a transient scroll-up then corrective scroll-down.
- [ ] Distinguish user scroll intent from programmatic layout/content/timer updates and coalesce follow-to-bottom into the appropriate render phase.
- [ ] Remove timeout-countdown layout churn/flicker; timer text must not cause transcript geometry or scroll ownership changes.
- [ ] Add deterministic unit tests plus browser tests for fast simultaneous results, countdown ticks, message height changes, long loaded history, pagination, and user scroll-away/live-edge return.

## Phase 6 — Compact operational/error presentation at min verbosity

- [ ] Define a shared compact semantic representation for errors, retry/auto-continue notices, manual continuation, and related recovery state.
- [ ] Implement parity in Egg and EggW at `/displayVerbosity min`; retain expansion/inspectability and chronology.
- [ ] Avoid hiding active errors or recovery decisions and avoid repeated verbose cards.
- [ ] Test all verbosity levels and both clients.

## Phase 7 — Cross-client model synchronization

- [ ] Reproduce `/model` in Egg not updating EggW’s selected dropdown.
- [ ] Ensure model changes publish/consume canonical settings events or invalidate/refetch the exact query.
- [ ] Handle simultaneous clients and stale responses without local dropdown rollback.
- [ ] Add cross-client backend/event/frontend tests.

## Phase 8 — EggW navigation and Children identity

- [ ] Show an explicit thread ID for every child even when it has a name; use a compact unique suffix only if the full ID is available via title/copy affordance.
- [ ] On EggW `/`, open an appropriate existing/recent thread or a neutral thread chooser instead of creating a new root on every start.
- [ ] Preserve quick-start semantics: when an unclaimed quick-start draft/attachment exists, create/claim exactly one suitable thread.
- [ ] Keep explicit New Thread as the creation action and test reload/restart/direct-link/empty-database/quick-start cases.

## Phase 9 — README normalization (discuss/review before implementation)

Current root README opens as an AI self-assessment and comparison essay. The user marked this item `[?]`, so do not rewrite it silently.

- [ ] Propose a normal README structure: concise product description, key capabilities, screenshots, install/quick start, Egg vs EggW, safety/configuration, core architecture, development/tests, and links to deeper docs.
- [ ] Preserve honest maturity/limitations in a short dedicated section rather than first-person conversational prose.
- [ ] Get user approval or a very clear manager decision before broad rewrite.

## Final validation

- [ ] Focused tests per phase and independent review for lifecycle/state changes.
- [ ] Full EggThreads, Egg, and EggW backend suites.
- [ ] Frontend unit tests, TypeScript, production build, and relevant/full Playwright.
- [ ] `git diff --check`, clean tracked worktree, and exact commit ledger below.

## Status notes / commit ledger

- 2026-07-15: Phase 1 repair complete, pending independent re-review. The canonical normal-message append boundary now acquires SQLite writer authority, selects the newest live lease-backed wait, terminalizes only older candidates, appends the reply, and records its exact consumed-by claim in one transaction; immediate sequential and simultaneous two-connection submissions prove only the earliest event claims and the waiter returns. Wait start is one lease-fenced transaction that validates the exact TC3 owner before note append/older retirement, so stale/lost-lease invocations write nothing. The shared incremental tool reducer now carries a bounded unresolved/recovery candidate index and exact note/claim/result metadata, eliminating full note scans and per-note state rebuilds; a 120-lifecycle SQL cost-shape regression guards the hot path. Recovery publishes successful TC4 output, retains pre-existing TC5 decisions, and lazily unskips pre-repair declarations/notes/results without schema migration or duplicate results. Provider-only projection coalesces completed get-user declarations/results into one literal exact-ID contiguous block, while canonical UI history remains event ordered. `/continue` validates before mutation. EggW wait-only tool streams neither install a new one-second timing interval nor show `streaming Ns`; ordinary sibling tools still time normally. WebSocket sends now restart/ensure the scheduler like HTTP. Validation: EggThreads `1336 passed`; Egg `560 passed`; EggW backend `226 passed, 1 skipped`; frontend Vitest `93 passed`; TypeScript; production build; Get-user Playwright `4 passed`; compile and diff checks. No Phase 2 work was started.

- 2026-07-15: Phase 1 independent review rejected premature completion in `bec869c`; repair is in progress. Blocking gaps: reply claim was not atomic with message append, wait start was not lease-fenced, successful-TC4 and legacy skipped waits were not recoverable, multi-wait provider projection dropped exact results, failed `/continue` mutated history, wait-only EggW still ticked, normal sends had quadratic historical-wait work, and WebSocket sends lacked scheduler restart parity. Phase 1 completion boxes are reopened until literal regressions and full validation pass.

- 2026-07-15: Plan created from the post-merge clean baseline `af7b2e9`. Read canonical invariants and prior `user-facing-shortcuts-and-eggw-correctness-todo.md`; several symptoms are regressions or uncovered multi-instance cases despite earlier single-lifecycle fixes. Immediate verified diagnosis: Tavily HTTP 432 is currently non-retriable and stops the Tavily→SearXNG chain. Screenshot confirms two concurrent long-lived get-user cards rather than only one stale answered card. Start with Phase 1 because ambiguous live wait authority is a durable correctness issue; Phase 2 and launcher CI follow as small backend slices before broader UI work.

- 2026-07-15: Initial Phase 1 implementation (`bec869c`; subsequently rejected by independent review). Root-cause reproduction against the real 6.6 GB event database found two threads with two started/unpublished get-user calls: old waits survived manual `continue_thread` rewinds because continuation skipped their messages without writing terminal tool lifecycles, while the wait implementation independently selected “the next user message after my note,” so concurrent pollers could race to consume one reply. Canonical authority now assigns a normal User reply to the newest unresolved effective get-user note; all Egg, EggW HTTP/WebSocket, and manager→child normal-input paths use one shared append boundary, reply claiming is SQLite write-serialized with durable consumed-by exact identity, and starting a newer wait terminalizes older waits. Obsolete, orphaned, continued, and interrupted waits receive ordinary exact-ID output-decision plus `role=tool` results; continuation explicitly retains those declaration/note/result messages so provider history remains valid. Existing TC5 decisions are published rather than overwritten. EggW recognizes the wait semantically, keeps its live call/output rows collapsed with stable “waiting for reply” text, suppresses its 86,400-second countdown/elapsed timer, and excludes a wait-only card from the one-second React timer while unrelated tools retain live detail/timing at max/medium/min. Regression coverage includes concurrent claimers, newest ownership, stale/orphan waits, continuation, pending/answered/interrupted, manager-child provenance, unrelated user tools, reload, all verbosity levels, a 2,243-second-old 86,400-second wait, and a timed sibling tool. Validation: full EggThreads `1323 passed`; full Egg `560 passed`; full EggW backend `225 passed, 1 skipped` with isolated origin; frontend Vitest `93 passed`; TypeScript; production build; focused Get-user Playwright `3 passed`; compile checks and `git diff --check`. This initial validation did not cover the blockers recorded above.
