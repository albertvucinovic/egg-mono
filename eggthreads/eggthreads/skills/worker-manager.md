# Worker Manager Skill

Use this skill when a task should be implemented across one or more worker subthreads using a hierarchical TODO / handoff document, such as `compaction-todo.md` or `plugins-todo.md`.

This is a local skill-style document, not an installed Egg skill. To use it in a future session, tell Egg:

```text
Please use ./worker-manager.md to manage workers for <task/todo-file>.
```

or:

```text
Read ./worker-manager.md and follow it while delegating this implementation.
```

## Core idea

The manager stays responsible for direction, scope, and synthesis. Workers do focused implementation chunks.

A good worker loop is:

```text
manager reads TODO + repo state
manager spawns worker with one clear slice
worker edits/tests/commits/updates TODO
manager waits in bounded increments
manager reviews result/status
manager either sends next slice, spawns fresh worker, or stops for user discussion
```

Do not use workers as a substitute for product/design decisions. If a TODO item contains an unresolved design choice, the worker may analyze options, but the manager should decide or ask the user.

## Manager pre-flight

Before spawning workers:

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
5. Decide the smallest useful worker slice.
6. Note any hard constraints from the user.

## Worker scope rules

Give each worker one coherent implementation slice, not an entire multi-phase plan unless the user explicitly wants broad autonomous execution.

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
Finish compaction.
```

```text
Refactor context, UI, commands, tests, and auto-compaction as needed.
```

## Spawn template

Use `spawn_agent_auto` for coding workers when tool auto-approval is appropriate.

Suggested template:

```text
Continue <project/task> implementation. Read ./<todo-file> first.
Run git status --short before editing.
Current relevant commits: <commit list or latest hash>.
Your task: <one small slice>.
Follow ./<todo-file> exactly: small meaningful commits, focused tests, update the TODO status/test notes before commit.
Do not broaden scope: <explicit non-goals>.
If this requires a broad refactor or unresolved design decision, stop and report options instead of implementing.
Report commit hash, tests, current git status, and next recommended task when done.
```

Recommended worker system prompt:

```text
You are a careful coding worker for Egg. Follow the specified TODO/handoff file exactly. Keep changes small, run focused tests, update the TODO with status notes, and commit each meaningful chunk. Do not broaden scope. Use git commands, not grep -R or ls -R. Prefer root-cause fixes and reuse existing code. Report concise progress and next steps when waiting for manager guidance.
```

Typical tool allowance:

```json
{
  "allowed_tools": ["bash", "python"],
  "disabled_tools": [],
  "share_session": false,
  "share_repl": false
}
```

Avoid sharing REPL/session unless the task explicitly requires shared state.

## Wait loop

Wait in bounded increments so the manager can retain control and handle failures.

Default pattern:

```text
wait(worker, timeout_sec=300)
if not finished:
    get_child_status(worker)
    wait again
```

For long requested runs, repeat for the requested budget, e.g. up to 4 hours, but checkpoint after each worker result.

If the worker is still running and healthy, keep waiting. If there are errors, high context, or no progress, intervene.

## After each worker result

When a worker returns:

1. Read its final message.
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
   - spawn a fresh worker;
   - do a small manager fix;
   - stop and ask the user.

## Sending continuation to an existing worker

Reuse the same worker when:

- its context is still reasonable;
- the next slice builds directly on its knowledge;
- it is not close to the context limit;
- the previous result was reliable.

Continuation template:

```text
Great. Continue with the next unchecked item in ./<todo-file>: <item>.
Keep it to one small commit. Update the TODO, run focused tests, commit, and report commit hash/tests/next task.
If this becomes broader than expected, stop and explain.
```

## Spawning a fresh worker

Spawn a fresh worker when:

- the previous worker context is large or near the context limit;
- the next task is a new phase;
- the previous worker made many decisions and a clean slate is safer;
- the worker reports context pressure;
- the manager wants independent review.

Fresh-worker context should include:

- TODO file path;
- latest relevant commits;
- what was just completed;
- exact next slice;
- non-goals.

## Context pressure

If worker context approaches the effective limit, do not keep sending more work. Spawn a new worker with a concise handoff.

Useful rule of thumb:

```text
If worker context is above ~70-80% of the expected limit, prefer a fresh worker for the next phase.
```

If exact context limit is unknown but the status shows very large context, prefer a fresh worker.

## Commit discipline

Workers should commit meaningful chunks.

A meaningful chunk has:

- one coherent purpose;
- focused tests run;
- TODO/handoff status updated;
- no unrelated cleanup;
- no tracked dirty files after commit.

Managers should ask workers to commit before returning. If a worker returns uncommitted changes, either send it back to finish/commit or inspect and commit manually.

## TODO/handoff discipline

The TODO file is the durable coordination layer. Workers must update it before committing.

Good status note:

```text
Status notes:
- YYYY-MM-DD HH:MM UTC: Implemented <slice>. Changed <files/modules>. Tests passed: <commands>. Commit: <hash>. Next: <specific task>. Caveats: <if any>.
```

For design changes, update the plan before implementation when possible.

## Manager review checklist

Before telling the user a phase is done:

- Is the intended behavior implemented literally?
- Did tests cover the behavior and one likely failure mode?
- Did the worker avoid unrelated refactors?
- Is the TODO updated with current status and next task?
- Is `git status --short` clean for tracked files?
- Are any remaining open questions explicit?

## Handling failures

If a worker fails tests:

1. Ask it to fix the root cause if the failure is in scope.
2. If the fix expands scope, stop and ask the user.
3. If the worker is confused or context-heavy, spawn a fresh worker with the failing test and relevant diff.

If a worker leaves a messy partial edit:

- inspect `git diff`;
- decide whether to continue, revert, or ask the user;
- do not spawn another worker on top of unknown partial state unless the new worker is explicitly told to repair it.

## Long-running manager loop

When the user asks for a long loop, e.g. “wait in 300s increments for at least 4h or until done”:

1. Spawn one worker for the next slice.
2. Wait 300s.
3. If unfinished, inspect child status.
4. Continue waiting while healthy.
5. When finished, send a continuation for the next slice or spawn a fresh worker.
6. Stop early only if:
   - implementation is complete;
   - user/design decision is required;
   - tests fail in a way that needs manager/user input;
   - worker context is too high and a fresh worker is needed.

Keep a compact manager-side ledger of:

```text
worker id -> task, commits, tests, next recommendation
```

## Final response to user

When reporting back, include:

- commits made;
- phases/items completed;
- tests run;
- remaining tasks/open questions;
- whether tracked working tree is clean.

Keep it concise unless the user asks for detailed logs.
