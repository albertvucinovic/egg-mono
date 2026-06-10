# get_user_message_while_preserving_llm_turn TODO

## Goal

Add a model-callable tool named `get_user_message_while_preserving_llm_turn`.

The tool lets an LLM pause the current assistant/tool chain, show a user-facing assistant note, wait for the next real user message, and then return that user message as the tool result so the same LLM turn/tool chain can continue.

This complements `answer_user_while_preserving_llm_turn`: together they allow long or even unbounded interactive workflows without forcing every user-facing assistant note to end the underlying tool/LLM chain.

## Chosen name

Use `get_user_message_while_preserving_llm_turn`.

Rationale:
- parallel to `answer_user_while_preserving_llm_turn`;
- simpler than `get_next_user_message_tool_while_preserving_llm_turn`;
- explicit enough that model/tool traces are understandable.

## Current relevant behavior researched

- `answer_user_while_preserving_llm_turn` lives in `eggthreads/eggthreads/builtin_plugins/answer_user.py`.
  - It appends a visible assistant message with `answer_user_preserve_turn=True`.
  - It records `tool_call_id` and `source_tool_name` when called from a running tool stream.
- Tool execution is driven by RA2 in `eggthreads/eggthreads/runner.py`.
  - Assistant tool calls are auto-approved for names in `AUTO_APPROVED_TOOL_NAMES` / approval policies.
  - Tool functions can be async and context-aware through `ToolContext`.
  - Runner heartbeat keeps the lease alive while async tools await.
  - Tool results become `tool_call.finished`, then output approval, then a `role='tool'` message.
- `wait` completion lives in `eggthreads/eggthreads/api.py::wait_for_threads`.
  - It currently treats any non-expired open stream as unfinished/running.
  - It returns the last assistant message via `_last_assistant_content_from_snapshot`.
- Runnable state lives mostly in `eggthreads/eggthreads/tool_state.py`.
  - RA1 is triggered by provider-visible normal user messages and tool messages after the last LLM boundary.
  - `msg.edit` currently only affects skipped/deleted behavior for reducer purposes; ordinary edited flags such as `no_api` are not applied to RA1 scans.
- Snapshots apply normal `msg.edit` payloads, so marking a consumed user message with `no_api=True` / `keep_user_turn=True` via `msg.edit` will affect provider-context construction, but reducer/wait code also needs to learn that the message is consumed.

## Desired semantics / invariants

1. Tool schema:
   - name: `get_user_message_while_preserving_llm_turn`;
   - argument: `assistant_note: string`;
   - model-callable, not local-only.
2. On execution, the tool appends `assistant_note` as a visible assistant message.
   - Use existing assistant-note UI path by setting `answer_user_preserve_turn=True`.
   - Attach metadata such as `source_tool_name`, `tool_call_id`, and a new marker like `awaiting_user_message_tool_call_id` so wait/UI logic can identify this state.
   - Create/update snapshot after appending the note so `wait` can immediately return it.
3. While the tool is waiting for input, parent/manager `wait` must treat the thread as user-waiting, not running.
   - `wait_for_threads` should return `finished=True`, `state='waiting_user'`, and `last_assistant_message=<assistant_note>` while the active tool stream is specifically this waiting-for-user tool.
   - If a user message has already arrived after the note but the tool has not consumed it yet, prefer treating the thread as running rather than finished to avoid a false second “waiting user” result.
4. The tool waits indefinitely for the next normal user input.
   - No default timeout.
   - Poll the event log; rely on the runner heartbeat/lease while awaiting.
   - Respect cancellation/lease loss through `ctx.cancel_check` if available.
5. When the next normal user message arrives:
   - return that message content as the tool result;
   - mark the original user message as consumed by this tool, using a `msg.edit` payload such as:
     - `no_api=True`,
     - `keep_user_turn=True`,
     - `consumed_by_tool_call_id=<tool call id>`,
     - `consumed_by_tool_name='get_user_message_while_preserving_llm_turn'`.
   - Keep the user message visible in snapshots/UI, but prevent it from triggering a separate RA1 or being sent as an independent provider-visible user message.
6. Reducer/wait/provider-trigger logic must ignore consumed user messages for RA1/wait trigger purposes.
   - Do not use `skipped_on_continue`; that would hide the message from UI snapshots.
   - Teach reducer and uncached/public scans to recognize consumed message ids from `msg.edit`.
7. The tool result remains provider-visible as the assistant tool-call result.
   - This is how the current LLM turn receives the user’s input.
