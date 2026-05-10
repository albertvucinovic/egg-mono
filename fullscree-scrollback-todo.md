# Full-screen Virtual Scrollback TODO

Goal: make full-screen mode render transcript history as a lazy, virtual scrollback instead of eagerly printing every message into the alternate-screen renderer on startup/redraw. The initial full-screen paint must render only the visible transcript tail, while PageUp/mouse scroll lazily renders older history on demand. Inline mode should keep using native terminal scrollback.

Hard constraints:
- Keep full history accessible in full-screen mode; do not reintroduce a fixed “last N messages” cap.
- Do not eagerly render the full transcript just to populate in-memory scrollback.
- Keep transient streaming rows renderer-owned so `stream_end()` can remove them cleanly.
- Prefer small, focused changes and reuse existing formatting/rendering logic.
- Keep existing inline-mode behavior unchanged unless a test proves otherwise.
- Use this file as the coordination ledger; update status notes before each commit.

Current relevant behavior:
- `EggDisplayApp.run()` creates a `DiffRenderer` and currently calls `print_static_view_current()` in full-screen mode to seed history.
- `PanelsMixin.print_static_view_current()` walks `snapshot_messages()` and calls `console_print_message()`/`console_print_compaction_marker()`.
- In full-screen mode `_live_print()` routes to `FullScreenDiffRenderer.print_above()`, which appends rendered rows to private `_scrollback` and repaints.
- `FullScreenDiffRenderer._paint()` composes `_scrollback + stream_rows + live_lines` into the terminal viewport.

Design:
1. Add a public virtual scrollback source API to `eggdisplay.FullScreenDiffRenderer`.
   - The renderer should support an optional source that can return transcript rows from the bottom for a given terminal width, bottom offset, and requested height.
   - The renderer remains responsible for composing three segments: persistent virtual source, appended in-session `_scrollback` rows, transient stream rows, then live panel rows.
   - Startup should not call `print_above()` for historical rows.
   - Existing `print_above()` remains valid for in-session appended content and inline compatibility.

2. Add a lazy `TranscriptScrollbackSource` in `egg`.
   - It should use existing static transcript formatting/rendering logic to build message/compaction renderables.
   - It should render blocks from newest to oldest and stop as soon as the requested bottom window is satisfied.
   - Cache rows per terminal width and display verbosity; invalidate by replacing the source on redraw/thread switch/verbosity change.
   - It must not render all messages during initial full-screen startup when only the tail viewport is needed.

3. Make static transcript rendering pure enough to reuse.
   - Extract message/compaction renderable builders from `PanelsMixin` so the console printer and virtual source use one implementation.
   - Avoid monkeypatching `_live_print()` as a renderable-capture mechanism.

4. Wire full-screen mode.
   - In full-screen mode, install a `TranscriptScrollbackSource` on the renderer before the first `update()`/paint that needs history.
   - Stop calling `print_static_view_current()` for historical transcript seeding in full-screen renderer startup.
   - `/redraw`, terminal resize redraw, thread switch, and `/displayVerbosity` should replace/refresh the source and repaint the visible window.
   - Inline mode should continue to print static history to the terminal as before.

5. Tests.
   - Renderer unit tests: virtual source rows compose with `_scrollback`, stream rows, live rows; scrolling requests older rows; initial paint only asks for visible tail; offset clamps/behaves sanely at source top.
   - Egg tests: full-screen startup/redraw installs source and does not call `console_print_message()` for every historical message; inline static view still prints full history; display verbosity/redraw refreshes full-screen source.
   - Run focused tests for changed packages.

Phases:

- [x] Phase 1 — Renderer virtual source API
  - Add a minimal public source protocol/data shape to `eggdisplay/eggdisplay/renderers.py`.
  - Update `FullScreenDiffRenderer._paint()` and `_max_scroll_offset()`/scroll behavior to compose source rows lazily with `_scrollback`, stream rows, and live rows.
  - Keep behavior identical when no source is installed.
  - Add unit tests in `eggdisplay/tests/test_renderers_terminal_safety.py` or a focused renderer test file.
  - Status notes:
    - 2026-05-10: Added `FullScreenScrollbackSource` protocol and `set_scrollback_source()` on `FullScreenDiffRenderer`; paints now compose virtual source rows before in-session `_scrollback`, stream rows, and live rows while keeping the no-source path on the existing local model. Initial source paint asks only for the visible bottom slice and does not query total row count.
    - 2026-05-10: Added focused renderer tests for visible-tail source requests, source/local/stream/live composition, scrolling to older source rows, and top clamp behavior. Test runs: `python -m pytest eggdisplay/tests/test_renderers_terminal_safety.py -q` (15 passed), `python -m pytest eggdisplay/tests -q` (45 passed).

- [x] Phase 2 — Pure static transcript renderables
  - Extract reusable renderable-producing methods from `egg/egg/panels.py` for messages and compaction markers.
  - Keep `console_print_message()` and `console_print_compaction_marker()` as thin printers over these renderables.
  - Preserve existing static transcript output and tests.
  - Status notes:
    - 2026-05-10: Extracted static transcript message, compaction marker, and hidden-detail renderable builders from `PanelsMixin`; `console_print_message()` and `console_print_compaction_marker()` now only print the returned renderables. Builder calls avoid `_live_print()` capture/monkeypatching and support caller-owned hidden-detail state for later lazy transcript rendering.
    - 2026-05-10: Added focused panel tests proving message/compaction builders return renderables without printing. Test runs: `python -m pytest egg/tests/test_panels.py::TestConsolePrintMessage -q` (16 passed), `python -m pytest egg/tests/test_formatting.py egg/tests/test_panels.py egg/tests/test_integration_workflow.py -q` (111 passed), `python -m pytest egg/tests -q` (388 passed).

- [ ] Phase 3 — Lazy `TranscriptScrollbackSource`
  - Implement a source class in `egg` that reads the current snapshot/events and lazily renders transcript blocks from newest to oldest.
  - Cache rows by width/verbosity and avoid full-history rendering for bottom viewport requests.
  - Add focused tests proving the bottom window only renders enough tail blocks.
  - Status notes:

- [ ] Phase 4 — Full-screen wiring and redraw behavior
  - Install the source on full-screen renderers in `EggDisplayApp.run()` before initial paint/history display.
  - Stop eager full-history `print_static_view_current()` seeding in full-screen mode.
  - Update `redraw_static_view()` to refresh the source in full-screen mode and print full static history only in inline/non-renderer contexts.
  - Ensure thread switch/mode switch/display verbosity changes refresh source without duplication.
  - Status notes:

- [ ] Phase 5 — Integration polish
  - Run focused eggdisplay and egg test suites.
  - Inspect behavior around stream close/new messages: no duplicated transcript rows after source refresh/redraw, appended in-session rows remain visible until next source replacement.
  - Update this TODO with final test notes and remaining caveats.
  - Status notes:

