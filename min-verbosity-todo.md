# Min Verbosity Static Display TODO

Goal: make display verbosity `min` keep user and assistant messages visible while collapsing consecutive reasoning/tool activity into one live-updating summary line/block in the static display.

Desired behavior:
- `max` and `medium` remain unchanged.
- In `min`, user messages and assistant content messages stay visible as they are today.
- Consecutive hidden activity between visible messages is represented by a single summary item, not one panel per hidden detail.
- The summary text should read like: `Executed 47 tools, got 42 tool results, 10 reasoning blocks, total running time 12min, total tokens 45k`.
- After each new tool execution/result/reasoning block in full-screen static display, the single summary item should update in place rather than appending another summary.
- Only the single summary line counts reasoning blocks; do not emit separate reasoning rows in `min`.
- Include a compact tool-name list line when available, e.g. `Tools: bash, bash, python_repl, spawn_agent, bash, ...`.
- While streaming in `min`, show only a simple animation/indicator with stream type, not raw streamed content/reasoning/tool output.

Implementation notes:
- The current `min` path uses hidden-detail state and emits `Hidden Details` panels before visible user/assistant/system messages. Replace that behavior with a run summary model.
- A “run” means consecutive hidden activity bounded by visible messages. Tool calls, tool result messages, streamed tool-call args/output, and reasoning blocks in that run should merge into one summary item.
- In full-screen mode, in-session updates can replace the current summary line by rebuilding/replacing the renderer source or by updating the pending run summary model; avoid appending repeated summary panels.
- In inline/native scrollback mode, true in-place updates to terminal scrollback are not generally possible; preserve correctness by emitting concise summaries at visible boundaries unless a simple safe update path exists.
- For elapsed running time, use available event timestamps/tool execution events when practical; otherwise omit rather than invent inaccurate data. Prefer a minimal, tested implementation.
- For total tokens, use existing per-message token stats helpers/caches where available; for hidden content without per-message stats, approximate with existing `eggthreads.count_text_tokens` if needed.

Phases:

- [x] Phase 1 — Shared min run-summary model
  - Add focused helpers for min-verbosity hidden activity summaries.
  - Count tool executions/tool calls, tool results, and reasoning blocks.
  - Track tool names and token totals where available.
  - Format a single summary item with optional tool-list line.
  - Update `format_messages_text()` min behavior and tests.
  - Status notes:
    - Implemented shared `egg.min_run_summary` helpers for min hidden activity runs.
    - `format_messages_text()` now emits one run summary between visible messages, counts tool executions/results/reasoning blocks, includes known tool names, and totals hidden tokens from cached per-message stats or existing approximate token counting.
    - Elapsed running time intentionally omitted in Phase 1 because accurate per-run timing is not cheaply available in text formatting.
    - Focused formatting tests updated for no `Hidden details:` rows in min.

- [x] Phase 2 — Static panel renderables use run summaries
  - Replace `Hidden Details` min panels with the new run-summary renderable in `PanelsMixin` static transcript builders.
  - Ensure user/assistant visible message renderables are unchanged.
  - Update static panel tests and lazy `TranscriptScrollbackSource` tests if affected.
  - Status notes:
    - `PanelsMixin` min hidden state now stores a `MinHiddenActivitySummary` and renders the shared formatted summary body in an untitled yellow summary panel; `Hidden Details` title/text is no longer emitted.
    - Static transcript builders record hidden reasoning, tool calls/streamed args, tool outputs/results, tool names, and best-effort hidden token totals into the shared summary model while leaving visible user/assistant renderables and max/medium behavior unchanged.
    - Focused static panel tests now assert run-summary text/no `Hidden details:` output, including merged consecutive hidden activity before the next visible message.
    - Lazy `TranscriptScrollbackSource` block rendering now uses the same summary renderable; consecutive hidden blocks are aggregated by sharing `hidden_details` state across the `_ensure_rows` loop (flushed at visible boundaries via `_is_min_block_visible` heuristic).  Cross-cache aggregation (across separate `_ensure_rows` invocations) remains a known limitation when the viewport is filled before all hidden blocks are processed.

- [x] Phase 3 — Full-screen in-place summary update
  - Ensure consecutive in-session hidden activity in full-screen updates one summary item instead of appending repeated summaries.
  - Prefer source refresh/local-row replacement over private renderer mutation unless the renderer has/gets a small public API.
  - Add regressions for repeated tool calls/results/reasoning producing one summary item that updates counts.
  - Status notes:
    - Added `FullScreenDiffRenderer.replace_recent_scrollback(row_count, ...)` as a small public API for replacing the newest locally appended rows while preserving scroll position; `print_above()` now delegates to it for append-only behavior.
    - `PanelsMixin` tracks the rendered row count for the current full-screen min hidden-activity summary and refreshes that local row in place for consecutive hidden-only messages; visible user/assistant/error panels finalize the run and reset tracking.
    - Source replacement/redraw resets pending min summary state/tracking so locally replaced rows do not leak across a refreshed transcript source.
    - Added focused regressions for repeated in-session reasoning/tool-call/tool-result activity producing one local summary item with updated counts, plus renderer row-replacement coverage.
    - Follow-up fix: `TranscriptScrollbackSource._ensure_rows` now shares `hidden_details` state across consecutive blocks so that lazy scrollback rows also aggregate consecutive hidden activity into one summary per run.  Added `_is_min_block_visible` helper to determine when a block is a visible boundary that should flush the pending hidden state.

- [ ] Phase 4 — Min streaming simplification
  - In `min`, full-screen streaming should show only a small animated/type indicator (`llm`, `tool`, etc.) and no raw stream content/reasoning/tool output.
  - Inline `compose_chat_panel_text()` should similarly avoid raw streaming details in `min` and show only a compact indicator.
  - Add focused streaming tests.
  - Status notes:

- [ ] Phase 5 — Final focused test pass and caveats
  - Run focused egg tests and relevant eggdisplay renderer tests.
  - Document any intentional caveats, especially inline native scrollback limitations and elapsed-time availability.
  - Status notes:
