# Explicit RLM Plan for eggthreads

This document describes a detailed implementation plan for a native eggthreads
RLM (Recursive Language Models) substrate.

The central design is:

> A persistent REPL/session is represented by a real child thread. Programmatic
> REPL tool calls are enqueued into that runtime child thread as normal
> user-originated tool calls, then executed by the existing `ThreadRunner` through
> the TC state machine. Completion callbacks are derived from persisted events,
> not in-memory runner callbacks.

This preserves the eggthreads event-sourced model, makes recursive execution
visible in the thread tree, avoids duplicating tool execution logic, and gives
normal tool approval/output-approval semantics.

---

## 1. Goals

### 1.1 User-facing goals

- A thread can request a persistent Docker-backed session.
- The session can expose a Python REPL and/or Bash REPL.
- REPL state persists across calls in the same session.
- A spawned child can share the parent session when requested.
- Code running in the REPL can call Egg tools programmatically:
  - `spawn_agent`
  - `spawn_agent_auto`
  - `wait`
  - `web_search`
  - `fetch_url`
  - `bash`
  - `python`
  - `replace_between`
  - eventually every tool in the active `ToolRegistry`
- Programmatic tool calls should be chainable:

  ```python
  from eggtools import spawn_agent, wait

  def llm_response(query):
      thread_id = spawn_agent(context_text=query, label="child")
      result = wait([thread_id])
      return result[thread_id]
  ```

- When REPL code calls `spawn_agent`, the spawned thread should be a child of
  the executing runtime thread, so normal egg thread tree commands show the full
  recursive execution.
- This should work recursively.
- The caller should control which tools spawned children may access.

### 1.2 System goals

- Keep the event log as the source of truth.
- Reuse existing TC states and `ThreadRunner` tool execution logic.
- Avoid special in-memory callback-only state.
- Avoid letting Docker containers mutate `.egg/threads.sqlite` directly.
- Avoid same-thread runner reentrancy deadlocks.
- Avoid scheduler deadlocks caused by counting tool-running threads as scarce
  LLM-running threads.
- Keep tool permissions attenuating down the recursion tree.

---

## 2. Existing eggthreads concepts to reuse

The current system already has useful primitives:

- Threads are tree-structured via `children(parent_id, child_id)`.
- All state is event-sourced through the `events` table.
- Tool calls use TC states reconstructed from events:
  - TC1: needs approval
  - TC2.1: approved
  - TC2.2: denied
  - TC3: executing
  - TC4: finished, waiting output approval
  - TC5: output decision made
  - TC6: published tool message exists
- Runner actionables:
  - RA1: LLM turn
  - RA2: assistant-originated tool execution
  - RA3: user-originated tool execution
- User commands are already represented as RA3 tool calls.
- `ToolRegistry.execute(...)` already injects `_thread_id` when called with
  runner context. Existing `spawn_agent` uses this to infer parent thread.
- `SubtreeScheduler` drives all runnable threads in a subtree.
- `stream.open` already records `stream_kind`/`ra_kind`, and runner already
  distinguishes `purpose='llm'` from `purpose='tool'`.

The RLM design should deepen these concepts instead of bypassing them.

---

## 2.5 Runtime invariants

The implementation should preserve these invariants. They are the compact
contract that keeps the RLM design egg-native rather than a parallel execution
system.

1. **Egg events are the source of truth.** Docker containers may hold mutable
   compute state, but semantic execution state lives in the eggthreads event
   log.
2. **Containers never write directly to eggthreads SQLite.** Container code may
   request actions only through the host bridge.
3. **The host bridge is the authority boundary.** It resolves eval tokens,
   identifies the executing runtime thread, checks capabilities, and appends
   events.
4. **Runtime tool calls are RA3 tool calls on the runtime thread.** Programmatic
   calls from `eggtools`/`eggtool` are represented exactly like user-originated
   tool calls, not as hidden side effects.
5. **Runtime tool execution is owned by `ThreadRunner`.** The bridge enqueues
   and waits; it does not duplicate the runner's tool execution semantics.
6. **Bridge callbacks are event-derived.** Completion means the matching
   `tool_call_id` reaches TC6 in persisted events.
7. **Runtime internal messages are `no_api=True` by default.** Runtime audit
   messages should not accidentally trigger RA1 or leak into provider context.
8. **The runtime thread is the executing thread for REPL programmatic calls.**
   Therefore `spawn_agent` from a REPL creates children under the runtime
   thread.
9. **Capabilities attenuate recursively.** A child cannot gain tools that the
   calling/runtime thread did not have.
10. **Tool-running threads are running but do not consume LLM slots.** Scheduler
    accounting distinguishes active tool work from scarce RA1/LLM work.
11. **A runtime child stays in the caller's subtree.** The normal subtree
    scheduler should be able to discover and drive it.
12. **Sharing a Docker session does not imply sharing a REPL channel.** By
    default, shared containers still use per-thread/per-runtime interpreter
    channels.
