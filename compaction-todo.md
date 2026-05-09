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
- Normal provider filtering still applies after the start message: `no_api` messages remain excluded, tool protocol sanitization remains enforced, and provider-specific adapters may normalize message sequences as they do today.

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

## Phase 0 — Baseline and current invariants

Goal: document the existing behavior compaction will reuse.

- [ ] Confirm current user-command behavior.
  - Visible `$` command results can become model-visible context.
  - Hidden `$$` command results are marked `no_api` and excluded from providers.
  - User-command results use existing turn-preservation behavior.
  - Suggested tests: `pytest -q eggthreads/tests/test_commands_tools.py eggthreads/tests/test_tool_message_format.py eggthreads/tests/test_snapshot_builder.py`.
- [ ] Confirm manager/child message scheduling.
  - Messages to a running child should not interleave into an active assistant/tool turn.
  - If this is not currently guaranteed, record the gap before implementing compaction.
- [ ] Identify provider-context construction path.
  - Current likely owner: `ThreadRunner._run_ra1_llm` plus `_sanitize_messages_for_api`.
- [ ] Identify `/continue` effective-view behavior.
  - Determine whether non-message events after the continue point are currently ignored or still visible to context builders.
- [ ] Commit baseline notes/test coverage updates.

Status notes:
- 2026-05-09: Rewritten plan around `thread.compaction` as provider-context start pointer. No implementation yet.

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

## Phase 8 — Decompaction/source exploration tools

Goal: let the LLM recover older details without sending the full old transcript by default.

- [ ] Add `show_compaction_start` or equivalent helper if useful.
  - It can report the latest compaction start message and marker.
- [ ] Add source search/fetch helpers over pre-start history.
  - Must skip `no_api`/hidden content by default.
  - Must bound output sizes.
  - Must apply existing secret masking/tool-output policy.
- [ ] Add REPL hydration helpers for compacted threads.
  - Expose compaction markers and old-source search/fetch functions.
  - Keep durable source in the event log; REPL is only a cache/workspace.
- [ ] Add audit/logging for source access if needed.
- [ ] Add tests.
  - Hidden `$$` output is not returned by model-visible search.
  - Visible old content before compaction can be found/fetched within bounds.
- [ ] Commit.

Status notes:
- Not started.

## Phase 9 — UI/status and diagnostics

Goal: make compaction visible and debuggable without hiding history.

- [ ] Show compaction markers in UI or diagnostics.
  - Include start message id and event seq.
  - Make clear that old messages are still present in raw history.
- [ ] Add read-only compaction status command or extend thread diagnostics.
  - Latest effective compaction start.
  - Raw compaction history.
  - Provider-context token estimate if available.
- [ ] Ensure message ids are visible/copyable enough for `/compact <msg_id>` and `/continue <msg_id>` workflows.
- [ ] Commit.

Status notes:
- Not started.

## Phase 10 — Hardening and cleanup

Goal: reduce edge-case risk after the core path works.

- [ ] Review provider protocol edge cases.
  - Starting at assistant messages with tool calls.
  - Starting after/before tool result blocks.
  - Strict providers that dislike assistant-first transcripts.
- [ ] Review interactions with message edits/deletes/skips.
  - If the start message is later deleted/skipped, provider context should fall back safely or ignore that compaction event.
- [ ] Review token accounting.
  - UI may show full historical tokens.
  - Provider-context estimates should reflect compaction start.
- [ ] Add invariant tests where cheap.
  - Compaction never changes parent/child rows.
  - Provider context never includes `no_api` messages because of compaction.
  - `/continue` before compaction invalidates the compaction for provider view.
- [ ] Remove temporary compatibility code only after tests cover the new path.
- [ ] Commit.

Status notes:
- Not started.

## Open design questions

Resolve these only when implementation pressure makes them concrete:

- Should omitted selector be accepted for `/compact` as well as the tool? Current plan says yes.
- Should explicit `<msg_id>` allow only user/assistant messages forever, or should advanced/debug mode allow system/tool messages with provider sanitization?
- How should strict providers handle a compacted context that starts with an assistant/LLM message?
- Should automatic compaction directly choose `last_llm`/`last_message`, or should it ask the assistant to write a summary and call the tool?
- What is the cleanest way to make non-message control events ineffective after `/continue`?
- How visible should compaction tool results be in the UI/provider context after the boundary?
- Should source exploration tools search only before the latest start message, or the whole raw event log with markers?

## Suggested first implementation slice

The smallest useful slice is:

1. Add selector resolution and `thread.compaction(start_msg_id, start_event_seq)` event append helper.
2. Add provider-context filtering from latest effective start pointer while leaving UI/raw history unchanged.
3. Add `compact_thread(start_message?)` default tool using the helper.
4. Add `/compact [selector]` command using the same helper.
5. Add tests for `/continue` before a compaction event invalidating it for provider context.

Do not start with tree-like threads, source bundles, or automatic compaction. The start-pointer boundary must be correct first.
