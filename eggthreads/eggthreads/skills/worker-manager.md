# Worker Manager Skill

Use this skill when a task should be implemented through a manager/worker split using a durable TODO / handoff document, such as `compaction-todo.md` or `plugins-todo.md`.

This is the authoritative Worker Manager skill. In repository checkouts it may be present both at the repo root and under `eggthreads/eggthreads/skills/`; keep those copies synchronized when editing the skill text.

## Core idea

The manager stays responsible for direction, scope, review, and user-facing synthesis. The worker does focused implementation slices.

For manager/worker communication, use the `infinite-turn` skill in the worker. The worker should not end each slice with a normal final assistant message; instead, its would-be final status goes in `get_user_message_while_preserving_llm_turn(assistant_note=...)`, and the manager's next instruction becomes the tool result that lets the same worker continue. Mid-turn manager messages should be answered with `answer_user_while_preserving_llm_turn`.

When `wait` returns because the worker is waiting in `get_user_message_while_preserving_llm_turn`, treat the visible assistant note as the worker's slice status. Review it, then use `send_message_to_child` with the next manager instruction; that message answers the worker's get-user tool call and continues the same worker turn. If there is no next slice, send a brief stop/stand-by instruction instead of leaving the worker expecting more guidance.

If the user gives a time budget, e.g. “work on this for 4 hours,” that budget belongs to the **manager**, not the worker. The manager should not tell the worker “work for 4 hours.” Instead, the manager should assign one smaller concrete slice, wait for it to finish, review it, then send the next smaller concrete slice to the same worker until the requested manager-side budget is used, the work is complete, or a stop condition is reached.

Worker threads are treated as **infinite** for this workflow: they have auto-compaction plus summarization and can keep useful project context across phases. Therefore, the manager should normally create **one primary worker thread for the task** and keep sending it the next slice.

A good worker loop is:

```text
manager reads TODO + repo state
manager spawns one primary worker with the first clear slice
worker edits/tests/commits/updates TODO
manager waits in bounded increments
manager reviews result/status
manager sends the next slice to the same worker, or stops for user discussion
```

Do not rotate workers because a phase completed. Do not rotate workers because the task is long. Do not rotate workers because of context-size concerns. The worker's accumulated context is an asset.

Do not use workers as a substitute for product/design decisions. If a TODO item contains an unresolved design choice, the worker may analyze options, but the manager should decide or ask the user.

## Manager pre-flight

Before spawning the primary worker:

1. Read the relevant TODO/handoff file.
2. Run:

   ```bash
   git status --short
   git log --oneline -8
   ```

3. Identify the earliest incomplete phase unless the user explicitly selected another phase.
4. Check whether there is uncommitted tracked work.
   - If yes, inspect it before spawning a worker.
   - Do not let a worker accidentally build on unknown dirty state.
5. Decide the smallest useful first worker slice.
6. Note any hard constraints from the user.

After the primary worker exists, prefer sending continuations to that same worker over spawning another one.

## Worker scope rules

Give the worker one coherent implementation slice at a time, not an entire multi-phase plan unless the user explicitly wants broad autonomous execution.

Do not delegate duration management to the worker. A worker task should be scoped by concrete deliverable, not by wall-clock budget. The manager owns deciding when to send another slice and when to stop.

Good worker scopes:

```text
Implement Phase 5 only: /continue invalidates compaction events.
Add tests and commit.
```

```text
Add the REPL context builder only. Do not wire hydration yet.
```

```text
Remove these three redundant tools and update tests. Do not implement the replacement yet.
```

Bad worker scopes:

```text
Work on compaction for 4 hours.
```

```text
Keep going until the long-run budget is used up.
```

```text
Finish compaction.
```

```text
Refactor context, UI, commands, tests, and auto-compaction as needed.
```

The worker receives one slice at a time, but it should remain the same thread across slices so it can reuse its prior context.

## Spawn template for the primary worker

Use `spawn_agent_auto` for coding workers when tool auto-approval is appropriate.

Suggested template:

