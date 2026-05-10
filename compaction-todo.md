# Egg Compaction TODO

This file is the handoff document for implementing Egg thread compaction over many sessions.

It replaces the earlier summary-bundle/range-heavy sketch with a simpler core rule:

> A `thread.compaction` event sets the provider/API context start message for the same stable thread. The UI and raw event log still show the whole thread.

Compaction does not create a replacement thread, does not hide history from humans, and does not package summaries as magic metadata. If a summary exists, it is ordinary thread content. The compaction event only records where future model context starts.

## Session operating instructions

Every session working on this plan should:

1. Read this file first, then read `compaction.md` for background.
2. Run `git status --short` before editing.
3. Pick the next unchecked item in the earliest incomplete phase, unless the user explicitly asks for another item.
4. Keep each change small, local, and committable.
5. Preserve the stable `thread_id`; compaction must not create sibling/replacement threads.
6. Preserve UI/raw-history visibility. Humans should be able to scroll the whole thread and see message ids before compaction.
7. Keep provider/API compaction separate from UI/audit history. The API gets the compacted provider view; the UI gets the full raw/effective transcript.
8. Reuse existing tool/user-command machinery where possible, especially the `$` / `$$` distinction between model-visible and hidden command results.
9. Reuse the same compaction implementation for model tool calls, `/compact` user commands, and automatic compaction requests.
10. Run focused tests for the touched area.
11. After each committable change:
    - update this file with status, decisions, and test commands;
    - `git add` the changed files, including this file;
    - `git commit` the unit of work.
12. Do not batch unrelated phases into one commit.
13. Commit meaningful chunks: each commit should have one coherent purpose, a focused test result, and no unrelated cleanup. Prefer several small commits over one broad mixed commit.
14. If a task appears to require a broad refactor beyond the current phase, stop and ask the user before proceeding.

## Manual context handoff until compaction exists

Until this compaction system is implemented, use this file as the durable
handoff document between sessions. At the end of any session that worked on
compaction, add a short handoff note under the relevant phase before stopping.

Each handoff note should include:

- date/time;
- current phase and exact next unchecked task;
- files changed in the session;
- whether changes are committed or still uncommitted;
- focused tests run and their results;
- any known failures, incomplete edits, or design decisions;
- the recommended first command for the next session, usually `git status --short` plus a focused test or file to inspect.

Suggested handoff note template:

```text
Status notes:
- YYYY-MM-DD: Handoff — <short summary>.
  - Changed files: <paths>.
  - Commit: <hash> or uncommitted.
  - Tests: <commands and pass/fail>.
  - Next: <one concrete next action>.
  - Caveats: <anything the next session must know>.
```

If a session stops with uncommitted work, the next session should first run:

```bash
git status --short
git diff --stat
git diff -- <relevant paths>
```

Then either finish that in-progress unit or ask the user before changing direction.

## Core model

A thread remains one append-only raw event log and one stable orchestration identity:

```text
Thread W  # stable worker child id
  events/messages 1..1000        # old conversation, still visible in UI
  msg 1001                       # maybe a summary, maybe a normal prior user turn
  msg 1002..1010                 # maybe assistant/tool workflow
  thread.compaction(start=1001)  # provider/API context boundary
  events/messages 1012..         # continuation
```

After the compaction event, the provider/API context starts at the resolved start message:

```text
provider/API context = messages from start_msg_id onward, filtered by normal provider rules
UI/raw history       = all events/messages in the thread
```

So compaction is best understood as:

```text
set_provider_context_start(thread_id, start_msg_id)
```

not as:

```text
summarize range into magic summary object
create new thread
hide old messages from humans
```

## User-visible behavior

### UI behavior

- The UI should show the whole thread, including messages before compaction.
- The UI should show message ids so users can `/continue <message_id>` from before a compaction event.
- The UI may show compaction events as markers, but it should not truncate the visible transcript at those markers.
- This gives humans an "infinite scrollback" experience analogous to how the LLM can use REPL/decompaction tools for old context.

### Provider/API behavior

- Provider context uses the latest effective `thread.compaction` event to find the start message.
- Messages before the start message are not sent to the provider as normal context.
- Normal visibility rules still apply after the start message: hidden/local-only messages remain excluded from API context, tool protocol sanitization remains enforced, and provider-specific adapters may normalize message sequences as they do today.

### Failure/retry behavior

- No special abort event is required for MVP.
- If compaction fails before emitting `thread.compaction`, it is just a normal failed workflow in the thread; use `/continue <message_id>` to erase/retry if desired.
- If compaction succeeds but the boundary/summary is bad, `/continue <message_id_before_compaction>` should make that compaction event ineffective for provider-context purposes.

## Compaction tool

The core primitive is a default model-visible tool:

```text
compact_thread(start_message?)
```

The tool emits the `thread.compaction` event. Its only model-facing argument is the new start message selector.

### Start message selector

Supported selectors for both the tool and `/compact` command:

```text
<msg_id>       # explicit message id shown in the UI
last_user      # latest provider-visible user message in the effective thread
last_llm       # latest provider-visible assistant/LLM message in the effective thread
```

If the argument is omitted, it means:

```text
last_message   # latest provider-visible user or assistant/LLM message
```

Important rules:

- Do not add a special `this` selector for the MVP.
- For a model tool call with no argument, `last_message` may resolve to the assistant message containing the `compact_thread` tool call, once that message exists in the event log.
- `last_llm` means assistant/LLM message, not a tool result.
- Tool-role messages are not valid start messages in the MVP.
- Hidden/`no_api` messages are not valid start messages in the MVP.

### Examples

#### Summary-style full compaction

The assistant writes a normal summary message and calls the tool with no argument:

```text
assistant:
  Summary/start state:
  - ...

  tool_call: compact_thread()
```

The omitted selector resolves to the latest assistant/LLM message. Future provider context starts at that summary message. The summary is ordinary assistant content, not magic compaction metadata.

#### Tail-style compaction

A user or assistant chooses:

```text
compact_thread("last_user")
```

Future provider context starts at the latest user message, preserving that user turn and the following assistant/tool continuation as ordinary context. This replaces the earlier `/compact old` idea; the command name is `last_user`, not `old`.

#### Explicit boundary

A human sees a message id in the UI and runs:

```text
/compact msg_abc123
```

Future provider context starts at that message, if valid.

## `thread.compaction` event shape

The tool-facing argument stays minimal. The event may store derived metadata for audit and efficient lookup.

