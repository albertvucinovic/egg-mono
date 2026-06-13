# EggW parity TODO

Goal: bring EggW (`eggw`) back into parity with terminal Egg (`egg`) and the shared `eggthreads` command/tool semantics for user-visible features implemented in the last few weeks.

This document is the durable manager/worker handoff. Use `worker-manager` for implementation and use `infinite-turn` in the primary worker: each slice should update this file, run focused tests, commit a coherent chunk, and hand back status through `get_user_message_while_preserving_llm_turn`.

## Constraints and operating rules

- Keep EggW behavior aligned with shared `eggthreads` implementations instead of duplicating logic when practical.
- Prefer root-cause fixes over web-only patches that will drift again.
- Keep phases small enough for one coherent commit each.
- Update this TODO before every implementation commit.
- Preserve unrelated untracked files such as `count-lines.sh`.
- Focused tests are acceptable per phase; run broader suites before final completion.

## Phase 0 — Plan and baseline

- [x] Audit terminal Egg vs EggW parity gaps.
- [x] Write this hierarchical TODO.
- [ ] Establish a small parity-test strategy so future drift is caught.
  - Candidate: command registry / dispatcher parity tests for commands that should exist in both frontends.
  - Candidate: snapshot tests for `/cost` output sections shared between Egg and EggW.

Status notes:
- 2026-06-13: Audit found major drift caused by EggW hand-dispatching slash commands and separately formatting UI/status output. TODO created.

## Phase 1 — `/cost` and token/cost stats parity

Problem: EggW has a stale `/cost` implementation and a simplified `/stats` route. Terminal Egg/shared `/cost` now reports API-confirmed usage, estimated/actual calls, cached hit rate, cache-creation tokens/costs, full vs current-provider context usage, compacted-away tokens, and richer per-model details.

Reference files:
- Shared current command: `eggthreads/eggthreads/builtin_plugins/diagnostics.py`
- EggW stale command: `eggw/eggw/commands/utility.py`
- EggW stats model/route: `eggw/eggw/models.py`, `eggw/eggw/routes/stats.py`

Tasks:
- [ ] Reuse or mirror the shared `/cost` formatting in EggW so output includes:
  - [ ] full effective history usage;
  - [ ] current provider context usage after last compaction;
  - [ ] compacted-away token count;
  - [ ] cached input hit rate;
  - [ ] actual API-confirmed call count;
  - [ ] estimated call count;
  - [ ] `API-confirmed usage:` section with `Not available` when absent;
  - [ ] cache-creation input tokens;
  - [ ] cache-creation cost;
  - [ ] richer per-model breakdown.
- [ ] Expand EggW stats API/model enough for browser header/system display to consume precise fields without losing existing fields.
- [ ] Fix EggW live streaming TPS bug in `eggw/eggw/routes/stats.py` (`datetime` is used but not imported).
- [ ] Add focused tests for EggW `/cost` output and `/stats` shape.

Status notes:
- Pending.

## Phase 2 — Tool timeout countdown/header parity

Problem: terminal Egg computes dynamic timeout countdown locally from `tool_call.execution_started.timeout` and aliases. EggW receives the event but does not store/display timeout state. Since countdown summaries are no longer persisted, EggW often shows no timeout for running tools.

Reference files:
- Terminal timeout ingestion/rendering: `egg/egg/streaming.py`, `egg/egg/panels.py`
- EggW event/store/rendering: `eggw/frontend/src/hooks/useSSE.ts`, `eggw/frontend/src/lib/store.ts`, `eggw/frontend/src/components/ChatPanel.tsx`, `eggw/frontend/src/components/SystemPanel.tsx`

Tasks:
- [ ] Store tool execution start time and resolved timeout when EggW receives `tool_call.execution_started`.
- [ ] Accept canonical and legacy timeout payload keys: `timeout`, `timeout_sec`, `timeout_seconds`, `timeout_secs`, `timeout_s`, `_tool_timeout_sec`, `_egg_tool_timeout_sec`.
- [ ] Render a dynamic `timeout in Ns (limit Ns)` indicator for active tool streams/status, analogous to terminal Egg.
- [ ] Ensure timeout remains visible when tool summary/suppressed-output status is also visible.
- [ ] Ensure timeout display survives pending user messages / message invalidations while the tool remains active.
- [ ] Add frontend/unit/e2e coverage where practical.

Status notes:
- Pending.

## Phase 3 — `get_user_message_while_preserving_llm_turn` web UX/status/cancel parity

Problem: core tool semantics exist, but EggW lacks the terminal affordance and turn-control behavior for active get-user waits.

Reference files:
- Core/shared: `eggthreads/eggthreads/builtin_plugins/answer_user.py`, `eggthreads/eggthreads/api.py`
- Terminal UI/cancel: `egg/egg/panels.py`, `egg/egg/input.py`, `egg/egg/approval.py`
- EggW frontend/backend: `eggw/frontend/src/components/MessageInput.tsx`, `eggw/eggw/routes/threads.py`, `eggw/eggw/routes/messages.py`

Tasks:
- [ ] Expose active get-user waiting state in EggW thread state/settings APIs.
- [ ] Show distinct input mode while answering get-user tool call:
  - [ ] header/label like `Message Input (get answer tool)`;
  - [ ] distinct border/color;
  - [ ] status text explaining that the next normal message answers the tool.
- [ ] Allow normal input submission while the thread is in active get-user wait even if an LLM/tool stream is otherwise considered active.
- [ ] Route that normal message so it answers the active tool call using shared core semantics rather than creating an unrelated user turn.
- [ ] Implement cancel behavior equivalent to terminal Ctrl+C:
  - [ ] close the get-user tool call with `User interrupted...`;
  - [ ] publish a tool result with `keep_user_turn` where needed;
  - [ ] return UI to normal input mode.