13. **Session sharing is explicit or policy-driven.** Children do not inherit a
    mutable session by accident.
14. **Outer REPL calls and runtime activity should be linkable.** Events should
    carry enough metadata to trace from a model-facing `python_repl`/`bash_repl`
    result to the runtime thread/eval that produced it.


## 3. Core model

### 3.1 Application thread and runtime thread

If an application thread `T` asks for a persistent REPL/session, create or reuse
one or more runtime child threads under `T`:

```text
T  (normal conversation / task thread)
└── RT  (@runtime:python, @runtime:bash, or @runtime:session)
```

The runtime thread is a real egg thread. It stores:

- REPL eval requests.
- Programmatic tool calls from inside the REPL.
- Tool results.
- Session/container lifecycle events.
- Child agents spawned by REPL code.

By default, the runtime thread is non-LLM:

- `llm_tools_enabled=False`
- runtime/eval messages are usually `no_api=True`
- runtime/eval messages use `keep_user_turn=True`

The runtime thread can still have a model if needed, but the MVP should treat it
as an execution/audit thread rather than an assistant conversation.

### 3.2 Persistent Docker session

A session is the persistent compute environment. A runtime thread references or
owns a session.

A session means:

- persistent Docker container
- persistent filesystem inside that container
- persistent Python/Bash process state per REPL channel
- optional sharing between runtime threads/children

Important distinction:

- **Session**: shared container/filesystem/process namespace.
- **REPL channel**: Python/Bash interpreter state. Default should be per runtime
  thread to avoid interpreter-level contention/deadlocks.

Example tree with shared container but separate runtime threads:

```text
main-thread
└── @runtime:python  (session_id=sess_abc, repl=py:main-thread)
    └── child-agent
        └── @runtime:python  (session_id=sess_abc, repl=py:child-agent)
```

### 3.3 Programmatic tools as RA3 calls on the runtime thread

When Python code inside a REPL calls:

```python
web_search("recursive language models")
```

the bridge does not execute `web_search` directly and does not append a nested
ad-hoc event only. Instead, it enqueues a normal user-originated tool call on
the runtime thread `RT`:

```json
{
  "role": "user",
  "content": "eggtools.web_search(query='recursive language models')",
  "tool_calls": [
    {
      "id": "tc_...",
      "type": "function",
      "function": {
        "name": "web_search",
        "arguments": "{\"query\": \"recursive language models\"}"
      }
    }
  ],
  "no_api": true,
  "keep_user_turn": true,
  "origin": "repl"
}
```

The bridge may auto-approve the call if permitted by the runtime thread's
capability config:

```json
{
  "type": "tool_call.approval",
  "payload": {
    "tool_call_id": "tc_...",
    "decision": "granted",
    "reason": "Approved as REPL programmatic tool call"
  }
}
```

Then the existing scheduler/runner executes it through TC states:

```text
TC1 -> TC2.1 -> TC3 -> TC4 -> TC5 -> TC6
```

The bridge waits for TC6 by watching events and returns the final tool message
content to the REPL.

---

## 4. Callback model

### 4.1 Event-log callback, not in-memory runner callback

The bridge should not pass in-memory callbacks into `ThreadRunner`, because such
callbacks are not crash-safe and do not work naturally across processes.

Instead, a programmatic REPL tool call returns a local future/promise that is
completed by observing persisted events:

1. enqueue tool call on runtime thread
2. wait until matching `tool_call_id` reaches TC6
3. read matching `msg.create role='tool'`
4. return content/structured result to REPL

If the bridge process dies, the tool call remains in the event log. A new bridge
can reconnect and wait for the same `tool_call_id`.

### 4.2 New helper: wait for specific tool call result

Add or reuse helpers around existing tool state:

```python
wait_for_tool_call_result(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    timeout_sec: float | None = None,
    poll_interval: float = 0.05,
) -> ToolCallResult | None
```

Async variant:

```python
async def wait_for_tool_call_result_async(...)
```

The result should include at least:

```python
@dataclass
class ToolCallResult:
    thread_id: str
    tool_call_id: str
    state: str
    content: str | None
    finished_reason: str | None
    output_decision: str | None
    timed_out: bool = False
```

For compatibility with current APIs, a simple `str | None` helper can also be
provided.

---

## 5. Avoiding deadlocks

### 5.1 Same-thread reentrancy deadlock

Bad design:

```text
RT runs python_repl_eval as a tool call
  Python code calls web_search()
    bridge enqueues web_search on RT
    bridge waits for RT runner
```

This deadlocks because `RT` is already leased by `python_repl_eval`, so another
`ThreadRunner` cannot execute the nested `web_search` in `RT`.

### 5.2 Chosen solution

The outer model-facing `python_repl` tool call runs in the application thread
`T`, while nested programmatic tool calls are enqueued into the runtime child
thread `RT`.

```text
T
  assistant calls python_repl
  T runs the outer python_repl tool

T
└── RT
    web_search/spawn_agent/wait calls are enqueued here and executed by RT runner
```