Target payload:

```json
{
  "type": "thread.compaction",
  "start_msg_id": "msg_...",
  "start_event_seq": 1001,
  "selector": "last_user | last_llm | last_message | msg_...",
  "created_by": "assistant_tool | user_command | auto_compaction",
  "tool_call_id": "call_...",
  "committed_from_msg_id": "msg_...",
  "created_at": "..."
}
```

Do not store the summary itself on the compaction event in the MVP. The summary, if any, is normal thread content at or after `start_msg_id`.

## Tool behavior

The compaction tool should behave like a normal tool when called by the LLM:

- It appends/emits `thread.compaction`.
- It returns a normal tool result such as `Compaction committed; provider context now starts at msg_...`.
- The tool result participates in the normal LLM tool-call protocol.
- The assistant may continue the turn after the tool result.

When invoked by the user command path, it should behave like other user commands:

- Use the same core compaction code.
- Preserve user-command turn semantics; do not automatically steal the next user turn.
- Show local command output/confirmation.

No-op/rejection cases should be handled inside the tool/core code and should produce clear results without emitting a compaction event:

- selector cannot be resolved;
- selected message does not exist, is deleted/skipped, hidden, or `no_api`;
- selected message is not a user or assistant/LLM message;
- selected start is at or before the current effective provider-context start, so compaction would not reduce context or would expand it;
- selected start would create an invalid provider tool-call sequence that cannot be sanitized safely;
- thread has no valid provider-visible user/assistant message for the requested selector.

## Commands and automatic compaction

### `/compact`

User command forms:

```text
/compact              # default: last_message
/compact <msg_id>
/compact last_user
/compact last_llm
```

The command should use the same core implementation as `compact_thread`.

### Automatic compaction

Automatic compaction should happen only when it is effectively the user's turn / at a normal turn boundary. It should be implemented similarly to user commands, not as an interleaved mutation inside an active assistant/tool turn.

Suggested first auto-compaction behavior:

1. Detect provider-context budget pressure at a safe user-turn boundary.
2. Append or run an automatic user-command-like compaction request.
3. Let the assistant produce an ordinary summary/start message if desired.
4. Have the assistant call `compact_thread()` or call the shared compaction core with an explicit selector.

Auto-compaction should reuse the same `compact_thread` core path and no-op validation.

## Scheduling assumption

No compaction-specific input gate is required for the MVP if normal thread scheduling already guarantees that messages sent to a child/thread while it is running are scheduled after the current active turn.

Required general invariant:

- External user/manager messages must not be interleaved into the middle of an active assistant/tool cycle.
- If a manager sends a worker a message while the worker is compacting, that message should appear after the current turn/tool workflow, and therefore naturally after any `thread.compaction` boundary emitted during that workflow.

If any append path violates this, fix the general scheduling behavior rather than adding a compaction-only queue.

## Effective compaction and `/continue`

The provider-context builder must not simply pick the latest raw `thread.compaction` event in the event log. It must pick the latest **effective** compaction event in the current thread view.

Required behavior:

- `/continue <message_id>` from before a compaction event should make that later compaction ineffective for provider context.
- UI/raw history can still show the old compaction event for audit.
- Provider context after continue should be based on the effective messages/events, not stale raw control events.

This likely requires extending or centralizing the thread "effective view" logic used by snapshots/provider context so non-message control events after a continue point can be ignored for current-context purposes.

## Provider context construction

Provider context after compaction should be built from:

```text
latest effective compaction start_event_seq
through current effective thread tail
```

not from:

```text
latest compaction event seq
through current tail
```

This distinction matters because the start message often appears before the compaction event itself:

```text
1001 assistant summary + compact_thread tool_call
1002 tool result
1003 assistant follow-up
1004 thread.compaction(start=1001)
```

The future provider view starts at `1001`, not `1004`.

Provider adapters may still need to sanitize/normalize the resulting messages to satisfy provider-specific protocol rules. Existing no-api, tool-call pairing, and strict-provider cleanup logic should remain authoritative.

## Phase 1 — Core compaction event and resolver

Goal: add the durable boundary event and selector resolution without changing provider context yet.

- [x] Add helper to resolve compaction start selectors.
  - Inputs: `thread_id`, optional selector string.
  - Supported selectors: omitted/`last_message`, `last_user`, `last_llm`, explicit `msg_id`.
  - Return resolved `msg_id` and `event_seq` or a clear error/no-op reason.
- [x] Add helper to append `thread.compaction`.
  - Suggested internal name: `commit_thread_compaction(db, thread_id, selector=None, *, created_by, tool_call_id=None, committed_from_msg_id=None)`.
  - The helper should resolve the selector, validate it, and append the event only if useful.
  - Do not store summary text in the event.
- [x] Add helper to list/latest compaction events.
  - Raw latest helper is useful for diagnostics.
  - Effective latest helper may wait until `/continue` handling is clear.
- [x] Add no-op validation.
  - Reject invalid/hidden/tool-role starts.
  - Reject starts at or before current effective start.
  - Reject provider-protocol-unsafe starts if existing sanitization cannot repair them.
- [x] Add focused tests.
  - Resolves omitted selector to latest user/assistant message.
  - Resolves `last_user` and `last_llm` correctly.
  - Resolves explicit `msg_id`.
  - Rejects hidden/no_api/tool messages.
  - Emits `thread.compaction` with start pointer metadata.
  - Does not change parent/child rows.
- [x] Commit.

Status notes:
- 2026-05-09: In progress, uncommitted. Added initial `thread.compaction` core helpers in `eggthreads/eggthreads/api.py`, exports, focused tests, and a `CompactionPlugin` skeleton with `compact_thread` tool plus `/compact` command registration. Basic no-op validation exists for missing/skipped/deleted/no_api/non-user-or-assistant/non-forward selectors; deeper provider-protocol-unsafe starts remain for Phase 10 hardening. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`.
- 2026-05-09: Committed in `de7281a` (`Add thread compaction start pointer`).

## Phase 2 — Provider-context start boundary

Goal: make API/provider context respect the latest effective compaction start pointer while UI/raw history remains full.

- [x] Add a provider-context event/message builder or filter.
  - Prefer a small shared builder over scattering compaction checks through runner code.
  - It should derive provider messages from the effective thread view and latest effective compaction event.
- [x] Apply compaction as a start pointer.
  - Include messages from `start_event_seq` onward.
  - Exclude earlier messages from normal provider context.
  - Continue applying `no_api` filtering and existing provider sanitization.
- [x] Preserve UI/raw snapshot behavior.
  - Do not truncate full UI history at the compaction start.
  - If existing `threads.snapshot_json` is both UI cache and provider source, split or filter provider view without destroying UI/audit history.
- [x] Add tests.
  - UI/raw snapshot or event listing still contains pre-compaction messages.
  - Provider input excludes messages before `start_event_seq`.
  - Provider input includes the selected start message and later messages.
  - Hidden/no_api messages after start remain excluded.
- [x] Commit.

Status notes:
- 2026-05-09: In progress, uncommitted. Added `filter_messages_for_compaction_provider_context(...)`, persisted `event_seq` in snapshot messages, and wired RA1 prompt building to filter snapshot messages before provider conversion. UI snapshot remains full. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`.
- 2026-05-09: Committed in `de7281a` (`Add thread compaction start pointer`).

