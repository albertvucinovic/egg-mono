# Display Verbosity TODO

Goal: add UI-only display verbosity levels for transcripts without changing stored messages, snapshots, provider context, tool execution, or compaction behavior.

Terminology note: keep the user-facing filename as `verbocity-todo.md` per request, but use the correctly spelled `verbosity` in code and command names.

## Streaming rule

Display verbosity primarily affects static/completed transcript display, not active streaming.

- `max` and `medium`: leave active streaming display as-is for now.
  - While reasoning/content/tool output/tool-call arguments are streaming, show the current live streaming UI.
  - After a message part finishes and becomes part of the static transcript, apply the selected verbosity rules to that completed/static display.
- `min`: active streaming should not show full live bodies.
  - Show animated short status text instead, such as `Reasoning streaming…`, `Content streaming…`, `Tool call streaming…`, or `Tool output streaming…`.
  - Once the part is complete/static, render according to `min`: user/assistant content plus hidden-detail summaries.
- This means the first implementation should be careful not to rewrite streaming internals except for the minimal `min` status-only display.

## Levels

### `max`

Current behavior exactly as-is.

- Full user/assistant/system/tool content.
- Full reasoning blocks.
- Full tool call argument panels/blocks.
- Full tool result/output panels/blocks.
- Current metadata/header detail, including full ids where currently shown.

### `medium`

Conversation remains complete, but noisy sections are collapsed to headers/previews.

Important rule: every collapsed/header-only item must keep all metadata currently shown in its header/title at `max` verbosity. Lower verbosity hides bodies, not header information.

- User and assistant message content remains full.
- Tool calls display as one-line entries under a `Tool Calls` header.
  - The `Tool Calls` header/title should include the same model, token, TPS, timestamp, and full message-id information it includes today.
  - Each tool-call row should include tool-call id suffix/full copyable id where currently available.
  - Include tool name.
  - Include a shortened one-line argument preview.
- Tool results display as header-only rows, one row per tool result.
  - Reuse the same header information as today's tool result panel: tool name, model, content tokens, TPS, timestamp, full message id, and tool-call id where available.
  - Include tool name.
  - Include content length/tokens when available.
  - Do not show full output body.
- Reasoning displays as one header-only row.
  - Reuse the same header information as today's reasoning panel: model, reasoning tokens, TPS, timestamp, and full message id.
  - Do not show reasoning body.
- System/command messages: keep visible for now; do not hide errors/command feedback.

### `min`

Conversation-first display.

- Show only user and assistant message content as full message bodies.
- Hide standalone system/tool message bodies from the transcript display unless they are important command/error messages.
- Between user/assistant messages, insert compact summary rows for hidden detail, e.g.:
  - `Hidden details: 12 tool calls, 12 tool results, 5 reasoning blocks.`
- Header information must remain available for hidden detail so a later expand-by-id flow is possible.
  - The summary row/card may be short, but it should expose the same header metadata the hidden items would show today: role/type, tool name, model, token/TPS metrics, timestamp, full message id, and tool-call id where applicable.
  - If many items are summarized, use compact header rows under the summary rather than showing bodies.
  - Do not delete or mutate the underlying history.

## Non-goals for first implementation

- No provider/API context changes.
- No snapshot/storage changes.
- No compaction behavior changes.
- No per-feature toggles yet.
- No web-only expandable detail UI in the first slice unless the base level rendering is already done.
- No broad rendering rewrite.

## Open design decisions

- Command name:
  - Preferred: `/displayVerbosity <max|medium|min>`.
  - Optional aliases later: `full=max`, `compact=medium`, `minimal=min`.
- Persistence:
  - First slice can be in-memory app state only.
  - Persist later only if there is already a nearby UI-preference mechanism.
- Scope of `system` messages in `min`:
  - Keep errors and command replies visible.
  - Hide ordinary system prompt messages from transcript display.
  - Decide whether skill/system-context messages get summarized or hidden.
- Expand flow:
  - First slice should preserve enough ids in headers/summaries.
  - Later slice can add `/showMessage <msg_id>` or web click-to-expand if wanted.

## Proposed implementation phases

### Phase 1 — Shared semantics and state

Status: Implemented terminal UI-only state and `/displayVerbosity <max|medium|min>` command. Default is `max`; no rendering, persistence, web UI, streaming, or expand-by-id behavior was changed.

Test notes: `pytest -q egg/tests/test_commands_display.py::TestCmdDisplayVerbosity eggthreads/tests/test_command_registry.py::test_display_input_commands_are_registered_handlers eggthreads/tests/test_command_registry.py::test_display_input_commands_change_app_state` (6 passed); `pytest -q egg/tests/test_commands_display.py eggthreads/tests/test_command_registry.py` (44 passed).

- Add a small display verbosity state to the terminal app:
  - Default: `max`.
  - Allowed values: `max`, `medium`, `min`.
- Add `/displayVerbosity <max|medium|min>` to display/input commands.
  - With no argument, show current level and usage.
  - On set, log/return user-facing text.
- Keep command behavior UI-only.

Likely files:

- `eggthreads/eggthreads/builtin_plugins/display_input.py`
- `egg/egg/app.py`
- command tests near `eggthreads/tests/test_command_registry.py` and/or `egg/tests/test_commands_display.py`

### Phase 2 — Terminal inline/live transcript formatting

