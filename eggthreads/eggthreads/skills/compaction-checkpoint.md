# Compaction Checkpoint Skill

Use this skill when the provider context starts immediately after an Egg thread compaction and the prompt asks for a continuation checkpoint.

## Goal

Create a concise durable checkpoint of the work state, then choose whether to stop or continue based on the compaction mode.

The checkpoint should preserve:

- the pending user request or task;
- important decisions, invariants, and design constraints;
- files changed or intended to change;
- commands/tests already run and their results;
- known failures, risks, or unresolved questions;
- exact next steps.

Use hydrated thread-history helpers when needed (`all_messages`, `current_prompt_messages`, `older_messages_not_in_prompt`, `messages_by_id`, `search_thread(...)`, `get_message(...)`, `print_message(...)`, `reload_thread_context()`).

## Modes

### `summary_only`

Use this mode for user-initiated/manual/handoff compaction, including `/compactWithSummary` unless the prompt explicitly says otherwise.

Behavior:

1. Write the checkpoint as normal assistant content.
2. Do not continue the task in the same turn.

### `checkpoint_and_resume`

Use this mode for recovery compaction, such as context-length exhaustion while the assistant was trying to continue work or an unhandled user message that arrived during assistant streaming.

Behavior:

1. Call `answer_user_while_preserving_llm_turn` with the checkpoint summary.
2. Do not treat that checkpoint as the final answer to the task.
3. Continue from the current actionable state after the checkpoint.
4. If there is a newer unhandled user message that arrived during the interrupted work, handle that user message before resuming older work.
5. Do not fabricate tool results. If complete assistant tool calls were persisted, let the tool-call state machine handle them; if only partial tool-call deltas existed, resume from the last stable user/task state.

## Output style

Keep the checkpoint concise but specific. Prefer bullets. Avoid replaying the whole transcript.