## Phase 3 — Model-visible `compact_thread` tool

Goal: expose compaction as a normal default tool using the core helper.

- [x] Add `compact_thread` to the default tool registry.
  - One optional argument: `start_message`.
  - Tool description should explain accepted values: explicit msg id, `last_user`, `last_llm`, omitted = latest user/assistant message.
  - Tool description should say it sets future provider context start and does not delete history.
- [x] Implement model-tool behavior.
  - Normal RA2 tool call path.
  - Normal tool result visible to the LLM.
  - Assistant can continue after result.
- [x] Handle no-op/rejection cases with clear tool output and no event emission.
- [x] Add tests.
  - Tool emits `thread.compaction` for valid selector.
  - Tool returns no-op result for invalid selector.
  - Tool result participates in normal tool protocol.
  - Future provider context starts at resolved message.
- [x] Commit.

Status notes:
- 2026-05-09: In progress, uncommitted. Added built-in `CompactionPlugin` and registered the `compact_thread` tool. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`.
- 2026-05-09: Committed in `de7281a` (`Add thread compaction start pointer`).

## Phase 4 — `/compact` user command

Goal: expose manual user compaction using the same core code.

- [x] Add `/compact` command forms.
  - `/compact` -> omitted selector / `last_message`.
  - `/compact <msg_id>`.
  - `/compact last_user`.
  - `/compact last_llm`.
- [x] Reuse the same core compaction helper as the tool.
  - Do not implement parallel command-only compaction behavior.
- [x] Preserve user-command turn semantics.
  - Command output should be local/user-command style.
  - It should not unexpectedly trigger an assistant response by itself.
- [x] Add tests.
  - Command emits `thread.compaction` for all valid selector forms.
  - Command no-ops/rejects invalid selectors.
  - Command does not alter child relationships.
- [x] Commit.

Status notes:
- 2026-05-09: In progress, uncommitted. Added `/compact [msg_id|last_user|last_llm]` through `CompactionPlugin`, using the same core helper as the tool. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`.
- 2026-05-09: Committed in `de7281a` (`Add thread compaction start pointer`).

## Phase 5 — `/continue` and effective control events

Goal: make `/continue` able to erase/retry compaction in practice.

- [x] Define effective view for compaction events after continue.
  - A compaction event after the continue point should not affect provider context.
  - Raw UI/audit history can still show it.
- [x] Update provider-context builder to use effective latest compaction, not raw latest compaction.
- [x] Add tests.
  - Compact, then `/continue` from before compaction.
  - Provider context ignores the old compaction event.
  - UI/raw history still contains the old compaction event for audit.
  - Re-compaction after continue works.
- [x] Commit.

Status notes:
- 2026-05-09 21:32 UTC: Implemented effective compaction lookup for provider context. Later `control.interrupt` events with `purpose=continue` now erase non-message control events in the continued-away range for compaction purposes; raw `latest_thread_compaction(...)` remains available for diagnostics/audit, while provider filtering and selector forward checks use `latest_effective_thread_compaction(...)` / `current_effective_compaction_start_event_seq(...)`. Added focused tests for compact-then-continue, raw audit retention, and re-compaction after continue. Tests passed: `pytest -q eggthreads/tests/test_compaction.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 5 change.

## Phase 6 — LLM/system instructions

Goal: teach the model how and when to use the default compaction tool.

- [x] Add concise tool-use guidance to the relevant system/runtime prompt contribution.
  - Compaction does not delete history.
  - Use `compact_thread` when explicitly asked, during automatic compaction requests, or when context pressure makes a faithful start message appropriate.
  - If writing a summary, write it as normal assistant content and then call `compact_thread()` with omitted selector.
  - Use `last_user` when the goal is to keep the latest user turn and following continuation as the new start.
- [x] Avoid over-encouraging spontaneous compaction.
  - The model should not compact in the middle of substantive work unless requested or needed.
- [x] Add or update tests for tool schema/prompt contribution if applicable.
- [x] Commit.

Status notes:
- 2026-05-09 21:38 UTC: Added concise `compact_thread` tool-description guidance rather than a broader prompt refactor. The schema now states that compaction does not delete UI/raw history, should be used only on explicit request, automatic compaction request, or real context pressure, that summaries should be normal assistant content before an omitted-selector call, and that `last_user` keeps the latest user turn as the new start. Added focused schema guidance assertions. Tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 6 change.

## Phase 7 — Automatic compaction

Goal: implement threshold-triggered compaction using the same semantics as user/tool compaction.

- [x] Define threshold signal.
  - Trigger on provider-context token estimate, not raw UI/history size.
  - Use hysteresis to avoid immediate re-triggering.
- [x] Trigger only at user-turn/safe turn boundaries.
  - Do not interrupt active assistant/tool turns.
  - Reuse general scheduling semantics rather than a compaction-specific input gate.
- [x] Decide first auto behavior.
  - Option A: compact to `last_llm` or omitted `last_message` directly.
  - Option B: append an automatic compaction request asking the assistant to write a summary and call `compact_thread()`.
  - Prefer the simpler behavior first unless summary quality requires Option B.
- [x] Reuse `commit_thread_compaction` / `compact_thread` core path.
- [x] Add tests.
  - Threshold triggers at safe boundary.
  - No trigger below threshold.
  - Active turn defers compaction.
  - Auto compaction emits the same `thread.compaction` event shape.
- [x] Commit.

Status notes:
- 2026-05-09 21:58 UTC: Implemented the smallest Phase 7 behavior as direct threshold compaction to `last_llm` at the RA1 boundary. Added `provider_context_token_stats(...)` so the threshold uses effective provider context instead of raw UI history, `maybe_auto_compact_thread(...)` so auto compaction reuses `commit_thread_compaction`, and `RunnerConfig.auto_compact_threshold_tokens` checked only after acquiring the per-thread lease and before opening an LLM stream/provider call. Re-trigger hysteresis is provided by core forward-only validation: after compacting to the latest assistant, a repeated check without a newer assistant no-ops and emits no second event. Tests cover threshold trigger/no-trigger, no duplicate without new LLM, RA1-boundary provider view, deferral during tool turns, and provider-token counting after compaction. Tests passed: `pytest -q eggthreads/tests/test_compaction.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit eggthreads/tests/test_token_count_public.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 7 change.

