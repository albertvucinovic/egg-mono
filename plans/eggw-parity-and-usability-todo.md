# EggW Parity and Usability Follow-up TODO

Status: in progress (plan created; investigation begins with output-approval authority)
Created: 2026-07-17
Branch baseline: `000d5df` (`Commit schema-free Phase 5 transcript stability`)
Related discussion: `plans/cache-sidecar.md` (local proposal; no implementation authorized)

## Scope and governing rules

Continue the original reliability plan from Phase 6 through Phase 9 and add the
new EggW startup, transcript-parity, compact-inspection, and output-approval
requirements below.

- Treat `plans/analysis/invariants.md` as canonical. In particular, do not change
  the SQLite schema or storage authority without an explicit proposal,
  compatibility plan, and user approval.
- EggThreads owns scheduling, tool lifecycle, approval policy, and durable thread
  semantics. Egg and EggW are clients of those authorities; running EggW must not
  change whether an approval is required.
- Terminal Egg and EggW should preserve the same core chronology, headers,
  inspectability, visibility, and access semantics even when presentation differs.
- `displayVerbosity=min` may compact presentation, but must not reorder records,
  hide active errors/recovery decisions, or make canonical content unreachable.
- Prefer existing shared commands, projections, formatters, and autocomplete
  authorities over parallel EggW-only behavior.
- Interactive paths must remain bounded for very long threads and must preserve
  complete history reachability.
- Keep each implementation slice coherent, covered by literal regressions, and
  committed separately. Do not bundle unrelated cleanup.
- Report progress to the user at least every 30 minutes of wall-clock work and at
  completed/blocked slices. Stop and ask before major architectural, persistence,
  approval-policy, public-command, or destructive compatibility changes.

## Execution order

1. Phase 10 output-approval investigation: possible scheduler regression and
   unwanted user prompt.
2. Phase 6 compact presentation, chronology, header parity, default minimum
   verbosity, and `/show` inspection.
3. Phase 7 cross-client model synchronization.
4. Phase 8 startup/navigation and child identity.
5. Phase 9 README proposal only after explicit discussion/approval.
6. Full cross-client validation and independent review.

The numbering retains Phases 6–9 from the original plan. The urgent newly
reported approval issue is Phase 10 even though it is investigated first.

## Phase 6 — Compact presentation, chronology, headers, and inspection

### Shared compact semantics

- [ ] Inventory errors, retry/auto-continue notices, manual continuation,
  assistant notes, assistant messages, tool declarations/results, and other
  operational records in both Egg and EggW at every verbosity.
- [ ] Define or reuse one shared semantic representation for compact operational
  and error output rather than inferring chronology separately in each client.
- [x] Preserve canonical event chronology in EggW: assistant notes/messages and
  tool declarations/results must not be grouped or reordered incorrectly,
  including at `displayVerbosity=min`.
- [x] Add a literal interleaved chronology fixture (assistant message/tool call →
  Assistant Note/message → tool result → recovery/error) and require EggW min,
  medium, and max to retain the shared canonical message order. Cross-client Egg
  fixture parity remains part of the broader shared-semantics inventory.
- [ ] Keep active errors, approval/recovery decisions, and manual continuation
  inspectable without repeating large verbose cards.

### Header parity

Inventory matrix: `plans/eggw-header-parity-matrix.md`.

- [x] Inventory every header-bearing display component in Egg: timestamps,
  message/tool IDs, model/provider, token counts, TPS, duration/timeout,
  approval/result state, provenance, thread identity, and other existing fields.
- [x] Define which header fields are canonical/shared and which are intentionally
  client-specific; do not fabricate fields unavailable from shared state.
- [ ] Make EggW display all applicable Egg header information for messages,
  assistant notes, tool calls/results, errors, and every other rendered object
  that has a header in Egg.
- [ ] Preserve compact geometry at minimum verbosity while keeping full values
  available through title/copy/expansion or `/show`.
- [ ] Add parity tests from shared fixtures covering missing/partial headers,
  long IDs, token/TPS timing values, active and completed tools, and all
  verbosity levels.