8. Preserve existing behavior for:
   - `answer_user_while_preserving_llm_turn`;
   - ordinary user messages;
   - user command tool calls (`$`, `$$`, REPL bridge);
   - `wait` for normal running child threads;
   - no persisted countdown summary events.

## Phase 1 — Tool registration and naming

- [x] Add/register `get_user_message_while_preserving_llm_turn` alongside `answer_user_while_preserving_llm_turn`.
- [x] Expose it in the default tool registry/tool schema with required `assistant_note`.
- [x] Add it to deterministic/constant auto-approval for safe control tools.
- [x] Tests:
  - default registry includes the new tool;
  - schema requires `assistant_note`;
  - approval policy / `AUTO_APPROVED_TOOL_NAMES` covers it.

## Phase 2 — Async tool implementation

- [x] Implement a context-aware async tool function.
- [x] Append the assistant note with preserve-turn metadata and create a snapshot.
- [x] Wait indefinitely for the next normal user `msg.create` after the note.
- [x] On cancellation/lease loss, return a structured interrupted result rather than hanging forever.
- [x] When input is found, mark it consumed with `msg.edit`, refresh snapshot, and return the input text.
- [x] Tests:
  - tool appends assistant note with metadata;
  - tool immediately returns a later user message in an async test;
  - consumed user message remains visible in snapshot but has consumed/no_api/keep_user_turn flags;
  - cancellation path does not hang.

## Phase 3 — Reducer and RA1 trigger semantics for consumed user messages

- [x] Add a small shared helper or local reducer logic to collect message ids consumed by `get_user_message_while_preserving_llm_turn` from `msg.edit` events.
- [x] Teach cached reducer RA1 scanning to ignore consumed messages.
- [x] Teach uncached/public RA1 scanning paths to ignore consumed messages where still used.
- [x] Teach wait trigger logic (`_latest_api_trigger_seq`) to ignore consumed messages.
- [x] Tests:
  - consumed user message does not trigger RA1 after tool result publication;
  - ordinary user message still triggers RA1;
  - consumed message is not hidden from snapshots.

## Phase 4 — `wait` semantics while the tool is actively waiting

- [ ] Add a narrow `wait_for_threads` special case for an active, non-expired open stream whose executing TC3 tool is `get_user_message_while_preserving_llm_turn` and whose assistant note has been appended.
- [ ] Return `finished=True`, `state='waiting_user'`, and the assistant note as `last_assistant_message` in this state.
- [ ] Do not report finished if a user reply already exists after the note and is not yet consumed.
- [ ] Tests:
  - `wait_for_threads(..., timeout_sec=0)` finishes for active waiting-user tool and returns the note;
  - normal active tool stream still returns unfinished/running;
  - after a user message arrives but before it is consumed, wait does not falsely report finished.

## Phase 5 — End-to-end runner behavior

- [ ] Add an end-to-end runner test with a fake LLM:
  1. user asks something;
  2. LLM emits `get_user_message_while_preserving_llm_turn({assistant_note: ...})`;
  3. tool appends note and waits;
  4. a user message is appended;
  5. tool returns that text as tool result;
  6. LLM receives the tool result and produces a follow-up assistant message.
- [ ] Verify the consumed user message does not trigger an extra independent RA1.
- [ ] Verify `wait` sees the assistant note while waiting.

## Phase 6 — Review and cleanup

- [ ] Keep changes minimal and local; no scheduler redesign unless Phase 3/4 proves impossible without it.
- [ ] Run focused tests first, then package tests that touch runner/tool/wait behavior.
- [ ] Update this TODO with status notes and commit hashes before each implementation commit.

## Status notes

- 2026-06-05: Plan created after researching `answer_user` plugin, `ToolContext`, RA2 execution, reducer RA1 scanning, snapshots, and `wait_for_threads`. Next: implement Phase 1–2 in a focused worker slice.
- 2026-06-10: Phase 1/2 implemented in commit `2b85cb7`: registered `get_user_message_while_preserving_llm_turn`, added deterministic auto-approval, implemented the async note/wait/consume tool path, and added direct registry/schema/approval/metadata/wait/consume/cancel tests. Phase 3/4 reducer and `wait_for_threads` semantics intentionally not implemented in this slice.
- 2026-06-10: Phase 3 implemented in a focused slice: reducer/public RA1 scans, `wait_for_threads` trigger completion, and `get_child_status` state reporting now ignore user messages consumed by `get_user_message_while_preserving_llm_turn` while keeping them visible in snapshots/UI. Phase 4 active waiting-tool wait semantics intentionally not implemented.