## Phase 7.5 — Auto-compaction configuration and summary mode

Goal: make auto-compaction usable in normal Egg runs, prefer summary-producing compaction by default, and keep the direct start-pointer path easy to select.

### Decisions for implementation

- Summary mode is the default auto-compaction behavior.
- `EGG_COMPACT_SUMMARY` controls the auto-compaction mode:
  - unset/empty/default truthy => summary mode;
  - false-like values (`0`, `false`, `no`, `off`) => direct mode.
- Direct mode remains supported and easy to switch to, but should be a simple policy branch over the same core compaction primitive, not a parallel compaction implementation.
- Add a `thread.compaction_summary_in_progress` control event to prevent repeated automatic summary requests.
  - The pending marker should be considered effective only in the current effective thread view; `/continue` should be able to erase/retry it like other control events.
  - A later effective `thread.compaction` event should satisfy/clear the pending summary request for duplicate-prevention purposes.
- Do not add a human diagnostic command in this phase.

### Token threshold policy

- [x] Add thread-level compaction context length override event.
  - Suggested event type: `thread.compaction_context_length`.
  - Payload should include an integer token threshold and `created_by`/timestamp metadata.
  - The latest effective event for the current thread wins.
  - It must take priority above all other threshold sources.
  - Non-positive values should disable auto-compaction for that thread if this matches existing config style.
- [x] Add `EGG_AUTO_COMPACT_THRESHOLD_TOKENS` as fallback auto-compaction threshold.
  - Requested fallback/default value: `150000`.
  - Treat unset/empty as using the fallback/default.
  - Non-positive env value disables auto-compaction when no higher-priority source exists.
- [x] Derive a better default threshold from current model context metadata when available.
  - `max_tokens` in `models.json` is the context-window length.
  - Follow where `max_tokens` is stored in the model registry / `concrete_model_info`; if it is not preserved, add preservation there.
  - Use roughly 80% of the model context window as the preferred model-derived threshold.
  - Fall back to `EGG_AUTO_COMPACT_THRESHOLD_TOKENS` / `150000` when model metadata is missing.
- [x] Implement/test precedence.
  - Required order: latest effective thread override event > explicit `RunnerConfig.auto_compact_threshold_tokens` > model-derived 80% context window > env fallback/default.
  - Add focused tests for thread override, explicit config, model-derived threshold, env fallback/default, and disabling with non-positive values.

### Summary-producing manual compaction

- [x] Add `/compactWithSummary` user command.
  - It should append a normal model-visible request asking the assistant to write a concise continuation summary and then call `compact_thread()` with omitted `start_message`.
  - The summary must be normal assistant content, not stored as magic compaction metadata.
  - It should behave like other user commands: clear visible feedback to the user, scheduler starts/continues as needed, no silent failure.
  - Reuse existing message/tool scheduling and `compact_thread`; do not implement a parallel summary storage path.
- [x] Add tests for `/compactWithSummary` command behavior.
  - Command appends a normal request message.
  - Request includes clear instructions to call `compact_thread()` after writing summary.
  - Command logs/returns user-visible confirmation.

### Summary-producing automatic compaction

- [x] Add auto-summary mode for threshold compaction.
  - In summary mode, threshold pressure at a safe RA1/user-turn boundary appends an automatic compaction request instead of directly compacting to `last_llm`.
  - The request asks the assistant to write a concise continuation summary as normal assistant content and then call `compact_thread()` with omitted `start_message`.
  - The eventual `compact_thread` tool call emits the same `thread.compaction` start-pointer event.
  - Append a `thread.compaction_summary_in_progress` event alongside the automatic request so threshold checks do not repeatedly append summary requests while one is pending.
  - Avoid loops: if an effective pending summary-in-progress marker exists after the current effective compaction start, do not append another request.
- [x] Preserve direct mode.
  - When `EGG_COMPACT_SUMMARY` is false-like, keep the existing direct compaction behavior to `last_llm`.
  - Direct mode must still reuse `commit_thread_compaction` and all normal no-op validation.
- [x] Add focused tests.
  - Threshold in summary mode appends exactly one summary request and in-progress marker at a safe boundary.
  - Below threshold does not append.
  - Existing pending summary marker prevents duplicates.
  - Assistant summary + `compact_thread()` produces normal compaction boundary and allows future threshold checks to proceed only after new useful context.
  - `EGG_COMPACT_SUMMARY=0` uses direct mode.

Status notes:
- 2026-05-10: Design decisions recorded. Summary mode should be default via `EGG_COMPACT_SUMMARY` default true; direct mode remains env-selectable. Thread-level compaction context length override should be a latest-effective thread event and take priority over runner config, model-derived thresholds, and env/default thresholds. `/compactWithSummary` is required. Human diagnostic command is intentionally out of scope.
- 2026-05-10: Threshold resolver slice implemented. Added `thread.compaction_context_length` helpers, latest-effective override lookup with `/continue` erasure semantics, auto-compaction threshold resolver precedence (thread event > runner config > 80% model `max_tokens` > `EGG_AUTO_COMPACT_THRESHOLD_TOKENS` > 150000), and runner wiring so RA1 auto-compaction uses the resolver. Non-positive thread/config/env values disable at that source. Direct summary-mode work was intentionally not touched.
  - Changed files: `eggthreads/eggthreads/api.py`, `eggthreads/eggthreads/__init__.py`, `eggthreads/eggthreads/runner.py`, `eggthreads/tests/test_compaction.py`, `compaction-todo.md`.
  - Commit: this Threshold resolver slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py` passed; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit eggthreads/tests/test_token_count_public.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py` passed.
  - Next: implement the Summary mode auto-compaction slice (`EGG_COMPACT_SUMMARY` default true plus `thread.compaction_summary_in_progress` duplicate-prevention helpers).
  - Caveats: no summary mode, `/compactWithSummary`, diagnostics, child token reporting, or hardening changes were made.