### Default minimum verbosity

- [x] Change the existing session-local absence defaults to `min` for fresh
  terminal Egg and EggW frontend instances.
- [x] Preserve explicit in-session `min`/`medium`/`max` command and selector
  overrides without introducing persistence or cross-client synchronization.
- [x] Test fresh terminal/frontend state, explicit in-session medium/max, and
  browser reload returning to the fresh session-local default.
- [ ] Canonical persistence, restart survival, inheritance, malformed-state
  policy, and cross-client synchronization remain an unapproved design question.
  No shared setting authority exists today; do not imply these guarantees.

### `/show <id_hint>` command

- [x] Define `/show` as a shared command, not an EggW-only local shortcut.
- [x] Resolve an ID-autocompletion hint against currently accessible rendered
  messages, assistant notes, tool declarations/results, and other inspectable
  records using exact access/descendant rules.
- [x] Specify deterministic outcomes for exact match, unique prefix/suffix hint,
  ambiguous hint, missing/hidden/deleted item, and ID reuse across record kinds.
- [x] Render the selected record with complete inspectable headers and content
  regardless of current compact verbosity, without mutating transcript state.
- [x] Add suggestions and interactive autocomplete using the existing shared
  command/catalog completion path; keep completion bounded and stale-safe.
- [x] Implement parity in Egg and EggW and test suggestion, autocomplete,
  ambiguity, access denial, long output/artifact references, and all verbosity
  levels.

## Phase 7 — Cross-client model synchronization

- [ ] Reproduce `/model` in Egg not updating EggW's selected model dropdown.
- [ ] Identify and use the canonical settings/event authority; do not repair this
  with a client-local optimistic assumption.
- [ ] Ensure Egg-initiated and EggW-initiated model changes publish/consume the
  same canonical state or invalidate/refetch the exact query.
- [ ] Fence stale responses and handle simultaneous clients without dropdown
  rollback.
- [ ] Cover inherited/default model behavior, explicit thread override, reload,
  reconnect, simultaneous writes, and cross-process schedulers.
- [ ] Add shared backend/event tests plus Egg and EggW presentation tests.

## Phase 8 — EggW startup/navigation and child identity

### Do not create a root thread on every start

- [ ] Reproduce and trace every startup/navigation path that currently creates a
  new root thread, including `/`, browser launch, reload, direct links,
  quick-start drafts, and attachments.
- [ ] Define the desired empty-state contract before implementation. Discuss at
  least:
  - a non-thread neutral chooser/landing state;
  - opening an appropriate existing/recent thread; and
  - lazy creation when the user sends the first prompt.
- [ ] Treat a synthetic all-zero `000000...` thread as a design option only;
  evaluate collisions with ULID assumptions, foreign keys, descendant trees,
  scheduler ownership, commands, exports, and access rules before considering
  it. Do not implement it without explicit approval.
- [ ] Ask the user before selecting an option that changes startup/product
  semantics.
- [ ] Ensure a normal EggW start/restart does not create canonical thread/events
  merely by opening the UI.
- [ ] Preserve quick-start semantics: an unclaimed prompt or attachment creates
  or claims exactly one suitable thread when an actual durable action requires
  it.
- [ ] Keep explicit **New Thread** as the intentional eager-creation action.
- [ ] Test empty database, existing database, `/`, reload, restart, direct thread
  URL, invalid/deleted URL, two tabs, concurrent startup, quick-start text,
  attachment-only start, and explicit New Thread.

### Child identity

- [x] Show an explicit thread ID for every child even when it has a name.
- [x] A compact unique suffix is acceptable only when the complete ID remains
  available through title, copy, details, or `/show`.
- [x] Preserve identity across duplicate child names, renamed children, nested
  descendants, route switches, and all verbosity levels.
- [x] Add backend/frontend tests for duplicate names and full-ID access.

## Phase 9 — README normalization (discussion and approval required)

The root README currently opens as an AI self-assessment/comparison essay. This
phase is intentionally proposal-first.

