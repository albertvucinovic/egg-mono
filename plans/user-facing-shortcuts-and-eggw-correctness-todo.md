# User-facing shortcuts, quick start, and EggW correctness TODO

## Scope and invariants

- Implement all five task groups requested on 2026-07-14.
- Preserve terminal input responsiveness and EggW incremental rendering performance.
- Keyboard shortcuts must survive common terminal/tmux/Sway paths, avoid normal Readline bindings, be mnemonic, and be listed comprehensively in `/help`/Help UI.
- Quick-start text is an unsent draft. A sole existing-file argument should use the existing attachment-staging model rather than silently inlining file bytes.
- Children-panel navigation must hydrate a useful initial transcript window (target within 60–300 messages) without waiting for reload.
- Tool call/result pairing must remain ID-based; never pair a result with an unrelated call merely because it is adjacent.
- `get_user_message_while_preserving_llm_turn` must leave a logical durable/visible sequence after an answer and must not remain visually streaming after terminal completion.
- Cover EggW max/medium/min and multi-tool behavior where applicable.
- Commit each coherent completed slice and keep this document current.

## Phase 1 — keyboard shortcuts and help

- [x] Audit existing Egg/EggW key bindings plus common Readline, tmux, and repository/user Sway conflicts.
- [x] Select mnemonic bindings for `/toggleAutoApproval` and `/toggleSandboxing`.
- [x] Implement in terminal Egg and EggW unless a client-specific limitation is documented.
- [x] Consolidate/list every app keyboard shortcut in terminal `/help` and EggW Help.
- [x] Add focused tests.

## Phase 2 — quick-start draft arguments

- [x] `egg.sh Tell me a story` opens with an unsent draft.
- [x] `eggw.sh Tell me a story` opens with an unsent draft.
- [x] A sole existing file is staged through existing attachment semantics where supported.
- [x] Preserve wrapper/reload arguments and shell quoting.
- [x] Add focused tests.

## Phase 3 — Children-panel navigation hydration

- [x] Reproduce route-click undersized transcript.
- [x] Fix initial navigation hydration to load a useful 60–300 message range.
- [x] Preserve pagination and performance bounds.
- [x] Add regression test proving click navigation, not only reload.

## Phase 4 — EggW min tool pairing

- [x] Reproduce bare `tool` / `tool tool tool` output cards.
- [x] Audit durable and streaming call/result correlation.
- [x] Fix pairing at the root without unsafe positional fallbacks.
- [x] Test max/medium/min and multiple fast/simultaneous tool calls.

## Phase 5 — get-user and multi-tool display lifecycle

- [x] Reproduce answered get-user remaining visually open/streaming.
- [x] Ensure completion clears streaming state and renders the answer logically as a displayed User message.
- [x] Audit pending, answered, interrupted, manager/child answer, reload, and multi-tool cases.
- [x] Add tests across display verbosity levels.

## Final validation

- [x] Relevant Python suites.
- [x] Frontend unit/type/lint/build tests.
- [x] Relevant Playwright flows/performance tests.
- [x] Full feasible suites.
- [x] `git diff --check` and clean tracked working tree.

## Status notes