- 2026-05-10: Summary mode auto-compaction slice implemented. Added `EGG_COMPACT_SUMMARY` default-true mode selection, `thread.compaction_summary_in_progress` raw/effective helpers with `/continue` erasure semantics, automatic summary request appending at the existing RA1 boundary, duplicate prevention while a pending marker is effective after the current compaction start, and direct-mode preservation via false-like env values (`0`, `false`, `no`, `off`) using the existing `commit_thread_compaction(..., selector="last_llm")` path. Summary mode now avoids immediate re-request loops after a satisfying compaction until new provider-visible user/assistant context appears.
  - Changed files: `eggthreads/eggthreads/api.py`, `eggthreads/eggthreads/__init__.py`, `eggthreads/eggthreads/runner.py`, `eggthreads/tests/test_compaction.py`, `compaction-todo.md`.
  - Commit: this Summary mode auto-compaction slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py` passed; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit eggthreads/tests/test_token_count_public.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py` passed.
  - Next: implement the Manual summary command slice (`/compactWithSummary`) using the same summary-request text and normal scheduling/message machinery.
  - Caveats: `/compactWithSummary`, child status token reporting, diagnostics, and provider-protocol hardening were intentionally not implemented.
- 2026-05-10: Manual summary command slice implemented. Added `/compactWithSummary`, reusing the shared summary request text/helper to append a normal model-visible user request that asks the assistant to write a concise continuation summary and then call `compact_thread()` with omitted `start_message`. The command rebuilds the snapshot, starts/returns the current-thread scheduler hook, and logs or returns a clear confirmation. It does not emit `thread.compaction` directly and does not use the automatic in-progress marker.
  - Changed files: `eggthreads/eggthreads/api.py`, `eggthreads/eggthreads/__init__.py`, `eggthreads/eggthreads/builtin_plugins/compaction.py`, `eggthreads/tests/test_compaction.py`, `compaction-todo.md`.
  - Commit: this Manual summary command slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_command_registry.py` passed; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_command_registry.py eggthreads/tests/test_plugin_tool_registry.py` passed.
  - Next: implement the Token/status reporting slice (Phase 9 suggested slice 4), keeping `context_tokens` as current provider/API context and adding `full_thread_tokens` where needed.
  - Caveats: child status token reporting, diagnostics, and provider-protocol hardening were intentionally not implemented.

## REPL thread context design

The REPL is the main decompaction surface. It should be hydrated automatically with a clear, consumer-friendly view of the whole effective thread context, not only the compacted-away part.

Design principles:

- Prefer self-explanatory names over internal implementation terms.
- Do not make the LLM learn Egg internals such as "effective compaction" before it can use old context.
- Do not advertise "provider-safe" in the REPL object names. Internally, the builder must still obey normal visibility rules and exclude hidden/local-only content, but the consumer-facing structure should feel like "the usable thread context".
- Include current prompt messages and older messages in the same context object so the LLM can retrieve exact current-context text too.
- Precompute common groupings so the LLM does not need boilerplate filtering for user/assistant/tool messages.
- Keep the event DB as source of truth. REPL hydration is a regenerated working copy/cache.

Target REPL variables:

```python
thread_context = {
    "thread": {
        "thread_id": "...",
        "loaded_at": "...",
        "loaded_through_event_seq": 1234,
        "message_count": 87,
        "visibility_note": "Contains the thread messages available for model use; hidden/local-only content is excluded.",
    },
    "how_to_use": "... short instructions ...",
    "all_messages": [...],                 # whole usable effective thread transcript
    "current_prompt_messages": [...],      # messages currently in the API prompt after compaction
    "older_messages_not_in_prompt": [...], # usable messages omitted from current prompt due to compaction
    "messages_by_id": {...},
    "messages_by_role": {
        "system": [...],
        "user": [...],
        "assistant": [...],
        "tool": [...],
    },
    "compactions": [
        {
            "marker_event_seq": 1004,
            "current_prompt_starts_at_msg_id": "msg_...",
            "current_prompt_starts_at_event_seq": 950,
            "selector_used": "last_llm",
            "created_by": "assistant_tool",
            "is_current": True,
        }
    ],
    "context_files": {
        "jsonl_path": "...",
        "markdown_path": "...",
    },
}
```

Convenience aliases should also be present:

```python
all_messages
current_prompt_messages
older_messages_not_in_prompt
messages_by_id
messages_by_role
system_messages
user_messages
assistant_messages
tool_messages
compactions
context_files
```

Helper functions should use simple names:

```python
search_thread(query, role=None, in_prompt=None)
get_message(msg_id)
print_message(msg_id)
reload_thread_context()
```

`compactions` should be an array because a thread may be compacted multiple times. Mark the current/active marker with `is_current=True` instead of using an ambiguous singular `compaction` or internal name like `effective_compaction`.

Freshness policy:

- Hydrate on first REPL use for compacted threads, and ideally for all threads if cheap.
- Store `loaded_through_event_seq`.
- Before REPL eval, cheaply compare the current max event seq. If stale, rebuild for correctness in the MVP.
- Later optimization may incrementally append simple `msg.create` tails, but correctness should come first.

File-backed context:

- Write JSONL and Markdown copies for grep/read workflows.
- Do not print the full context automatically.
- The files are a cache and can be regenerated.

## Phase 8 — REPL thread context hydration

Goal: make the hydrated REPL context the primary way for the LLM to inspect full thread context, including both current prompt messages and older messages omitted by compaction.

- [x] Remove redundant model-visible compaction source/status tools.
  - Remove default registrations for `show_compaction_start`, `search_compaction_sources`, and `fetch_compaction_source`.
  - Keep only `compact_thread` as the compaction-specific model-visible tool.
  - Remove/update tests that expect those tools in generated `eggtools` wrappers.
  - Keep internal helper code only if immediately reused by REPL hydration; otherwise remove it to avoid parallel retrieval systems.