```text
Continue <project/task> implementation as the primary long-lived worker. Read ./<todo-file> first.
Use the `infinite-turn` skill for manager/worker handoffs: when your slice is done, put your full status in get_user_message_while_preserving_llm_turn(assistant_note=...) instead of sending a normal final answer.
Run git status --short before editing.
Current relevant commits: <commit list or latest hash>.
Your task now: <one small slice>.
Follow ./<todo-file> exactly: small meaningful commits, focused tests, update the TODO status/test notes before commit.
Do not broaden scope: <explicit non-goals>.
If this requires a broad refactor or unresolved design decision, stop and report options instead of implementing.
Report commit hash, tests, current git status, and next recommended task when done.
Expect to be reused for later slices, so preserve useful context in your final note.
```

Worker system prompt and tools:

- Do **not** pass a custom `system_prompt` for normal workers. Let the child inherit Egg's default/current system prompt so it keeps the same general capabilities and instructions as the manager. Put worker-specific guidance in `context_text`, not in `system_prompt`.
- Do **not** narrow `allowed_tools` for normal workers. Leave `allowed_tools` omitted/empty so the worker inherits all available tools, including REPL and thread-history tools needed for compaction-aware work.
- Leave thread-spawning tools available too unless the user explicitly asks to restrict delegation.
- Use `disabled_tools` only for a concrete safety reason, and state that reason in manager notes.
- Avoid sharing REPL/session unless the task explicitly requires shared state.

Typical spawn config for a normal worker:

```json
{
  "context_text": "<worker instructions>",
  "label": "<short label>",
  "disabled_tools": [],
  "share_session": false,
  "share_repl": false
}
```

Do not include `system_prompt` or `allowed_tools` in the normal case.

## Wait loop

Wait in bounded increments so the manager can retain control and handle failures.

Default pattern:

```text
wait(worker, timeout=300)
if not finished:
    get_child_status(worker)
    wait again
```

For long requested runs, repeat for the requested manager-side budget, e.g. up to 4 hours, but checkpoint after each worker result. Do not send the worker a single 4-hour assignment; send one bounded slice, wait for completion, then decide and send the next bounded slice.

If the worker is still running and healthy, keep waiting. If there are errors or no progress, intervene in that same worker when possible.

## Repairing the primary worker

Before abandoning a worker that hit a transient runner/LLM/session failure, repair the existing child thread.

Use this when:

- `wait` returns no useful assistant message;
- `get_child_status` shows recent LLM/runner/session errors;
- the child is back in `waiting_user` after an infrastructure failure;
- the worker likely did not get a chance to summarize, commit, or clean up.

Repair pattern:

```text
get_child_status(worker)
continue_subthread(worker)
wait(worker, timeout=300)
```

Guidelines:

1. Inspect `get_child_status` first so you know whether the failure looks transient or implementation-related.
2. Prefer repairing or continuing the existing worker before creating any replacement.
3. After the repaired worker returns, review its status exactly like any other worker result.
4. If the same infrastructure failure repeats, try one more targeted continuation with explicit guidance if that is safe.
5. Do not use `continue_subthread` to paper over real test failures or design blockers; those need root-cause fixes or manager/user decisions.

## After each worker result

When the worker returns:

1. Read its final message. If it is missing/empty because of a runner failure, use the repair pattern above before treating the worker as done.
2. Run locally if useful:

   ```bash
   git status --short
   git log --oneline -5
   ```

3. Confirm it committed the chunk and left no tracked dirty files.
4. Check the reported test commands and results.
5. Inspect the TODO status note if the task was important or risky.
6. Decide the next step:
   - send continuation to the same worker;
   - do a small manager fix;
   - stop and ask the user;
   - only exceptionally spawn a replacement or review worker.

## Sending continuation to the same worker

This is the normal path. Prefer continuing the primary worker almost always.

Reuse the same worker when:

- the next slice is another phase of the same TODO/task;
- the next slice builds directly or indirectly on previous implementation knowledge;
- the previous result was reliable;
- the worker has useful local understanding of tests, files, constraints, or prior decisions;
- there is no concrete safety reason to isolate the next task.

Continuation template:

```text
Great. Continue with the next unchecked item in ./<todo-file>: <item>.
Keep it to one small commit. Reuse your prior context. Update the TODO, run focused tests, commit, and report commit hash/tests/next task.
If this becomes broader than expected, stop and explain.
```

If the next slice is a new phase, still send it to the same worker by default:

```text
Continue to Phase <N>. Before editing, briefly re-read the relevant TODO section and your previous status note. Then implement only <specific item>. Keep one coherent commit and report tests/status.
```

## Spawning another worker is exceptional

Do **not** spawn a fresh worker merely because:

- the previous slice finished;
- the next task is a new phase;
- the worker thread is long;
- the worker context might be large;
- the manager wants tidy separation between phases.

Spawn a second/replacement worker only when there is a concrete reason, such as:

- the primary worker is unrecoverably stuck after repair attempts;
- the primary worker repeatedly ignores scope or makes unreliable changes;
- the working tree is messy and the manager wants a separate repair attempt with explicit instructions;
- the user explicitly asks for independent review or parallel work;
- a risky change needs an independent reviewer, not a continuation implementer;
- two truly independent tasks must run in parallel and the user accepts the coordination cost.

When spawning an exceptional worker, say why it is exceptional and provide enough context from the primary worker's commits/status. Do not let the new worker build on unknown partial state.

## Worker context

Worker threads are infinite for this workflow. Do not rotate workers or shorten guidance because of token budget. The long-lived worker's accumulated context should improve implementation quality across phases.

The manager should still keep guidance concise and explicit, but not because the worker might run out of context. Concision is for clarity.

## Commit discipline

Workers should commit meaningful chunks.

A meaningful chunk has:

- one coherent purpose;
- focused tests run;
- TODO/handoff status updated;
- no unrelated cleanup;
- no tracked dirty files after commit.

Managers should ask the worker to commit before returning. If the worker returns uncommitted changes, either send it back to finish/commit or inspect and commit manually.

## TODO/handoff discipline

The TODO file is the durable coordination layer. Workers must update it before committing.

Good status note:

```text
Status notes:
- YYYY-MM-DD HH:MM UTC: Implemented <slice>. Changed <files/modules>. Tests passed: <commands>. Commit: <hash>. Next: <specific task>. Caveats: <if any>.
```

For design changes, update the plan before implementation when possible.

Because the worker is long-lived, its status notes should be useful for both the manager and its own future continuations.

## Manager review checklist

Before telling the user a phase is done:

- Is the intended behavior implemented literally?
- Did tests cover the behavior and one likely failure mode?
- Did the worker avoid unrelated refactors?
- Is the TODO updated with current status and next task?
- Is `git status --short` clean for tracked files?
- Are any remaining open questions explicit?

## Handling failures

If the primary worker hits a transient LLM/runner/session failure, inspect `get_child_status` and try `continue_subthread(child_thread_id)` before discarding it.

If the worker fails tests:

1. Ask the same worker to fix the root cause if the failure is in scope.
2. If the fix expands scope, stop and ask the user.
3. If the worker is confused or unreliable after targeted guidance, then consider an exceptional replacement or review worker.

If the worker leaves a messy partial edit:

- inspect `git diff`;
- decide whether to continue the same worker, revert, repair manually, or ask the user;
- do not spawn another worker on top of unknown partial state unless the new worker is explicitly told to repair it.

## Long-running manager loop

When the user asks for a long loop, e.g. “wait in 300s increments for at least 4h or until done”:

1. Treat the requested duration as a manager-side budget.
2. Spawn one primary worker for the next concrete slice only.
3. Wait 300s.
4. If unfinished, inspect child status.
5. Continue waiting while healthy.
6. When the worker finishes, review its result and decide the next concrete slice.
7. Send the next slice to the same worker; do not delegate the remaining time budget to the worker.
8. Repeat until the manager-side budget is used, implementation is complete, or a stop condition is reached.
9. Stop early only if:
   - implementation is complete;
   - user/design decision is required;
   - tests fail in a way that needs manager/user input;
   - the current worker is unreliable enough to justify an exceptional replacement.

Bad long-loop instruction to a worker:

```text
Work on this for 4 hours and decide what to do next.
```

Good long-loop manager behavior:

```text
Manager sends: Implement only the provider header helper and tests.
Worker finishes.
Manager reviews.
Manager sends: Now implement only the API-key selector helper and tests.
```

The old loop in shorthand is:

```text
manager owns time budget
worker owns current concrete slice
manager reviews after each slice
manager sends next slice if budget and stop conditions allow
```

Keep a compact manager-side ledger of:

```text
primary worker id -> current task, commits, tests, next recommendation, any repair attempts
exceptional worker ids -> why they were needed, result
```

## Final response to user

When reporting back, include:

- commits made;
- phases/items completed;
- tests run;
- remaining tasks/open questions;
- whether tracked working tree is clean;
- if more than one worker was used, why that exception was necessary.

Keep it concise unless the user asks for detailed logs.
