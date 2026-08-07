# eggthreads

`eggthreads` is Egg's durable conversation and tool runtime. It stores
append-only thread events in SQLite, projects effective messages and snapshots,
runs model and tool turns, schedules parent/child thread trees, and provides the
shared command/tool plugins used by both the terminal and web clients.

Use it directly for a headless agent runtime, or through
[`egg`](../egg/README.md) and [`eggw`](../eggw/README.md). Model-provider
normalization is delegated to [`eggllm`](../eggllm/README.md).

## Install

```bash
pip install -e ./eggllm
pip install -e ./eggthreads
```

Python 3.10+ is required. The default database path used by Egg clients is
`.egg/threads.sqlite` in the active project.

## Runtime model

A thread has:

- one row of metadata and optional snapshot state;
- an ordered append-only event log;
- zero or one parent and any number of child threads;
- at most one active invocation lease;
- effective model, tool, sandbox, scheduling, context, and recovery settings.

Messages, edits, model switches, stream lifecycle, tool calls, approvals,
commands, compaction, and control operations are events. Raw history remains
available for audit while projection determines the effective transcript sent
to providers and shown by normal APIs. Snapshots accelerate projection; events
remain authoritative.

Parent/child links represent delegation. They are distinct from provider-
context compaction and from persistent REPL runtime children.

## Runners and scheduling

The scheduler recognizes three actionable classes:

- **RA1** — a user/tool trigger needs an LLM response;
- **RA2** — an assistant tool call needs execution;
- **RA3** — a queued user-originated tool call needs execution.

`ThreadRunner` acquires a fenced SQLite lease and advances one thread.
`SubtreeScheduler` discovers runnable work below a root and applies separate LLM
and optional tool concurrency limits. `RunnerConfig` controls leases,
heartbeats, concurrency, timeouts, priority behavior, context limits,
read-only mode, and automatic-compaction thresholds.

Leases make interrupted or competing writers fail closed. Status helpers derive
streaming/runnable/idle state from live leases and actionable work rather than
trusting the static metadata status alone.

## Minimal headless example

```python
import asyncio
from eggthreads import (
    RunnerConfig,
    SubtreeScheduler,
    ThreadsDB,
    append_message,
    create_child_thread,
    create_llm_client,
    create_root_thread,
    create_snapshot,
    wait_subtree_idle,
)

async def main():
    db = ThreadsDB(".egg/threads.sqlite")
    db.init_schema()

    root = create_root_thread(db, name="Coordinator", models_path="models.json")
    worker = create_child_thread(db, root, name="Worker", models_path="models.json")
    append_message(db, worker, "user", "Write a short status report.")
    create_snapshot(db, worker)

    scheduler = SubtreeScheduler(
        db,
        root_thread_id=root,
        llm=create_llm_client(
            models_path="models.json",
            all_models_path="all-models.json",
        ),
        config=RunnerConfig(max_concurrent_llm_threads=2),
        models_path="models.json",
        all_models_path="all-models.json",
    )
    task = asyncio.create_task(scheduler.run_forever())
    try:
        await wait_subtree_idle(db, root)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scheduler.shutdown()
        db.close()

asyncio.run(main())
```

The repository includes a fuller example:

```bash
python3 -u eggthreads/examples/headless_subtree_scheduler.py
```

## Tools, commands, and plugins

`ToolRegistry` owns model-visible tool specifications and execution. The built-in
plugin catalog supplies Bash/Python execution, persistent REPLs, web search and
fetch, attachments, artifacts, image generation, child-thread coordination,
thread inspection, compaction, waiting, and administrative controls.

Tools have durable lifecycle events, approval state, timeouts, terminal-safe
presentation, and output policies. Large outputs can remain in canonical tool
events while bounded presentation or extracted provider artifacts keep prompts
manageable.

`create_default_command_registry()` exposes the shared slash-command catalog to
frontends. UI-specific commands may be registered by Egg or EggW on top.

## Thread trees and cross-thread boundaries

Child agents can be spawned, inspected, messaged, waited on, interrupted, or
continued. Ancestor access is explicit: APIs reject unrelated, sibling, or
ancestor targets where a caller is authorized only over descendants.

The model-visible `threads` tool returns the calling subtree as nested JSON with
full ids, names, descriptions, effective models, last-modified timestamps,
real-time state, and children. Fast mode detects streaming; full mode also
checks runnability. The same tree implementation backs `/threads`.

`execute_tool_in_other_thread` lets an ancestor run an opted-in tool with a
strict descendant's context. For persistent REPL tools, hydration uses that
descendant's effective history. Results publish to the caller while normal tool
side effects remain owned by the target runtime/thread.

## Persistent Python and Bash sessions

Python/Bash REPL tools use named channels backed by persistent session
containers or processes. Python context is hydrated with transcript views such
as:

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

Helpers include `search_thread(...)`, `get_message(...)`, `print_message(...)`,
and `reload_thread_context()`. Hidden/local-only content is excluded.

Docker-backed session limits are configured with
`EGG_RLM_SESSION_MEMORY` and `EGG_RLM_SESSION_PIDS_LIMIT`. See source validation
and tests for accepted ranges; invalid limits fail before container
reconciliation.

## Compaction and continuation

A `thread.compaction` event changes where provider context begins without
removing raw/UI history. Public surfaces include:

```text
/compact [msg_id|last_user|last_llm]
/compactWithSummary
compact_thread(start_message?)
```

Summary mode asks the assistant to produce a checkpoint before moving the
boundary. `EGG_COMPACT_SUMMARY=0` selects direct compaction. Automatic
compaction resolves its threshold from thread settings, runner configuration,
model context metadata, environment, then a fallback.

`/continue <msg_id>` and continuation APIs make later effective messages/control
state inapplicable while preserving raw events. Repair validates boundaries,
recovers interrupted invocations where safe, and refuses ambiguous history
rather than replaying silently.

Token APIs distinguish:

- `context_tokens`: current provider-visible context after compaction;
- `full_thread_tokens`: full visible/effective history before context filtering.

## Attachments, artifacts, and sandboxing

Input attachments and provider-output artifacts are content-inspected,
access-controlled, and represented by durable references. Provider lowering is
model-capability aware. Artifact promotion and extraction enforce thread
ownership before bytes enter another context.

Tool path access uses each thread's working directory and sandbox policy. Build
the provided Docker images with:

```bash
./eggthreads/docker/create-image.sh
```

The shared image supports sandboxed commands and the thin persistent-session
wrapper. Without a configured sandbox, host execution may use the launching
user's permissions.

## Extension points

Public extension boundaries include:

- `ToolRegistry` and `CommandRegistry` registrations;
- input-prefix, display-input, output-policy, and context-policy plugins;
- custom `LLMClient`-compatible providers passed to runners;
- APIs for events, projection, scheduling, tools, sandbox settings, and thread
  lifecycle.

Prefer these boundaries over writing events or snapshot JSON directly.

## Development

```bash
pip install -e "./eggthreads[dev]"
pytest -q eggthreads/tests
```

Useful references:

- [API guide](API.md)
- [System design](system-design.md)
- [Local SearXNG integration](eggthreads/web/searxng/README.md)
- [Root project documentation](../README.md)