- [x] Add a consumer-friendly REPL context builder.
  - Suggested internal name: `build_repl_thread_context(db, thread_id)`.
  - Return `thread_context` with `all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, `messages_by_id`, `messages_by_role`, `compactions`, `context_files`, and `how_to_use`.
  - Group by role and expose message ids/event seqs/timestamps/content.
  - Internally obey normal visibility rules: hidden/local-only content is excluded, deleted/skipped messages are excluded, and tool output is sanitized consistently with API context.
  - Do not use internal-facing names like `effective_compaction` in the consumer-visible structure.
- [x] Add REPL hydration for compacted threads, and for all threads if cheap enough.
  - Inject `thread_context` plus convenience aliases into Python REPL state.
  - Inject helper functions: `search_thread`, `get_message`, `print_message`, `reload_thread_context`.
  - Rebuild if stale based on max event seq; optimize later only if needed.
  - Write JSONL/Markdown cache files and expose their paths in `context_files`.
- [x] Add instructions for using hydrated REPL context.
  - Tell the model to use `python_repl` for exact transcript details when needed.
  - Mention the obvious variable/function names, not implementation internals.
  - Do not overemphasize provider-safety language to the LLM; say hidden/local-only content is excluded.
- [x] Add tests.
  - Hydrated context includes visible old and current messages.
  - `older_messages_not_in_prompt` contains pre-start messages after compaction.
  - `current_prompt_messages` matches current compacted API context.
  - `messages_by_role` and aliases include user/assistant/tool groupings.
  - Hidden/`no_api` content is excluded.
  - `/continue` before compaction removes continued-away messages/markers from the hydrated current view.
- [x] Commit.

Status notes:
- 2026-05-09 23:30 UTC: Added automatic Python REPL hydration using `build_repl_thread_context`. In-memory Python REPL evals now refresh `thread_context` when the caller thread max event seq changes, install convenience aliases (`all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, role group aliases, `compactions`, `context_files`), and inject `search_thread`, `get_message`, `print_message`, and `reload_thread_context`. Hydration writes regenerated JSONL/Markdown cache files under `.egg_thread_context/<thread_id>/` and exposes their paths; Docker REPL evals receive the same hydrated context in their request payload with cache paths mapped to the workspace when possible. The `python_repl` tool description now points models to the hydrated variables/functions for exact transcript details and says hidden/local-only content is excluded. Focused tests added for schema guidance, in-memory hydration aliases/helpers/files, and stale max-event-seq rebuilds. Tests passed: `pytest -q eggthreads/tests/test_python_repl_tool.py`; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_python_repl_tool.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 8 REPL hydration change. Next: Phase 9 UI compaction markers and token/status reporting.
- 2026-05-09 23:25 UTC: Added the small consumer-friendly REPL context builder slice only. `build_repl_thread_context(db, thread_id)` now returns full usable effective transcript data, current compacted prompt messages, older usable messages omitted by compaction, `messages_by_id`, `messages_by_role`, effective compaction marker summaries with `is_current`, empty/deferred `context_files`, and `how_to_use`; it excludes hidden/no_api and continued-away messages and reuses provider compaction filtering/sanitization for current prompt content. No automatic REPL hydration, aliases, helper injection, or context files were wired in this slice. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py`; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py`. Commit: this Phase 8 builder change. Next: add REPL hydration/injected aliases/helpers as a separate slice.
- 2026-05-09 23:07 UTC: Removed redundant model-visible compaction source/status tools per the revised Phase 8 plan. `CompactionPlugin` now registers only `compact_thread`; local API/source helper code for `show_compaction_start`, `search_compaction_sources`, and `fetch_compaction_source` was removed instead of kept as a parallel retrieval surface; public exports were cleaned up; focused tests were updated to assert generated `eggtools` wrappers include `compact_thread` and exclude the removed helpers. No REPL hydration was implemented. Tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 8 removal change.
- 2026-05-09: Design revision after initial Phase 8 slices: model-visible `show_compaction_start`, `search_compaction_sources`, and `fetch_compaction_source` are now considered redundant with the desired hydrated REPL context and should be removed from default tools. Historical notes below describe already-committed work that should be superseded by the new plan.
- 2026-05-09: Added system-prompt guidance for hydrated REPL thread context and fixed generic command dispatch to log non-empty `CommandResult.message`, while keeping `/compact` from double-logging through both its handler and generic dispatch. Focused tests passed: `pytest -q egg/tests/test_integration_workflow.py::TestCommandRegistryDispatch eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`; `python -m compileall -q egg/egg eggthreads/eggthreads`. Commit: `29ed196` (`Document REPL context and log command results`).
- 2026-05-09 21:52 UTC: First slice only. Added read-only `show_compaction_start(...)` helper plus model-visible `show_compaction_start` tool. It reports raw compaction count/latest raw marker, latest effective compaction marker, start message id/event seq, selector/created_by, and a bounded start-message preview. It does not fetch/search old pre-compaction history and does not mutate the thread. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 8 first-slice change.
- 2026-05-09 22:00 UTC: Added model-visible pre-start source exploration helpers `search_compaction_sources(...)` and `fetch_compaction_source(...)`, plus default tools of the same names. They search/fetch only the latest effective compaction's pre-start history, rebuild from the effective snapshot so `/continue` skips still apply, skip `no_api`/hidden content by default (including hidden `$$` command/tool messages), bound result counts and returned characters, and apply terminal-safety plus provider-style secret masking before returning content. No REPL hydration workspace or audit event was added in this slice. Focused tests passed: `pytest -q eggthreads/tests/test_compaction.py`; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`. Commit: this Phase 8 source-search/fetch slice.
  - Next: Phase 8 REPL exposure/hydration should be a separate small slice, likely first adding focused tests that generated `eggtools` wrappers expose `show_compaction_start`, `search_compaction_sources`, and `fetch_compaction_source` without hydrating raw hidden content; defer any durable REPL cache/workspace or audit logging to later commits.
  - 2026-05-09 22:10 UTC: Added the small REPL exposure test slice. Focused tests now assert generated `eggtools` wrappers expose `show_compaction_start`, `search_compaction_sources`, and `fetch_compaction_source`, and an in-memory REPL can call those wrappers without returning hidden/no_api pre-start content. Fixed the compaction tool wrapper path to reopen the DB when context-aware compaction tools execute in a worker thread, matching the existing execution-tool DB pattern. No durable REPL cache/workspace hydration and no source-access audit logging were added in this slice. Focused tests passed: `pytest -q eggthreads/tests/test_repl_dynamic_tool_wrappers.py`; `pytest -q eggthreads/tests/test_repl_dynamic_tool_wrappers.py eggthreads/tests/test_compaction.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py`; `python -m compileall -q eggthreads/eggthreads`. Commit: this Phase 8 REPL exposure test slice.
  - Next: Phase 8 still has durable REPL hydration helpers and optional audit/logging unchecked; treat those as separate manager-approved slices because they are broader than wrapper exposure tests.