`RT` is free to be leased by its own runner because the outer `python_repl` tool
is executing in `T`, not `RT`.

### 5.3 Scheduler resource deadlock

Even with separate runtime thread `RT`, a scheduler can deadlock if it counts all
running threads against a single global slot pool.

Bad example with current semantics if `max_concurrent_threads=1`:

```text
T is running python_repl and waits for RT's web_search.
Scheduler slot is occupied by T.
RT cannot run.
T waits forever.
```

### 5.4 Chosen scheduler fix

Separate two concepts:

1. A thread is active/running.
2. A thread consumes a scarce LLM scheduler slot.

RA classifications:

```text
RA1_llm              consumes LLM slot
RA2_tools_assistant  does not consume LLM slot
RA3_tools_user       does not consume LLM slot
```

Tool-running threads remain active, leased, interruptible, and visible as
running, but they do not consume the LLM concurrency semaphore.

This is both more correct and improves normal tool-heavy workflows.

### 5.5 Bridge should not drive runtime thread by default

If the application thread is running under a `SubtreeScheduler`, then the root
scheduler is already alive. Since `RT` is a descendant of `T`, the same scheduler
will discover `RT` after the bridge enqueues a tool call.

Therefore the default bridge contract is:

> The bridge enqueues tool calls and waits for events. The subtree scheduler owns
> driving runnable threads.

If a caller uses a bare `ThreadRunner(db, T).run_once()` without a subtree
scheduler, runtime tool calls may remain pending. In that case the bridge should
time out with a diagnostic rather than silently starting a scheduler:

```text
Tool call is still pending. Runtime thread is runnable but was not driven.
Is a SubtreeScheduler running for this root?
```

A direct-drive helper can exist for tests/headless special cases, but it should
not be the default behavior.

---

## 6. Thread tree semantics

### 6.1 Spawn parentage

When a programmatic REPL call executes `spawn_agent`, the runtime thread is the
executing thread. The `ThreadRunner` for `RT` calls `ToolRegistry.execute` with:

```python
thread_id=RT
```

Existing `spawn_agent` then uses `_thread_id` as parent. Therefore the new child
is a child of `RT`:

```text
T
└── RT
    └── spawned-agent
```

If that spawned agent later uses a REPL/runtime thread, the tree naturally
recurses:

```text
T
└── RT
    └── spawned-agent
        └── RT2
            └── grandchild-agent
```

This satisfies the requirement that normal egg thread tree commands can inspect
the whole recursive execution.

### 6.2 Runtime thread naming

Suggested names:

```text
@runtime:python
@runtime:bash
@runtime:session
```

If multiple runtimes exist per application thread, include a stable suffix:

```text
@runtime:python:default
@runtime:bash:default
@runtime:python:data-analysis
```

Runtime threads should be discoverable via events rather than only name matching.

---

## 7. Session configuration events

### 7.1 Event type: `session.config`

Add event-sourced session config analogous to sandbox/tools config.

Example private session:

```json
{
  "type": "session.config",
  "payload": {
    "enabled": true,
    "provider": "docker",
    "image": "egg-rlm-session",
    "share": "private",
    "share_with_children_default": false,
    "workspace": "/workspace",
    "reason": "user"
  }
}
```

Example shared inherited session:

```json
{
  "type": "session.config",
  "payload": {
    "enabled": true,
    "provider": "docker",
    "share": "session",
    "session_id": "sess_01ABC...",
    "owner_thread_id": "01PARENT...",
    "workspace": "/workspace",
    "reason": "spawn_agent share_session=true"
  }
}
```

### 7.2 Event type: `runtime.config`

Add a lightweight event to connect application threads with runtime threads.

Example:

```json
{
  "type": "runtime.config",
  "payload": {
    "runtime_thread_id": "01RT...",
    "language": "python",
    "name": "default",
    "session_id": "sess_01ABC...",
    "reason": "created for python_repl"
  }
}
```

This avoids relying on child name alone.

### 7.3 Event type: `session.lifecycle`

Use lifecycle events for audit/debugging:

```json
{
  "type": "session.lifecycle",
  "payload": {
    "session_id": "sess_...",
    "action": "started",
    "container_name": "egg-rlm-...",
    "image": "egg-rlm-session"
  }
}
```

Actions:

```text
created
started
reattached
stopped
reset
error
```

These events should be appended to the runtime thread that owns/references the
session.

---

## 8. Docker session runtime

### 8.1 New module: `eggthreads/session.py`

Responsibilities:

- Resolve session config.
- Create/reuse runtime child threads.
- Start/reattach Docker containers.
- Execute Python/Bash REPL snippets through the container daemon.
- Provide status/reset/stop APIs.

Suggested public APIs:

```python
enable_thread_session(db, thread_id, *, language="python", image=None, share="private", ...)
disable_thread_session(db, thread_id, *, language="python", name="default")
get_thread_session_config(db, thread_id) -> SessionConfig
get_thread_session_status(db, thread_id) -> dict
get_or_create_runtime_thread(db, parent_thread_id, *, language="python", name="default") -> str
get_or_start_session(db, runtime_thread_id) -> SessionHandle
stop_thread_session(db, runtime_thread_id)
reset_thread_session(db, runtime_thread_id)
execute_python_repl(db, caller_thread_id, code, *, repl_name="default", timeout_sec=None) -> str
execute_bash_repl(db, caller_thread_id, script, *, repl_name="default", timeout_sec=None) -> str
```

### 8.2 Container naming

Use deterministic names:

```text
session_id:    sess_<ulid>
container:     egg-rlm-<dbhash>-<session_id>
```

Use labels:

```text
egg.kind=rlm-session
egg.session_id=<session_id>
egg.owner_thread_id=<thread_id>
egg.db_hash=<hash>
```

### 8.3 Container lifecycle

`get_or_start_session(...)` should:

1. Resolve effective session config.
2. Determine `session_id` and container name.
3. `docker inspect` container.
4. If running, reuse.
5. If stopped, start.
6. If missing, create with `docker run -d`.
7. Append lifecycle event.

### 8.4 Security constraints

- Do not mount `.egg/threads.sqlite` writable into the container.
- Prefer not to mount `.egg` at all.
- If the project root is mounted, overlay `.egg` read-only or mask it.
- Container code should call host bridge for Egg tools, not access SQLite.
- Use eval tokens/capability tokens for bridge calls.

---

## 9. In-container bridge and REPL helpers

### 9.1 `egg-sessiond`

Inside the container, run a small daemon responsible for:

- Python REPL channels.
- Bash REPL channels.
- stdout/stderr capture.
- timeout management.
- loading `eggtools` into Python REPLs.
- exposing `eggtool` shell command in Bash.

Possible implementation:

```text
eggthreads/session_runtime/sessiond.py
```

The host communicates with `sessiond` using one of:

- `docker exec` JSON RPC command
- Unix socket mounted between host/container
- stdio protocol through a long-lived `docker exec`

MVP can use `docker exec` for simplicity, then optimize later.

### 9.2 Python helper module: `eggtools.py`

Injected into Python REPL environment:

```python
from eggtools import (
    tool,
    spawn_agent,
    spawn_agent_auto,
    wait,
    web_search,
    fetch_url,
    bash,
    python,
    replace_between,
)
```

All helpers call the host bridge using an eval token:

```python
def tool(name: str, **kwargs):
    return _bridge_call(os.environ["EGG_EVAL_TOKEN"], name, kwargs)
```

Generated wrappers call `tool(...)`.

### 9.3 Bash helper: `eggtool`

Expose CLI:

```bash
eggtool web_search '{"query":"RLM", "max_results":5}'
eggtool spawn_agent '{"context_text":"Write me a story", "label":"story"}'
eggtool wait '{"thread_ids":["01..."]}'
```

Optional shell functions later:

```bash
spawn_agent() { eggtool spawn_agent "$@"; }
wait_threads() { eggtool wait "$@"; }
```

### 9.4 Eval tokens

For each outer REPL eval, the host creates an eval context:

```json
{
  "eval_token": "random-secret",
  "caller_thread_id": "T",
  "runtime_thread_id": "RT",
  "session_id": "sess_...",
  "allowed_tools": ["spawn_agent", "wait", "web_search"],
  "expires_at": "...",
  "outer_tool_call_id": "tc_parent_optional"
}
```

The container sends only `eval_token` to the host bridge. The host maps the
request to `RT`. This prevents a shared container from spoofing arbitrary thread
IDs.

---

## 10. Tool permissions and capability attenuation

### 10.1 Extend `tools.config`

Current config supports:

```json
{
  "llm_tools_enabled": true,
  "disable": ["tool_name"],
  "enable": ["tool_name"]
}
```

Add explicit allowlist support:

```json
{
  "allow_only": ["spawn_agent", "wait", "web_search", "fetch_url"]
}
```

Add clear operation:

```json
{
  "allow_only": null
}
```

Effective semantics:

```python
if allowed_tools is None:
    allowed = all_registry_tools
else:
    allowed = allowed_tools

allowed -= disabled_tools
```

`ToolsConfig` should include:

```python
allowed_tools: Optional[Set[str]] = None

def is_tool_allowed(name: str) -> bool: ...
```

### 10.2 Enforce at exposure and execution

`ThreadRunner` should use the same permission check for:

- RA1 tool spec exposure to LLM.
- RA2/RA3 tool execution.

A disabled/not-allowed tool should be handled as a synthetic finished tool call
with output like:

```text
Tool '<name>' is not allowed for this thread and was not executed.
```

### 10.3 Spawn attenuation

Extend `spawn_agent` and `spawn_agent_auto` schemas with optional child
capability controls:

```json
{
  "allowed_tools": ["web_search", "fetch_url", "wait"],
  "disabled_tools": ["bash"],
  "share_session": true
}
```

