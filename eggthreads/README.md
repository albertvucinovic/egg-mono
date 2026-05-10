# eggthreads

`eggthreads` is the core Egg conversation engine. It stores thread state in
SQLite as an append-only event log, builds effective snapshots, runs async LLM
and tool turns, manages parent/child thread trees, and exposes the primitives
used by both the terminal and web UIs.

## What it provides

- SQLite-backed thread/event storage (`events`, `threads`, `children`, leases).
- Stable parent/child thread trees for delegation and subtree scheduling.
- `ThreadRunner` and `SubtreeScheduler` for RA1 LLM turns and RA2/RA3 tool turns.
- Plugin-populated model-visible tools and user commands.
- Tool-call approval, output stashing, terminal-safety filtering, and sandbox
  integration.
- Persistent Python/Bash REPL sessions with hydrated thread context.
- Thread compaction by provider-context start pointer, without hiding UI/raw
  history.
- Token/status helpers for current provider context and full visible history.

## Thread model

A thread is one append-only raw event log plus an effective view built from that
log. Messages, stream deltas, tool-call state, user commands, compaction markers,
model switches, and control events are all events.

Important properties:

- Raw history is preserved for UI/audit.
- Effective snapshots hide deleted/skipped messages.
- `/continue <msg_id>` marks later messages/control events ineffective while
  leaving raw events available for audit.
- Parent/child rows model delegation, not compaction.

## Compaction

Compaction is represented by a `thread.compaction` event:

```text
set_provider_context_start(thread_id, start_msg_id)
```

The provider/API prompt starts at the selected message; UI/raw history still
shows the full thread.

Supported user/model surfaces:

```text
/compact [msg_id|last_user|last_llm]
/compactWithSummary
compact_thread(start_message?)
```

Automatic compaction can be threshold-triggered. Summary mode is the default and
is controlled by `EGG_COMPACT_SUMMARY`:

- unset/empty/truthy: append a summary request and let the assistant call
  `compact_thread()`;
- `0`, `false`, `no`, `off`: compact directly to `last_llm`.

Threshold precedence:

1. latest effective `thread.compaction_context_length` event;
2. explicit `RunnerConfig.auto_compact_threshold_tokens`;
3. 80% of current model `max_tokens`;
4. `EGG_AUTO_COMPACT_THRESHOLD_TOKENS`;
5. fallback `150000`.

Token naming:

- `context_tokens`: current provider/API context after compaction.
- `full_thread_tokens`: full visible/effective history before compaction
  filtering.

## Persistent REPL context

Python REPL sessions are hydrated with useful transcript state:

```python
thread_context
all_messages
current_prompt_messages
older_messages_not_in_prompt
messages_by_id
messages_by_role
user_messages
assistant_messages
tool_messages
compactions
context_files
```

Helper functions include:

```python
search_thread(query, role=None, in_prompt=None)
get_message(msg_id)
print_message(msg_id)
reload_thread_context()
```

Hidden/local-only content is excluded from this model-usable context.

## Headless subtree example

The repository includes a headless example that creates a root thread, creates
children, and runs a `SubtreeScheduler` over the whole subtree.

Run from the monorepo root:

```bash
python3 -u eggthreads/examples/headless_subtree_scheduler.py
```

Optional environment variables:

```bash
export SYSTEM_PROMPT_PATH=/path/to/systemPrompt
export EGG_MODELS_PATH=models.json
export EGG_ALL_MODELS_PATH=all-models.json
export MAX_CONCURRENT=8
```

The default database path for Egg frontends is `.egg/threads.sqlite`.

## Minimal programmatic use

```python
import asyncio
from eggthreads import (
    ThreadsDB, RunnerConfig, SubtreeScheduler,
    append_message, create_child_thread, create_llm_client,
    create_root_thread, create_snapshot, wait_subtree_idle,
)

async def main():
    db = ThreadsDB(".egg/threads.sqlite")
    db.init_schema()

    root = create_root_thread(db, name="root")
    child = create_child_thread(db, root, name="worker")
    append_message(db, child, "user", "Write a short status report.")
    create_snapshot(db, child)

    llm = create_llm_client()
    scheduler = SubtreeScheduler(
        db,
        root_thread_id=root,
        llm=llm,
        config=RunnerConfig(max_concurrent_threads=2),
    )
    task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    await wait_subtree_idle(db, root)
    task.cancel()

asyncio.run(main())
```

## Sandbox/session Docker images

For Docker-backed tool execution and REPL sessions:

```bash
./eggthreads/docker/create-image.sh
```

This builds the shared `egg-sandbox` image and the thin `egg-rlm-session`
wrapper image. To build only the session image:

```bash
./eggthreads/docker/create-session-image.sh
```

If the local image is missing, Docker sandboxing can fall back to a plain Python
image, but the Egg image includes development tools used by tests and examples.

## Tests

```bash
pytest -q eggthreads/tests
pytest -q eggthreads/tests/test_compaction.py
pytest -q eggthreads/tests/test_child_status.py
```

## Related docs

- `README.md` at the monorepo root for overall setup.
- `eggthreads/API.md` for API-oriented notes.
- `eggthreads/eggthreads/web/searxng/README.md` for local web search.