## Phase 9 — UI compaction markers and status reporting

Goal: make compaction visible to humans without hiding history, and report token/status values accurately. Do not add `/compactionStatus` or other compaction-specific human diagnostics commands.

- [x] Draw visual compaction borders in the UI transcript.
  - The UI should still show all messages before and after compaction.
  - Render `thread.compaction` markers as a clear horizontal divider, for example a red horizontal line or similarly visible boundary.
  - Include concise text near the divider such as `Compaction boundary: API context now starts at msg_...`.
- [x] Update token/context reporting names and values.
  - `context_tokens` should mean the current API/provider context length after compaction.
  - `full_thread_tokens` should mean all visible/effective thread history length before compaction filtering.
  - Update `/cost`, diagnostics, status panels, and any API/status output that currently reports only one ambiguous token count.
  - Preserve backward compatibility carefully where external callers may already read `context_tokens`; after this change, it should intentionally mean current provider context.
- [x] Update child status tools with both token counts and compaction info.
  - `get_child_status` should report `context_tokens` for current after-compaction context.
  - It should also report `full_thread_tokens`.
  - Include concise compaction info when present: compacted boolean, current prompt start msg id/event seq, marker event seq, and raw marker count if cheap.
- [x] Ensure message ids are visible/copyable enough for `/compact <msg_id>` and `/continue <msg_id>` workflows.
- [x] Commit.

Status notes:
- 2026-05-09 23:46 UTC: Added the smallest visual marker slice. The terminal transcript formatter now inserts a clear compaction divider before the selected start message while preserving all snapshot messages; static console rendering prints the divider as a red `Compaction Boundary` panel, and the live event watcher notices `thread.compaction` control events so markers appear during active UI watching. The web message API now injects read-only `compaction_marker` transcript items before the start message, and the React chat panel renders them as red horizontal labeled dividers. Existing message-id display/copy behavior was not changed in this slice. Focused tests passed: `pytest -q egg/tests/test_formatting.py egg/tests/test_panels.py eggthreads/tests/test_command_registry.py`; `PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests/test_api.py::TestMessageOperations`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py`; `cd eggw/frontend && npx tsc --noEmit --pretty false`. Commit: this Phase 9 visual-marker slice. Next: verify/improve full message-id copyability, then continue token/status reporting; do not add a compaction diagnostics command.
- 2026-05-10: Token/status reporting slice implemented. Added `thread_token_stats(...)` so status surfaces can report `context_tokens` as the current effective provider/API context after compaction while preserving `full_thread_tokens` for full visible/effective history before compaction filtering. Updated terminal `/cost`, context-limit checks, web `/cost`, web token stats, and child status reporting to use the provider-context value; `get_child_status` now includes `full_thread_tokens` plus concise nested compaction info (`compacted`, current prompt start msg id/event seq, marker event seq, raw marker count). No `/compactionStatus` or compaction-specific diagnostic command was added.
  - Changed files: `egg/egg/formatting.py`, `egg/egg/panels.py`, `egg/tests/test_formatting.py`, `eggthreads/eggthreads/__init__.py`, `eggthreads/eggthreads/api.py`, `eggthreads/eggthreads/builtin_plugins/diagnostics.py`, `eggthreads/eggthreads/builtin_plugins/subagents.py`, `eggthreads/eggthreads/runner.py`, `eggthreads/eggthreads/token_count.py`, `eggthreads/tests/test_child_status.py`, `eggthreads/tests/test_compaction.py`, `eggthreads/tests/test_scheduler_slots.py`, `eggthreads/tests/test_token_count_public.py`, `eggw/eggw/commands/utility.py`, `eggw/eggw/models.py`, `eggw/eggw/routes/stats.py`, `eggw/frontend/src/components/SystemPanel.tsx`, `eggw/tests/test_api.py`.
  - Commit: this Token/status reporting slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_child_status.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit eggthreads/tests/test_command_registry.py eggthreads/tests/test_plugin_tool_registry.py egg/tests/test_formatting.py::TestCurrentTokenStats egg/tests/test_commands_utility.py::TestCmdCost` passed; `python -m compileall -q eggthreads/eggthreads egg/egg eggw/eggw && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_child_status.py eggthreads/tests/test_scheduler_slots.py::TestContextLimit eggthreads/tests/test_token_count_public.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py egg/tests/test_formatting.py::TestCurrentTokenStats egg/tests/test_commands_utility.py::TestCmdCost` passed (114 tests); `PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests/test_api.py::TestTokenStats::test_get_stats` passed; `cd eggw/frontend && npx tsc --noEmit --pretty false` passed.
  - Next: finish the remaining Phase 9 message-id visibility/copyability check, or proceed to the Phase 10 hardening slice if no UI copyability change is needed.
  - Caveats: provider-protocol hardening and unrelated cleanup were intentionally not implemented.
- 2026-05-10: Message-id visibility/copyability check completed. Verified existing Phase 9 marker tests for terminal and web, and confirmed the web UI already copied full message ids from the message `id` field. Made the minimal remaining UI improvement so full `msg_id` values are directly visible in terminal transcript text/static panel titles and in the web message header while retaining click-to-copy behavior. Added focused tests that terminal formatting/static panels and the web message API expose full ids for `/compact <msg_id>` and `/continue <msg_id>` workflows. No `/compactionStatus`, diagnostics command, Phase 10 hardening, or unrelated UI cleanup was added.
  - Changed files: `egg/egg/formatting.py`, `egg/egg/panels.py`, `egg/tests/test_formatting.py`, `egg/tests/test_panels.py`, `eggw/frontend/src/components/ChatPanel.tsx`, `eggw/tests/test_api.py`, `compaction-todo.md`.
  - Commit: this Message-id visibility/copyability slice change.
  - Tests: `pytest -q egg/tests/test_formatting.py::TestFormatMessagesText egg/tests/test_panels.py::TestPrintStaticViewCurrent` passed; `PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests/test_api.py::TestMessageOperations` passed; `cd eggw/frontend && npx tsc --noEmit --pretty false` passed; `python -m compileall -q egg/egg eggw/eggw && pytest -q egg/tests/test_formatting.py::TestFormatMessagesText egg/tests/test_panels.py::TestPrintStaticViewCurrent eggthreads/tests/test_compaction.py eggthreads/tests/test_command_registry.py && PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests/test_api.py::TestMessageOperations` passed.
  - Next: start Phase 10 hardening slice, beginning with provider protocol edge cases around assistant/tool-call compaction starts.
  - Caveats: terminal live/static headers now show full message ids, which is intentionally more verbose to support copy/paste workflows.