- [ ] Draft a proposed normal README structure: concise product description,
  capabilities, screenshots, installation/quick start, Egg vs EggW,
  safety/configuration, architecture, development/tests, limitations, and links
  to deeper documentation.
- [ ] Preserve honest maturity and limitations in a short dedicated section
  rather than conversational first-person prose.
- [ ] Show the outline/sample opening to the user and receive explicit approval
  before a broad README rewrite.
- [ ] If approved, implement and commit the documentation rewrite separately from
  product code.

## Phase 10 — Output-approval and long-output authority (investigate first)

Reported symptom: a user may again be asked how to publish long tool output even
though long-output optimization previously handled this automatically. It may be
correlated with EggW running, but EggW must not own or alter scheduling/approval
policy.

### Reproduction and authority audit

- [ ] Capture the exact unwanted approval card/prompt and its canonical event
  sequence, tool call ID, scheduler owner, policy configuration, output size,
  optimizer decision, and whether EggW was merely connected or actively issuing
  a command.
- [ ] Reproduce the same tool output matrix with:
  - Egg only;
  - EggW only;
  - Egg and EggW simultaneously;
  - one and multiple schedulers;
  - default, explicitly pinned, and legacy approval-policy settings; and
  - output below/above preview, storage, and optimizer thresholds.
- [ ] Trace the shared lifecycle from tool completion through output optimizer,
  output approval/decision, publication, long-output artifact/storage, and final
  tool message.
- [ ] Prove whether EggW startup or API/SSE reads can mutate approval policy,
  append an approval request, race the optimizer, or cause another scheduler to
  make a different decision.
- [ ] Distinguish intended input/tool-execution approval from output-publication
  approval; do not weaken security approval merely to remove a presentation
  prompt.

### Required behavior

- [ ] Successful long-output handling automatically chooses and durably records
  the appropriate whole/partial/omit/extracted/artifact-backed publication
  decision without interactive user approval under the established default
  policy.
- [ ] Exactly one scheduler/lease-fenced authority finalizes each output decision;
  concurrent Egg/EggW schedulers converge on the same durable result.
- [ ] EggW renders canonical approval/optimizer state but cannot create a parallel
  policy or scheduling path.
- [ ] Optimizer failure, malformed output, storage failure, explicitly configured
  manual policy, denial, cancellation, and recovery remain fail-closed and
  inspectable; automatic behavior must not silently discard output.
- [ ] Large raw output remains reachable through the existing long-output
  storage/extraction affordances while provider/UI payloads stay bounded.

### Regression coverage

- [ ] Add event-level tests for automatic output decision and exact-once
  publication across two competing schedulers.
- [ ] Add Egg and EggW integration tests proving that merely running/connecting
  EggW does not change the decision or introduce an approval prompt.
- [ ] Cover process restart, lease expiry/takeover, orphan recovery, duplicate
  completion, optimizer success/fallback/failure, and explicit manual policy.
- [ ] Verify all verbosity levels preserve chronology and inspectability of the
  automatic decision without presenting a false pending approval.

## Cache-sidecar discussion dependency

- [ ] Review `plans/cache-sidecar.md` with the user before any implementation.
- [ ] Do not change canonical SQLite schema or add a derived storage authority
  until location, cold-cache UX, cursor consistency, disk budget, lifecycle,
  and rollout decisions receive explicit approval.
- [ ] Keep sidecar work separate from Phases 6–10 unless an approved design is a
  literal prerequisite for one bounded backend behavior.

## Validation and acceptance

- [ ] Focused tests and a small coherent commit for each completed slice.
- [ ] Independent adversarial review for scheduler/approval, lifecycle,
  chronology, access-control, and cross-client state changes.
- [ ] Full EggThreads and Egg backend suites.
- [ ] Full EggW backend suite.
- [ ] Frontend unit tests, TypeScript, production build, and relevant/full
  Playwright.
- [ ] Representative >5M-token or equivalent fixtures prove bounded input,
  streaming, pagination, header rendering, `/show` completion, and chronology
  without sacrificing history reachability.