- 2026-07-14: Created handoff. Previous completed commit `c44415a` is clean and unrelated. Beginning Phase 1.
- 2026-07-14: Phase 1 complete. Audited Egg/EggW handlers, stock Readline emacs bindings, stock and user tmux config, and `~/.config/sway/config` (including its Ctrl+Alt bindings). Selected mnemonic Ctrl+Alt+A (auto-approval) and Ctrl+Alt+X (sandboXing): neither is a default Readline command, tmux root/prefix binding, common browser/terminal action, nor an audited Sway chord; using X also avoids terminal XOFF on Ctrl+S. Implemented exact Ctrl+Alt terminal escape-sequence handling and layout-stable EggW `KeyboardEvent.code` handling while preserving composer drafts; disabled sandbox control remains respected. Terminal `/help` now prepends all terminal keyboard controls, and EggW Help groups all global, composer/autocomplete, focused transcript/tree, dialog, rename, and edit-answer controls. Focused tests cover exact matching, draft preservation, comprehensive help catalogs, TypeScript, and a browser flow with the composer focused. Tests passed: combined relevant Python suite (602), `npm run test:unit` (76), `npx tsc --noEmit`, `npm run build`, and focused Playwright shortcut flow (1). Next: Phase 2 only.
- 2026-07-14: Phase 2 complete. Added one shared quick-start parser: positional argv becomes an unsent draft with quoted argument whitespace retained, while a sole existing regular file becomes an attachment request rather than inlined bytes. Terminal Egg applies it to the existing input panel or `/attach` staging path and skips reapplication on `/reload`. EggW carries argv as backend-only JSON, exposes it only through a one-shot landing-page thread claim, stages files through the existing `cmd_attach`/artifact pipeline, and hydrates the established Zustand draft/attachment owners before navigation; no launch content is appended as a user message. Fixed Egg's previously ineffective bare `export` reload variables while adding a wrapper test that proves argv survives re-exec. Validation passed: Egg plus quick-start suites (565), EggW API (113 passed, 1 skipped), EggW security/launcher (18), frontend unit tests (78), TypeScript, production build, shell syntax, compileall, and focused Playwright landing flow (1). Next: Phase 3 only.
- 2026-07-14: Phase 3 complete. Root cause was not a ChildrenPanel-specific fetch or stale React Query cache: route clicks and direct/reload navigation already converge on the same thread-keyed infinite query and hydrate a 300-entry tail, but the shared `ChatPanel` render-window default mounted only 5 messages. Raised the reusable initial/more-history window to the minimum useful target of 60 for every route transition, retained the authoritative 300-entry query page and cursored pagination, preserved start-message anchoring and 60-message history expansion, and removed the unrelated dead 300 constant from `ChatPanel`. A Children-panel browser regression now clicks parent→child, proves one `limit=300` request, 140 loaded entries, exactly messages 80–139 mounted initially, no parent leakage, and expansion to 120 without another network request. Updated scroll/min-history expectations and the deterministic 300-message performance gate; typing still causes no static transcript commit and all rendering remains bounded. Validation passed: frontend unit tests (79), TypeScript, production build, focused click/scroll/min/pagination/performance Playwright flows (4), and `git diff --check`. Next: Phase 4 only.
- 2026-07-14: Phase 4 complete. The reproduced bare/repeated labels had two root causes: runner-published `role=tool` messages omitted the canonical tool name even though `tool_call_id` was present, and min rendering still used legacy name inference (including persisted stream previews), which can cross-wire fast same-name calls. Runner result and denial publication now persist `name`; shared frontend `toolPresentation` resolves older missing names only from an exact, non-conflicting call ID and gives true orphans stable ID-suffixed labels instead of bare `tool`. Min call/result correlation now pairs exclusively by exact `tool_call_id`; ID-less legacy results/previews remain separate. Live lifecycle/delta handling no longer falls back to a name or shared `"tool"` key, so malformed ID-less frames cannot merge simultaneous tools; semantic headers still publish once and high-rate bodies stay in mutable buffers. Unit coverage includes out-of-order same-name calls, missing halves, legacy previews, conflicting IDs, stable orphan labels, and malformed live output. Browser coverage proves two fast live tools retain separate arguments/results at max/medium/min and a reloaded durable transcript recovers names/pairs by ID without bare labels or cross-pairing. Tests passed: focused Python runner/tool suites (37), tool-message/EggW API suites (128 passed, 1 skipped), full frontend Vitest (84), TypeScript, production build, focused live/durable tool Playwright suite (7), and `git diff --check`. Next: Phase 5 only.

- 2026-07-14: Phase 5 complete. The open/streaming get-user card had two root causes: EggW's public transcript projection discarded the durable consumed-answer identity written by the asynchronous `msg.edit`, and live completion only removed a timeout without terminalizing the call/output cards. EggW now exposes display-only `consumed_by_tool_call_id`/`consumed_by_tool_name` plus manager provenance while keeping provider-only `no_api`/`keep_user_turn` private; ordered `msg.edit` frames patch the existing User card in place, survive a stale same-ID snapshot by event cursor, and mark only the exactly correlated live tool finished. Generic tool finish/interrupt events use the same finished-state transition, which clears elapsed/timeout presentation without deleting sibling tools; existing bounded live-tool reconciliation still retires cards when durable results arrive. Min rendering uses exact tool name and `tool_call_id` to replace an answered get-user's duplicate call/result activity with the visible Assistant Note → User answer sequence, while pending and interrupted calls and unrelated simultaneous tools remain inspectable; max/medium retain durable detail. Coverage includes ordinary and manager/child answers, pending, answered, interrupted, reload, stale-refetch chronology, all verbosity levels, and a live get-user plus still-running sibling. Validation passed: full frontend Vitest (89), TypeScript, production build, focused get-user Playwright (2), deterministic performance Playwright (2), full frontend Playwright (86), focused cross-layer get-user/manager/edit-answer Python suites (49), isolated security suite (18), and full EggW backend with the test origin explicitly isolated (223 passed, 1 skipped), plus compileall and `git diff --check`. The first unconstrained full EggW run exposed a pre-existing test-order/environment issue: `test_api` can leave a cached app configured for the launcher shell's port 3001, causing two later security assertions expecting port 3000 to fail; running the security file alone and the full suite with `EGGW_ALLOWED_ORIGINS=http://localhost:3000 EGGW_FRONTEND_PORT=3000` both pass. No security/runtime files were changed. Exact next task: commit Phase 5; all requested phases are complete.