## Phase 10 — Hardening and cleanup

Goal: reduce edge-case risk after the core path works.

- [x] Review provider protocol edge cases.
  - Starting at assistant messages with tool calls.
  - Starting after/before tool result blocks.
  - Strict providers that dislike assistant-first transcripts.
- [x] Review interactions with message edits/deletes/skips.
  - If the start message is later deleted/skipped, provider context should fall back safely or ignore that compaction event.
- [x] Review token accounting.
  - UI may show full historical tokens.
  - Provider-context estimates should reflect compaction start.
- [x] Add invariant tests where cheap.
  - Compaction never changes parent/child rows.
  - Provider context never includes `no_api` messages because of compaction.
  - `/continue` before compaction invalidates the compaction for provider view.
- [x] Remove temporary compatibility code only after tests cover the new path.
- [x] Commit.

Status notes:
- 2026-05-10: Provider protocol hardening slice implemented. Inspected `ThreadRunner._sanitize_messages_for_api` / `_enforce_assistant_toolcall_protocol` and added compaction-time validation for unsafe provider tool-call boundaries: assistant `tool_calls` starts are accepted only when immediately followed by all matching visible tool results, and starts inside an assistant/tool result block are treated as unsafe for any future selector that might allow tool-role starts. Plain assistant summary compaction without tool calls remains allowed. Added cheap fallback tests confirming compaction markers are ignored if their start message is later deleted or skipped via `/continue`. No token/status/UI, diagnostics, source exploration, or broader hardening work was included.
  - Changed files: `eggthreads/eggthreads/api.py`, `eggthreads/tests/test_compaction.py`, `compaction-todo.md`.
  - Commit: this Provider protocol hardening slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py` passed; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_snapshot_builder.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_plugin_tool_registry.py eggthreads/tests/test_command_registry.py` passed.
  - Next: continue Phase 10 with a separate small invariant-test slice (parent/child rows unchanged, no_api never included because of compaction, and `/continue` invalidation invariants) or review token-accounting edge cases if needed.
  - Caveats: strict-provider assistant-first policy remains an open design question; this slice only rejects tool-call protocol starts that the existing sanitizer would have to drop or could not preserve safely.
- 2026-05-10: Token-accounting/invariant slice completed. Reviewed the existing token-counting path and verified `provider_context_token_stats(...)` applies `filter_messages_for_compaction_provider_context(...)`, while `thread_token_stats(...)` keeps `context_tokens` as current provider/API context and `full_thread_tokens` as full visible/effective history. Added cheap invariants that compaction does not mutate `children` rows, provider sanitization after compaction excludes `no_api` messages, `/continue` invalidates compaction for provider view (covered by existing tests), and token stats line up with provider/full helpers. No compatibility code was clearly obsolete, so no code was removed.
  - Changed files: `eggthreads/tests/test_compaction.py`, `compaction-todo.md`.
  - Commit: this Token-accounting/invariant slice change.
  - Tests: `pytest -q eggthreads/tests/test_compaction.py` passed; `python -m compileall -q eggthreads/eggthreads && pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_token_count_public.py eggthreads/tests/test_continue_thread.py eggthreads/tests/test_snapshot_builder.py` passed.
  - Next: Phase 10 is complete enough for MVP; recommended next task is a final focused full compaction regression run or manager review for any remaining open design question.
  - Caveats: no broad refactor, diagnostics, UI/status changes, source exploration, or compatibility-code removal was done.

## Open design questions

Resolve these only when implementation pressure makes them concrete:

- Should explicit `<msg_id>` allow only user/assistant messages forever, or should advanced/debug mode allow system/tool messages with provider sanitization?
- How should strict providers handle a compacted context that starts with an assistant/LLM message?
- How visible should compaction tool results be in the UI/provider context after the boundary?
- Should any source exploration remain as a user command, or should hydrated REPL context be the only decompaction surface besides UI scrollback?

Resolved decisions now captured in the phase plan:

- Omitted selector is accepted for `/compact` as well as the tool.
- Automatic compaction should use summary mode by default, controlled by `EGG_COMPACT_SUMMARY` default true; direct mode remains available when false-like.
- Model `max_tokens` is the context-window length for model-derived threshold calculation.
- Latest effective thread-level compaction context length event takes priority over runner, model, env, and default thresholds.
- Non-message compaction control events should use the same effective-view/`/continue` semantics as `thread.compaction`.
- REPL hydration now runs as the primary decompaction surface; UI compaction markers are already implemented.

## Suggested next implementation slices

Use focused worker slices, in this order:

1. **Threshold resolver slice**
   - Add the thread-level compaction context length event helpers.
   - Resolve threshold precedence: thread event > explicit runner config > 80% of model `max_tokens` > `EGG_AUTO_COMPACT_THRESHOLD_TOKENS` > 150000.
   - Wire the runner to use the resolver instead of only `RunnerConfig.auto_compact_threshold_tokens`.
   - Add focused tests.

2. **Summary mode auto-compaction slice**
   - Add `EGG_COMPACT_SUMMARY` default true.
   - Add `thread.compaction_summary_in_progress` duplicate-prevention helpers.
   - In summary mode, append one automatic summary request plus in-progress marker at the RA1 boundary instead of direct compaction.
   - Preserve direct mode when `EGG_COMPACT_SUMMARY` is false-like.
   - Add focused tests.

3. **Manual summary command slice**
   - Add `/compactWithSummary` using the same summary-request text and normal scheduling/message machinery.
   - Add command tests.

4. **Token/status reporting slice**
   - Update status surfaces so `context_tokens` means current provider/API context after compaction and `full_thread_tokens` means full visible/effective history.
   - Update `get_child_status` with `context_tokens`, `full_thread_tokens`, and nested compaction info.
   - Do not add `/compactionStatus`.

5. **Hardening slice**
   - Review provider protocol edge cases, especially assistant/tool-call starts.
   - Ensure deleted/skipped start messages cause provider context to fall back safely.
   - Add cheap invariant tests.