When parent/runtime thread requests child allowed tools:

```python
child_allowed = requested_allowed ∩ parent_effective_allowed
```

A child must never gain tools that the calling thread did not have.

After creating the child, append `tools.config` events to the child:

```json
{
  "type": "tools.config",
  "payload": {
    "allow_only": ["web_search", "fetch_url", "wait"]
  }
}
```

Then apply `disabled_tools` similarly.

### 10.4 Session sharing on spawn

If `share_session=True`, append a `session.config` event to the child or to its
runtime thread configuration path so the child's runtime reuses the session:

```json
{
  "type": "session.config",
  "payload": {
    "enabled": true,
    "share": "session",
    "session_id": "sess_parent",
    "owner_thread_id": "RT",
    "reason": "spawn_agent share_session=true"
  }
}
```

Default should be conservative:

```text
share_session=False unless explicit or parent config says share_with_children_default=true
```

---

## 11. New tools

### 11.1 `python_repl`

Model/user-facing tool that executes code in the caller thread's persistent
Python runtime.

Schema:

```json
{
  "name": "python_repl",
  "description": "Execute Python code in this thread's persistent Python REPL session.",
  "parameters": {
    "type": "object",
    "properties": {
      "code": { "type": "string" },
      "timeout_sec": { "type": "number" },
      "repl_name": { "type": "string" },
      "share_session": { "type": "boolean" }
    },
    "required": ["code"]
  }
}
```

Implementation outline:

1. Caller thread is `T` from `_thread_id`.
2. Resolve/create runtime child `RT` under `T`.
3. Ensure session/container exists for `RT`.
4. Execute code in container REPL with eval token bound to `RT`.
5. REPL programmatic tool calls enqueue TC calls on `RT` and wait for events.
6. Return stdout/stderr/result to `T` as the output of the outer `python_repl`
   tool call.

### 11.2 `bash_repl`

Same model for Bash.

Schema:

```json
{
  "name": "bash_repl",
  "description": "Execute Bash code in this thread's persistent Bash session.",
  "parameters": {
    "type": "object",
    "properties": {
      "script": { "type": "string" },
      "timeout_sec": { "type": "number" },
      "repl_name": { "type": "string" }
    },
    "required": ["script"]
  }
}
```

### 11.3 Session management tools

Add:

```text
session_status
session_reset
session_stop
```

These should act on the caller thread's runtime/session config.

---

## 12. Structured `wait`

Current `wait` returns human-readable text. For programmatic RLM, add a
structured shared implementation.

Core helper:

```python
wait_for_threads(
    db: ThreadsDB,
    thread_ids: list[str],
    *,
    timeout_sec: float | None = None,
) -> dict[str, ThreadWaitResult]
```

Dataclass:

```python
@dataclass
class ThreadWaitResult:
    thread_id: str
    finished: bool
    state: str
    last_assistant_message: str
    short_recap: str | None = None
```

The LLM-facing `wait` tool can format this as text. The Python `eggtools.wait`
wrapper can return:

```python
{
    thread_id: result.last_assistant_message
}
```

or, with `details=True`, the full structured result.

---

## 13. Scheduler changes

### 13.1 Current problem

`SubtreeScheduler` currently uses one semaphore/slot count for all runnable
threads. This conflates LLM API work with local/tool work.

### 13.2 Desired model

Add explicit resource accounting:

```python
resource_class = "llm" if ra.kind == "RA1_llm" else "tool"
```

Only `resource_class == "llm"` consumes the LLM concurrency limit.

### 13.3 Config changes

Current:

```python
RunnerConfig(max_concurrent_threads=8)
```

Add:

```python
RunnerConfig(
    max_concurrent_llm_threads: Optional[int] = None,
    max_concurrent_tool_threads: Optional[int] = None,
)
```

Compatibility:

- If `max_concurrent_llm_threads is None`, use `max_concurrent_threads`.
- Keep `max_concurrent_threads` as alias/backward-compatible field for now.
- UI/docs should gradually rename it to “max concurrent LLM turns”.

For MVP:

```text
LLM turns: limited.
Tool turns: not counted against LLM limit.
Tool turns: optionally unlimited or high-limit.
```

Be cautious with `max_concurrent_tool_threads`; a low global tool limit can
reintroduce RLM deadlocks if an orchestration tool waits on another tool call.

### 13.4 Scheduler pseudo-code

```python
running_threads: dict[str, str] = {}  # tid -> "llm" | "tool"

for tid in all_threads:
    if tid in running_threads:
        continue

    ra = discover_runner_actionable_cached(db, tid)
    if not ra:
        mark_checked(tid)
        continue

    kind = "llm" if ra.kind == "RA1_llm" else "tool"

    if kind == "llm" and running_llm_count >= max_concurrent_llm_threads:
        continue

    if kind == "tool" and tool_limit_enabled and running_tool_count >= max_concurrent_tool_threads:
        continue

    running_threads[tid] = kind
    asyncio.create_task(drive(tid, kind))
```