- [ ] Multi-process Egg/EggW tests prove canonical convergence.
- [ ] `git diff --check`, clean tracked worktree after each commit, and exact
  commit ledger below.

## Status notes / commit ledger

- 2026-07-17: Phase 6 shared `/show <id_hint>` slice implemented without schema,
  persistence, or transcript mutation. One shared resolver uses the canonical
  effective message projection for the explicitly selected current thread/view;
  it never scans descendants, ancestors, or siblings implicitly. Exact full ID
  wins, otherwise a unique case-sensitive prefix/suffix selects; ambiguity
  returns at most 10 newest candidates with no selection, and inaccessible,
  deleted, skipped, or missing IDs share one non-leaking miss result. Candidate
  identities are existing durable message/Assistant Note IDs, assistant
  tool-call IDs, and durable tool-result message IDs; exact call/result pairing
  remains `tool_call_id` based. Shared catalog completions are bounded to 20 and
  reuse Egg's async completion generation fence and EggW's abort/sequence fence.
  Terminal Egg renders the shared target at full detail while restoring the
  current verbosity; EggW receives the same authoritative target and opens a
  read-only full-detail modal without appending command output or changing
  verbosity. Optimized long output stays bounded and retains its existing raw
  artifact recovery hint. Validation: shared command/projection tests 62 passed;
  Egg display/completion/panel tests 186 passed; EggW backend
  show/autocomplete/display/help tests 12 passed; frontend unit tests 124
  passed; TypeScript and production build passed; focused Playwright 1 passed;
  full EggThreads 1530, Egg 612, and EggW backend 235 passed (plus one existing
  skip); compileall and `git diff --check` passed. Commit hash follows after
  final review; no other phase work started.

- 2026-07-17: Phase 6 default-min scope was narrowed by manager decision after
  investigation found no canonical/persisted display-verbosity authority.
  Terminal Egg and EggW previously held independent process-local defaults and
  mutations; EggW's backend command is explicitly frontend-only. This slice
  changes only those existing absence defaults to `min`, aligns terminal fallback
  rendering/command reads, and preserves explicit in-session medium/max/min
  overrides. Browser reload/new frontend state returns to `min` by design; no
  persistence, event/API field, inheritance, malformed-state policy, or
  cross-client synchronization was added. Future canonical persistence remains
  open and unapproved. Validation: terminal display tests 21 passed; shared
  command-registry tests 2 passed; EggW unit tests 123 passed; TypeScript and
  production build passed; focused Playwright 1 passed. The commit hash is
  reported in the final handoff. No other Phase 6 or excluded work started.

- 2026-07-17: Phase 6 header-parity inventory and first bounded implementation
  completed. The explicit matrix in `plans/eggw-header-parity-matrix.md` traces
  ordinary user/assistant messages, Assistant Notes, tool declarations/live
  tools/results, recovery/errors, compaction markers, pending approvals, and
  operational records to terminal Egg headers and shared EggW data. The first
  coherent available-data group required no protocol work: one shared
  `MessageHeaderFields` renderer now exposes model, content tokens, TPS, local
  timestamp, copyable message ID, and copyable tool-call ID on durable message
  headers at max/medium/min and on min compact reasoning/tool cards. Min keeps
  compact suffixes while title/ARIA/copy preserve exact IDs; max retains visible
  full IDs. Documented gaps requiring later decisions include absent
  `runner_error` and compaction timestamp API fields, live/approval header
  terminology, and non-durable operational records. Validation before commit:
  frontend unit `121 passed`; TypeScript and production build passed; bounded
  header/chronology/get-user/performance/accessibility Playwright envelope
  `17 passed`, including the 5M-token-equivalent fixture; `git diff --check`
  passed. Commit hash follows after creation. No `/show`, default-min, startup,
  model-sync,
  output-approval, sidecar, or README work started.

