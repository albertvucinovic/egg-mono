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

- [ ] `egg.sh Tell me a story` opens with an unsent draft.
- [ ] `eggw.sh Tell me a story` opens with an unsent draft.
- [ ] A sole existing file is staged through existing attachment semantics where supported.
- [ ] Preserve wrapper/reload arguments and shell quoting.
- [ ] Add focused tests.

## Phase 3 — Children-panel navigation hydration

- [ ] Reproduce route-click undersized transcript.
- [ ] Fix initial navigation hydration to load a useful 60–300 message range.
- [ ] Preserve pagination and performance bounds.
- [ ] Add regression test proving click navigation, not only reload.

## Phase 4 — EggW min tool pairing

- [ ] Reproduce bare `tool` / `tool tool tool` output cards.
- [ ] Audit durable and streaming call/result correlation.
- [ ] Fix pairing at the root without unsafe positional fallbacks.
- [ ] Test max/medium/min and multiple fast/simultaneous tool calls.

## Phase 5 — get-user and multi-tool display lifecycle

- [ ] Reproduce answered get-user remaining visually open/streaming.
- [ ] Ensure completion clears streaming state and renders the answer logically as a displayed User message.
- [ ] Audit pending, answered, interrupted, manager/child answer, reload, and multi-tool cases.
- [ ] Add tests across display verbosity levels.

## Final validation

- [ ] Relevant Python suites.
- [ ] Frontend unit/type/lint/build tests.
- [ ] Relevant Playwright flows/performance tests.
- [ ] Full feasible suites.
- [ ] `git diff --check` and clean tracked working tree.

## Status notes

- 2026-07-14: Created handoff. Previous completed commit `c44415a` is clean and unrelated. Beginning Phase 1.
- 2026-07-14: Phase 1 complete. Audited Egg/EggW handlers, stock Readline emacs bindings, stock and user tmux config, and `~/.config/sway/config` (including its Ctrl+Alt bindings). Selected mnemonic Ctrl+Alt+A (auto-approval) and Ctrl+Alt+X (sandboXing): neither is a default Readline command, tmux root/prefix binding, common browser/terminal action, nor an audited Sway chord; using X also avoids terminal XOFF on Ctrl+S. Implemented exact Ctrl+Alt terminal escape-sequence handling and layout-stable EggW `KeyboardEvent.code` handling while preserving composer drafts; disabled sandbox control remains respected. Terminal `/help` now prepends all terminal keyboard controls, and EggW Help groups all global, composer/autocomplete, focused transcript/tree, dialog, rename, and edit-answer controls. Focused tests cover exact matching, draft preservation, comprehensive help catalogs, TypeScript, and a browser flow with the composer focused. Tests passed: combined relevant Python suite (602), `npm run test:unit` (76), `npx tsc --noEmit`, `npm run build`, and focused Playwright shortcut flow (1). Next: Phase 2 only.