Inside `drive`, still use normal `ThreadRunner.run_once()`.

### 13.5 Thread state display

`thread_state()` can keep returning `"running"` for compatibility.

Add richer optional state later:

```text
running_llm
running_tool
waiting_tool_approval
waiting_output_approval
waiting_user
paused
```

---

## 14. Generic user tool-call enqueue API

Current APIs are bash-specific:

```python
execute_bash_command(...)
execute_bash_command_hidden(...)
```

Add generic helpers so REPL bridge, `/wait`, `$`, and future commands can reuse
one path.

```python
def enqueue_user_tool_call(
    db: ThreadsDB,
    thread_id: str,
    name: str,
    arguments: dict,
    *,
    content: str | None = None,
    hidden: bool = True,
    keep_user_turn: bool = True,
    origin: str = "user_command",
    auto_approve: bool = True,
    approval_reason: str | None = None,
) -> str:
    ...
```

Behavior:

- Append `msg.create role='user'` with `tool_calls`.
- Set `no_api=True` when hidden.
- Set `keep_user_turn=True` by default.
- Append `tool_call.approval decision='granted'` if `auto_approve=True`.
- Return `tool_call_id`.

Use this helper to refactor:

- `execute_bash_command`
- TUI `$` command
- TUI `/wait` command
- REPL bridge programmatic tool calls

This keeps the representation consistent.

---

## 15. REPL bridge algorithm

### 15.1 Outer `python_repl` tool execution

The normal `ThreadRunner` executes `python_repl` in caller thread `T`.

Implementation:

```python
def _python_repl_tool(args):
    caller_thread_id = args["_thread_id"]
    runtime_thread_id = get_or_create_runtime_thread(
        db,
        caller_thread_id,
        language="python",
        name=args.get("repl_name") or "default",
    )

    session = get_or_start_session(db, runtime_thread_id)

    eval_ctx = create_eval_context(
        caller_thread_id=caller_thread_id,
        runtime_thread_id=runtime_thread_id,
        session_id=session.session_id,
        allowed_tools=effective_allowed_tools(runtime_thread_id),
        outer_tool_call_id=args.get("_tool_call_id"),
    )

    return session.execute_python(
        code=args["code"],
        repl_name=..., 
        eval_token=eval_ctx.token,
        timeout_sec=..., 
    )
```

Note: `ToolRegistry.execute` currently injects `_thread_id`, timeout, model, and
cancel callback. It does not inject `_tool_call_id`; adding that context may be
useful for traceability, but is not strictly required for MVP.

### 15.2 Programmatic bridge call from container

When container code calls:

```python
web_search("RLM")
```

host bridge:

```python
ctx = resolve_eval_token(token)
rt = ctx.runtime_thread_id

authorize(rt, "web_search", args)

tcid = enqueue_user_tool_call(
    db,
    rt,
    "web_search",
    args,
    content="eggtools.web_search(...)" ,
    hidden=True,
    origin="repl",
    auto_approve=should_auto_approve(rt, "web_search"),
)

result = wait_for_tool_call_result(db, rt, tcid, timeout_sec=...)
return format_for_repl(result)
```

The bridge does not start a scheduler by default.

### 15.3 Pending approval

If a tool call is not auto-approved, the runtime thread enters
`waiting_tool_approval`.

The REPL bridge can support three modes:

```text
block       wait until approved/denied or timeout
raise       raise ToolApprovalPending immediately
return      return a PendingToolCall object
```

MVP can use `block` with timeout and clear diagnostics.

Example Python exception:

```python
class ToolApprovalPending(Exception):
    thread_id: str
    tool_call_id: str
```

---

## 16. Approval model

### 16.1 MVP default

Approving the outer `python_repl` tool call permits nested programmatic calls
only within the runtime thread's capability set, and those nested calls are
auto-approved.

This makes recursive programming usable.

### 16.2 Strict mode later

A runtime/session config can request strict nested approvals:

```json
{
  "nested_tool_approval": "prompt"
}
```

Modes:

```text
auto_within_acl  default MVP
prompt           require normal TC approval
never            deny all nested calls except maybe wait/status
```

---

## 17. File/code organization

Proposed new/modified files:

```text
eggthreads/eggthreads/session.py
    SessionConfig, RuntimeConfig, SessionManager, DockerSessionProvider
    get_or_create_runtime_thread, get_or_start_session, repl execution helpers

eggthreads/eggthreads/repl_bridge.py
    eval token registry, bridge call handler, programmatic tool call enqueue/wait

eggthreads/eggthreads/tools_config.py
    allow_only / allowed_tools support

eggthreads/eggthreads/api.py
    enqueue_user_tool_call
    wait_for_tool_call_result
    wait_for_threads structured helper

eggthreads/eggthreads/tools.py
    register python_repl, bash_repl, session_status/reset/stop
    extend spawn_agent/spawn_agent_auto args

eggthreads/eggthreads/runner.py
    scheduler LLM/tool resource split
    tool permission check uses ToolsConfig.is_tool_allowed
    optionally pass tool_call_id into ToolRegistry.execute context

eggthreads/eggthreads/session_runtime/sessiond.py
    container-side daemon

eggthreads/eggthreads/session_runtime/eggtools.py
    Python helper module

eggthreads/docker/Dockerfile
    include session runtime, eggtool helper, dependencies
```

