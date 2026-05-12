# Summary-after-compaction TODO

Goal: change automatic summary compaction so Egg commits the compaction boundary first, then asks the newly compacted context to create the continuation summary using hydrated REPL/thread-history helpers.

## Desired flow

1. When a compaction criterion is met:
   - provider context token threshold reached, or
   - provider/model error indicates context length exceeded.
2. Resolve the nearest protocol-safe compaction boundary.
   - Preserve existing assistant/tool-call/tool-result boundary rules.
   - Do not compact into the middle of a pending or malformed tool exchange.
3. Append the existing `thread.compaction` event immediately.
4. In the new compacted provider context, append an automatic summary-creation user message.
5. Run that summary turn before any other queued normal messages.
6. After the summary turn finishes, resume processing the queued messages normally.

## New automatic summary prompt

Replace the current auto-summary instruction that asks the assistant to call `compact_thread()` after writing the summary.

The new prompt should say that compaction has already happened, and instruct the assistant to use hydrated REPL history tools, for example:

- `all_messages`
- `current_prompt_messages`
- `older_messages_not_in_prompt`
- `messages_by_id`
- `messages_by_role`
- `search_thread(...)`
- `get_message(...)`
- `print_message(...)`
- `reload_thread_context()`

The prompt should ask for a concise continuation summary preserving:

- pending user request,
- important decisions and design constraints,
- files changed or intended to change,
- commands/tests already run and their results,
- known failures or unresolved risks,
- exact next steps.

It should also say not to continue the user task yet; only write the continuation summary.

## Important behavior changes

- Auto compaction summary mode becomes: `commit compaction -> request summary`.
- It is no longer: `request summary -> assistant summary -> compact_thread()`.
- The auto-summary request should not require the assistant to call `compact_thread()`.
- The post-compaction summary turn must be first in the new provider context.
- Queued user messages/tool continuations should wait until the summary turn completes.

## Why this is better

- Egg can use nearly the whole context window before compacting, instead of reserving room for a summary turn.
- Summary generation happens in a fresh compacted provider context, so context-limit failures during summary creation are much less likely.
- The newly compacted thread is explicitly introduced to REPL history helpers and can inspect omitted history hands-on.
- Context-length-exceeded provider errors can be handled by committing a boundary and retrying with the summary request in the new context.

## Implementation notes to check later

- `eggthreads/eggthreads/api.py`
  - `COMPACTION_SUMMARY_REQUEST` / `AUTO_COMPACTION_SUMMARY_REQUEST`
  - `maybe_auto_compact_thread(...)`
  - `append_auto_compaction_summary_request(...)`
  - `thread.compaction_summary_in_progress` handling
- `eggthreads/eggthreads/runner.py`
  - automatic compaction scheduling after LLM turns,
  - context-length-exceeded recovery path,
  - queue ordering so summary runs before pending normal work.
- Tests likely around `eggthreads/tests/test_compaction.py`.

## Pending-state question

Current pending marker behavior may assume the later `thread.compaction` event completes the summary request.
With the new order, completion should probably be based on the automatic summary request receiving a later assistant response, or on a small internal completion marker if needed.
Prefer the simplest robust option and avoid adding a public concept unless necessary.

## Acceptance criteria

- When auto-compaction threshold is reached, a `thread.compaction` event is written before the summary request message.
- The automatic summary prompt appears in the compacted provider context.
- The prompt tells the assistant to use hydrated REPL history helpers.
- The assistant is not asked to call `compact_thread()`.
- The summary turn runs before later queued messages.
- Existing manual `/compact`, `/compactWithSummary`, and `compact_thread` behavior remains sane or is intentionally updated with tests.
- Regression tests cover the new event ordering and pending-summary completion behavior.

## Status notes

- 2026-05-12 01:14 UTC: Implemented the API/command-level first slice. Automatic summary mode now commits a `thread.compaction` boundary before appending the summary request and marker, returns that compaction in `AutoCompactionResult`, and treats the pending marker as complete once the request receives a later assistant response. The shared summary prompt now says compaction already happened, points at hydrated REPL/thread-history helpers (`all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, `messages_by_id`, `messages_by_role`, `search_thread(...)`, `get_message(...)`, `print_message(...)`, `reload_thread_context()`), asks for continuation-summary details, and no longer asks for `compact_thread()`. `/compactWithSummary` now uses the same commit-then-request flow; plain `/compact` and direct `compact_thread` remain unchanged.
- Tests added/updated around event ordering, provider-view prompt placement, prompt content, summary-marker completion by assistant response, runner summary turn ordering, and `/compactWithSummary` behavior. Tests run: `pytest -q eggthreads/tests/test_compaction.py`; `pytest -q eggthreads/tests/test_compaction.py eggthreads/tests/test_command_registry.py`.
- Next slice: context-length-exceeded provider-error recovery remains separate runner work. Implement a recovery path that detects provider context-length errors, commits a safe compaction boundary, appends the new summary request, and retries/runs that summary before queued normal work.