- [ ] Add tests for state, input mode, answer submission, and cancel.

Status notes:
- Pending.

## Phase 4 — Slash-command parity / shared command registry

Problem: EggW hand-dispatches slash commands and has a static `/help`, causing drift from terminal Egg and shared plugins.

Reference files:
- Shared registry: `eggthreads/eggthreads/command_catalog.py`
- Shared plugins: `eggthreads/eggthreads/builtin_plugins/*`
- EggW dispatcher/help: `eggw/eggw/commands/__init__.py`, `eggw/eggw/commands/utility.py`

Tasks:
- [ ] Decide and implement a thin EggW adapter around `CommandRegistry` for commands that can use shared handlers.
- [ ] Keep web-only commands explicit: `/theme`, `/rename`, `/spawn` alias, browser-specific `/redraw`/`/displayMode` no-ops if still desired.
- [ ] Add `/btw` support in EggW.
- [ ] Make EggW `/help` generated from the shared registry plus web-only commands.
- [ ] Add parity tests ensuring commands advertised by autocomplete/help are executable or intentionally web-only/terminal-only.
- [ ] Revisit duplicated thread command behavior after registry adapter exists.

Status notes:
- Pending.

## Phase 5 — `/waitForThreads` parity

Problem: terminal/shared `/waitForThreads` queues a `wait` tool call and uses get-user-aware wait semantics. EggW currently blocks inside the HTTP command handler via `wait_subtree_idle`.

Reference files:
- Shared: `eggthreads/eggthreads/builtin_plugins/subagents.py`
- EggW: `eggw/eggw/commands/utility.py`

Tasks:
- [ ] Change EggW `/waitForThreads` to use the shared wait-tool-call behavior or an equivalent queued tool call.
- [ ] Match selector resolution with shared behavior.
- [ ] Ensure get-user waiting threads are treated as waiting-user, not indefinitely running.
- [ ] Add focused tests.

Status notes:
- Pending.

## Phase 6 — Output approval and long-output parity

Problem: EggW manual `Partial` output approval publishes only a shortened preview, while terminal/shared behavior preserves recoverability for full output via artifact/readback paths.

Reference files:
- EggW output approval: `eggw/eggw/routes/tools.py`
- Shared/terminal long-output handling: `eggthreads/eggthreads/builtin_plugins/long_output.py`, `egg/egg/approval.py`

Tasks:
- [ ] Inspect shared long-output approval payload conventions.
- [ ] Make EggW partial approval preserve artifact/readback metadata or use the same helper as terminal.
- [ ] Ensure UI copy explains how to read full output if partial is approved.
- [ ] Add regression tests.

Status notes:
- Pending.

## Phase 7 — Persisted streamed-tool metadata parity

Problem: live EggW streams current tool output, but historical message fetch drops persisted streamed-tool metadata such as `tool_stream` and `tool_calls_stream`.

Reference files:
- Terminal renderers: `egg/egg/formatting.py`, `egg/egg/panels.py`
- EggW API/frontend: `eggw/eggw/models.py`, `eggw/eggw/routes/messages.py`, `eggw/frontend/src/components/ChatPanel.tsx`

Tasks:
- [ ] Extend EggW message model/API to include persisted streamed-tool metadata.
- [ ] Render historical streamed tool-call args/output summaries in max/medium/min display modes.
- [ ] Include tool-call IDs and names consistently with terminal Egg.
- [ ] Add tests for reload/history display.

Status notes:
- Pending.

## Phase 8 — Display verbosity parity

Problem: EggW `min` verbosity summaries are simpler than terminal Egg and omit detailed token/tool-name summaries and some ordering semantics.

Reference files:
- Terminal: `egg/egg/formatting.py`, `egg/egg/min_run_summary.py`
- EggW: `eggw/frontend/src/components/ChatPanel.tsx`

Tasks:
- [ ] Align hidden reasoning/tool call/tool result summaries with terminal semantics.
- [ ] Include token totals/tool names where available.
- [ ] Match ordering of hidden-detail summaries relative to visible messages where feasible.
- [ ] Add focused frontend tests if practical.

Status notes:
- Pending.

## Phase 9 — New-thread system prompt parity

Problem: terminal Egg appends the loaded system prompt to newly created root threads. EggW `cmd_new_thread` and the thread creation route call `create_root_thread` but do not appear to append the system prompt.

Reference files:
- Terminal: `egg/egg/app.py`
- EggW: `eggw/eggw/commands/thread.py`, `eggw/eggw/routes/threads.py`

Tasks:
- [ ] Confirm current EggW root-thread behavior with a focused test.
- [ ] Append the correct system prompt for new EggW root threads.
- [ ] Ensure `/newThread` and API-created root threads match.
- [ ] Avoid duplicating system prompts on child threads or reload.

Status notes:
- Pending.

## Phase 10 — Final parity verification

Tasks:
- [ ] Run focused EggW backend tests.
- [ ] Run focused EggW frontend typecheck/tests.
- [ ] Run relevant shared `eggthreads` and terminal Egg tests for touched shared behavior.
- [ ] Verify tracked working tree clean except known unrelated untracked files.
- [ ] Summarize remaining intentional differences:
  - terminal-only `/displayMode`;
  - web-only `/theme` and browser layout behavior;
  - `/redraw` no-op in EggW unless future browser refresh behavior is desired.

Status notes:
- Pending.