Status: Implemented static transcript formatting in `FormattingMixin.format_messages_text(...)` for `max`, `medium`, and `min`. `max` preserves existing text output; `medium` collapses completed reasoning/tool-result bodies and shortens tool-call entries; `min` shows user/assistant bodies with hidden-detail summaries. Active streaming composition was left unchanged.

Test notes: `pytest -q egg/tests/test_formatting.py::TestFormatMessagesText` (7 passed); `pytest -q egg/tests/test_formatting.py` (24 passed).

Update `FormattingMixin.format_messages_text(...)`, which feeds the chat panel text, to respect the current display verbosity.

This phase is for static/completed transcript text. Do not compact active streaming output here except for the explicit `min` status-only streaming rule.

`max`:
- Preserve exact existing output.

`medium`:
- Reasoning: replace body with one header line.
- Tool calls: show one-line shortened entries.
- Tool outputs/results: show header-only rows.
- User/assistant content remains full.

`min`:
- Render full user and assistant content.
- Accumulate hidden reasoning/tool-call/tool-result counts between visible messages.
- Emit one compact hidden-detail summary row before the next visible user/assistant message, and at end if needed.

Likely files:

- `egg/egg/formatting.py`
- `egg/tests/test_formatting.py`

### Phase 3 — Terminal static console panels

Status: Implemented static console panel display verbosity. `max` preserves existing panel bodies; `medium` uses header-only reasoning/tool-result panels and one-line tool-call rows; `min` prints user/assistant content plus hidden-detail summary panels between visible messages. Active streaming panels/previews were not changed.

Test notes: `pytest -q egg/tests/test_panels.py::TestConsolePrintMessage` (14 passed); `pytest -q egg/tests/test_panels.py` (52 passed).

Update `PanelsMixin.console_print_message(...)`, which prints static transcript panels, to respect the same level.

Static console panels are already completed messages, so apply verbosity normally. Active streaming panels/previews should remain unchanged for `max`/`medium`.

`max`:
- Preserve exact existing panels.

`medium`:
- Reasoning panel becomes header-only.
- Tool Calls panel contains one-line shortened entries.
- Tool result panels become header-only.

`min`:
- Print only user/assistant content panels plus hidden-detail summary panels.
- Keep headers/ids in summary panels.

Likely files:

- `egg/egg/panels.py`
- `egg/tests/test_panels.py`

### Phase 4 — Web parity

Status: Implemented web display verbosity state, header selector, `/displayVerbosity <max|medium|min>` command response, static `ChatPanel` rendering for `max`/`medium`/`min`, and `min` status-only live streaming indicators. Did not add expand-by-id.

Test notes: `PYTHONPATH=.:eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests/test_api.py::TestCommands` (11 passed); `npx --prefix eggw/frontend tsc -p eggw/frontend/tsconfig.json --noEmit` (passed).

Add web-side display verbosity using the same level names.

- Store UI preference in frontend state, default `max`.
- Add a simple selector/control or slash command response for `/displayVerbosity` in eggw.
- Update `ChatPanel` rendering rules:
  - `max`: current behavior.
  - `medium`: reasoning header only, tool calls one-line, tool results header only.
  - `min`: user/assistant bodies plus hidden-detail summary rows.
- Streaming behavior:
  - `max`/`medium`: keep current streaming UI while content is actively streaming.
  - `min`: replace live streaming bodies with animated short status text (`Reasoning streaming…`, `Content streaming…`, etc.).
  - After streaming completes and messages are refetched/rendered as static transcript items, apply normal verbosity compaction.
- Keep ids/copy affordances visible in headers/summaries.

Likely files:

- `eggw/frontend/src/lib/store.ts`
- `eggw/frontend/src/components/ChatPanel.tsx`
- `eggw/eggw/commands/__init__.py` and/or `eggw/eggw/commands/utility.py`
- `eggw/tests/test_api.py` and frontend typecheck/tests

### Phase 5 — Expand-by-id follow-up

Only after levels work:

- Terminal: add a command such as `/showMessage <msg_id>` or `/expand <msg_id>`.
- Web: make collapsed headers clickable or add per-message expand state.
- Reuse stored message data; do not create a parallel detail store.

## Implementation notes

- Prefer local rendering helpers over a new abstraction unless the same exact summary/header logic is duplicated at least three times.
- Preserve full message ids in `max`; in lower levels, include enough id information for users to request expansion.
- Header/title metadata should not be reduced by lower verbosity. `medium` and `min` only hide or summarize bodies; headers keep the same information as `max`.
- Treat active streaming as separate from static transcript rendering. Compact only completed/static message parts, except that `min` uses status-only live streaming indicators.
- Errors, approval prompts, and command replies should remain visible at every level.
- Tool result summaries should distinguish:
  - assistant tool results (`role=tool`)
  - user-initiated command outputs stored as tool messages
- Keep tests focused on rendering differences, not full UI snapshots.

## Suggested first slice

Implement terminal-only first:

1. `/displayVerbosity max|medium|min`, default `max`.
2. `format_messages_text(...)` support for the three levels.
3. Focused tests for:
   - default `max` matches existing behavior for reasoning/tool output.
   - `medium` hides reasoning/tool-result bodies but keeps headers and ids.
   - `min` shows user/assistant bodies and a hidden-detail summary.

Then decide whether to do static panels or web parity next.