- 2026-07-17: Phase 6 chronology-ordering slice implemented. Reproduction proved
  the backend snapshot and ordered SSE `msg.create` feed already retained
  canonical `event_seq` order; the defect was EggW min-only presentation
  grouping. `renderMessagesForVerbosity()` accumulated hidden tool details across
  records, paired declarations/results by ID across intervening Assistant Notes,
  and emitted the combined block at a later visible boundary. Min now emits each
  compact reasoning/tool declaration/result at its source message position,
  retains full tool-call identity beside each inspectable entry, and keeps
  medium/max on the same transcript ordering authority. Literal browser coverage
  uses equal, missing, and adversarial timestamps with explicit event sequences;
  covers multiple interleaved calls, notes/messages, results, recovery notice,
  initial load, reload, and live-to-durable completion. Focused validation before
  commit: frontend unit `121 passed`; TypeScript and production build passed;
  chronology/live/get-user/performance Playwright envelope `17 passed`, including
  the 5M-token-equivalent fixture; `git diff --check` passed. The focused Playwright
  rerun used `--reporter=line` under an external timeout after an HTML-report
  process left only defunct children; listener 9323 is clear. Commit hash follows
  after creation. No other Phase 6 or excluded phase work started.

- 2026-07-17: Phase 8 child-identity slice implemented. Every Children panel
  row now shows an explicit compact thread-ID suffix independently of the
  optional name, exposes the canonical full ID through row/ID titles and
  accessibility labels, and provides a full-ID copy control that does not
  navigate. Unnamed children use a neutral label instead of duplicating the
  suffix; duplicate names remain distinguishable by ID. The same panel behavior
  applies to nested descendants, route switches, and all transcript verbosity
  settings because it is outside transcript presentation. Literal backend
  coverage verifies duplicate-name children retain distinct full canonical IDs;
  Playwright verifies duplicate/unnamed rows, full-ID keyboard copy, navigation
  isolation, all-theme geometry/focus, and mobile visuals. Validation before
  commit: full EggW backend `232 passed, 1 skipped`; frontend unit `121 passed`;
  TypeScript and production build passed; full Playwright `100 passed` (including
  the 5M-token-equivalent performance fixture); Python compile and
  `git diff --check` passed. Commit hash follows after creation. No chronology
  work started.

- 2026-07-17: Phase 10 first investigation stopped at bounded evidence on user priority change; no product repair or broad validation was attempted. Canonical lifecycle is `tool_call.finished` (durable raw output/TC4) → runner `_finalize_auto_tool_output()` → shared output-policy/optimizer registry → transactional lease/version-fenced `finalize_tool_output()` → one durable `tool_call.output_approval` (TC5) → final `role=tool` message (TC6); long whole output is automatically routed to a bounded preview plus thread-owned artifact/read instructions. Execution/security approval is the separate TC1 `tool_call.approval` authority. EggW GET/API/SSE reads do not write approval policy; its only approval writes are explicit user POST/WebSocket actions, while visiting/sending starts the same shared `SubtreeScheduler`. Existing exact-once tests cover competing finalizers and one long-output artifact. The literal unwanted prompt is reachable whenever Terminal Egg observes any durable TC4: `compute_pending_prompt()` still converts every TC4 with output into the legacy “include all?” prompt, and EggW similarly renders every TC4 as output approval. The likely trigger is a crash/lease-loss window after `tool_call.finished` but before the same invocation commits automatic policy: ordinary TC4 is not scheduler-actionable, so another scheduler cannot finalize it; current orphan recovery is limited to TC3 and a narrow interrupted/no-finished-event TC4. Open questions for later Phase 10 work: reproduce that exact inter-statement crash across Egg/EggW schedulers, decide whether successful durable TC4 should become lease-fenced recovery work versus suppressing obsolete UI prompts, and define explicit manual-policy configuration (none was found in current output-policy config) before removing manual fallback. Historical commits show automatic long-output handling was established before the legacy prompt was re-exposed by `e67ba30`. No SQLite/schema change is implicated.

- 2026-07-17: Plan created from clean `000d5df`. No Phase 6–10 product change has
  started. First implementation activity is a read-only/literal investigation of
  Phase 10 output-approval authority. Major design or policy changes require
  explicit user approval before implementation.