TUI/Web optional follow-up:

```text
egg/egg/commands/session.py
eggw/eggw/commands/session.py
eggw/eggw/routes/settings.py or new routes/session.py
```

Commands:

```text
/sessionStatus
/sessionOn
/sessionOff
/sessionReset
/sessionStop
```

---

## 18. Implementation phases

### Phase 1: Capability model

- Add `allow_only` to `tools.config`.
- Add `ToolsConfig.allowed_tools` and `is_tool_allowed(name)`.
- Update RA1 tool exposure filtering.
- Update RA2/RA3 execution filtering.
- Add APIs:
  - `set_thread_tool_allowlist`
  - `clear_thread_tool_allowlist`
- Add tests for:
  - allowlist exposure
  - allowlist execution denial
  - disabled overrides allowlist

### Phase 2: Generic tool-call enqueue/wait helpers

- Add `enqueue_user_tool_call(...)`.
- Refactor bash user command helper to use it.
- Add `wait_for_tool_call_result(...)` and async version.
- Add structured `wait_for_threads(...)`.
- Refactor `wait` tool formatting to use structured helper.
- Add tests for:
  - generic enqueue creates TC2.1 when auto-approved
  - hidden/no_api behavior
  - waiting for TC6
  - timeout diagnostics

### Phase 3: Scheduler resource split

- Add config fields:
  - `max_concurrent_llm_threads`
  - optional `max_concurrent_tool_threads`
- Keep `max_concurrent_threads` compatibility.
- Modify `SubtreeScheduler` to classify RA kind before slot accounting.
- Ensure RA2/RA3 tools do not consume LLM slots.
- Add tests for:
  - RA1 limited by LLM slots
  - RA3 can run while RA1/tool thread is active
  - no deadlock with `max_concurrent_llm_threads=1`

### Phase 4: Runtime child threads

- Add `runtime.config` events.
- Add `get_or_create_runtime_thread(...)`.
- Runtime thread should inherit model/sandbox/working directory as appropriate,
  but set tools config for runtime behavior.
- Add tests for:
  - runtime child created under caller
  - runtime child reused
  - runtime thread appears in children list

### Phase 5: Session config and Docker lifecycle

- Add `session.config` events and `SessionConfig` resolver.
- Add Docker session provider with deterministic names/labels.
- Add status/stop/reset APIs.
- Use fake Docker provider in tests where possible.
- Add tests for:
  - config resolution/inheritance
  - session ID stability
  - lifecycle event emission

### Phase 6: Python REPL MVP

- Add `python_repl` tool.
- Add minimal session runtime that can execute Python code and preserve state.
- Inject `eggtools` helpers.
- Implement host bridge with eval tokens.
- Programmatic bridge calls enqueue RA3 tool calls on runtime thread and wait.
- Add tests with fake in-process session provider:
  - variable persists across eval calls
  - `eggtools.web_search` enqueues tool call on runtime thread
  - `eggtools.spawn_agent` creates child under runtime thread
  - bridge waits for TC6 via events

### Phase 7: Bash REPL MVP

- Add `bash_repl` tool.
- Add `eggtool` CLI in container.
- Preserve shell state where practical.
- Tests:
  - exported variable persists
  - `eggtool` bridge call works

### Phase 8: Spawn/session sharing

- Extend `spawn_agent`/`spawn_agent_auto` schemas:
  - `allowed_tools`
  - `disabled_tools`
  - `share_session`
- Implement capability attenuation.
- Implement session config propagation when sharing.
- Tests:
  - child allowed tools are subset of parent
  - child cannot gain denied tools
  - `share_session=True` attaches same session ID
  - default does not share unless configured

### Phase 9: UI/Web commands

- Add session status/control commands to `egg` TUI.
- Add session status/control routes to `eggw`.
- Show runtime children clearly in tree display.
- Optional filtering/collapsing of runtime threads in UI.

---

## 19. Testing strategy

### 19.1 Unit tests without Docker

Use fake session provider for most tests.

- Fake Python REPL can be implemented with `code.InteractiveConsole` in-process.
- Fake bridge can call `enqueue_user_tool_call` and wait helpers.
- Tool execution can use simple fake tools registered in `ToolRegistry`.

### 19.2 Scheduler tests

Construct a root with:

```text
T running tool-like work
RT runnable with RA3
max_concurrent_llm_threads=1
```

Assert RT can be scheduled because tool work does not consume LLM slots.

### 19.3 Integration tests with Docker

Mark as optional/slow and skip when Docker unavailable.

Test:

- container persists across calls
- Python variable persists
- Bash variable/function persists
- `.egg` is not writable/visible as intended
- `eggtools.spawn_agent` produces child under runtime thread

### 19.4 Recovery tests

Because callbacks are event-derived:

- enqueue tool call
- simulate bridge crash before result
- runner publishes result
- new bridge waits for same `tool_call_id`
- result is recovered

---

## 20. Open design questions

### 20.1 One runtime thread per language or per session?

Options:

1. `@runtime:python` and `@runtime:bash` separate threads.
2. One `@runtime:session` thread for both languages.

Recommendation for MVP:

- Use separate runtime threads by language/name to keep transcripts clear.

### 20.1.1 Should REPL channels be shareable?

Session sharing and REPL sharing should remain distinct.  The default remains:

```text
share Docker/container session: optional
share interpreter/REPL channel: no
```

However, an explicit future option such as `share_repl=True` can allow advanced
workflows where parent/child runtime threads intentionally use the same Python
or Bash interpreter channel.  This must be opt-in because it creates stronger
coupling, possible blocking interactions, and shared mutable language state.

Implementation note: `session.config` now has an explicit `share_repl` boolean.
When false (the default), the provider-level REPL channel name is scoped by the
runtime thread id even if multiple runtime threads reuse the same `session_id`.
When true, the requested `repl_name` is used directly so callers can knowingly
share interpreter state across runtime threads.

### 20.2 Should runtime threads be hidden/collapsed by default?

Runtime threads may be noisy. But they are valuable for audit.

Recommendation:

- Keep them real and visible initially.
- Add UI collapse/filter later.

### 20.3 Should nested calls require approval?

Recommendation:

- MVP: auto-approve within runtime capability ACL once outer REPL eval is approved.
- Later: strict prompt mode.

### 20.4 Should the bridge ever start a scheduler?

Recommendation:

- Default: no. Assume active root `SubtreeScheduler`.
- On timeout, produce diagnostic.
- Provide explicit direct-drive helper only for tests/headless special modes.

### 20.5 Should tool calls in runtime thread be visible to the model?

Recommendation:

- Default `no_api=True` for runtime messages.
- Parent thread receives summarized output of the outer `python_repl`/`bash_repl`
  tool call through normal TC output approval.

---

## 21. Example end-to-end flow

User asks main thread:

```text
Use Python to research RLM and delegate a summary to a child.
```

Assistant calls:

```json
python_repl({
  "code": """
from eggtools import web_search, spawn_agent, wait
hits = web_search('Recursive Language Models RLM')
tid = spawn_agent(context_text=f'Summarize these results:\n{hits}', label='rlm-summary')
res = wait([tid])
print(res[tid])
"""
})
```

Execution:

1. Main thread `T` runs outer `python_repl` tool.
2. `python_repl` creates/reuses `RT=@runtime:python` child.
3. Python code starts in persistent Docker session.
4. `web_search` call enqueues RA3 tool call on `RT`.
5. Scheduler runs `RT` RA3 through TC states.
6. Bridge returns search result to Python.
7. `spawn_agent` enqueues RA3 tool call on `RT`.
8. Scheduler runs it; existing spawn tool receives `thread_id=RT`.
9. New child `C=rlm-summary` appears under `RT`.
10. `wait([C])` enqueues RA3 wait call on `RT`.
11. Scheduler runs child `C` as needed; C's RA1 consumes LLM slot.
12. `wait` returns C's final answer to Python.
13. Python prints answer.
14. Outer `python_repl` returns printed answer as tool output to `T`.

Tree:

```text
T main task
└── RT @runtime:python
    └── C rlm-summary
```

Runtime transcript includes real TC states for:

```text
web_search
spawn_agent
wait
```

Main transcript includes the outer `python_repl` call/result.

---

## 22. Non-goals for MVP

- Perfect container security beyond existing sandbox/container practices.
- Complex distributed bridge architecture.
- UI collapse/filter polish.
- Strict nested approval mode.
- Multi-user remote bridge authentication beyond local eval tokens.
- Heavy tool-call thread fanout mode where each nested call gets its own child
  thread.

---

## 23. Summary

The design is:

1. A persistent REPL/session is represented by a real runtime child thread.
2. Outer model-facing `python_repl`/`bash_repl` tools run in the caller thread.
3. Programmatic REPL tool calls are enqueued as RA3 tool calls on the runtime
   child thread.
4. The existing `SubtreeScheduler`/`ThreadRunner` executes those tool calls
   through normal TC states.
5. The bridge waits for TC6 by watching the event log.
6. Scheduler slot accounting is split so only RA1/LLM turns consume scarce LLM
   slots; RA2/RA3 tool execution remains running but does not consume LLM slots.
7. `spawn_agent` from the REPL naturally creates children under the runtime
   thread, producing a recursive inspectable thread tree.
8. Tool capabilities are explicit and attenuate down the recursion tree.
9. Docker containers are persistent session state, but Egg events remain the
   source of truth and the host bridge remains the authority.
