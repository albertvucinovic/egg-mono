# eggthreads API Reference

This document provides a comprehensive API reference for the eggthreads library.

## Quick Start

```python
from eggthreads import (
    ThreadsDB,
    ThreadRunner,
    create_root_thread,
    append_message,
)

# Initialize database
db = ThreadsDB()
db.init_schema()

# Create a conversation thread
thread_id = create_root_thread(db, name="My Chat")

# Add a user message
append_message(db, thread_id, role="user", content="Hello!")

# Run the thread (requires eggllm for LLM integration)
runner = ThreadRunner(db, thread_id)
await runner.run()
```

## Core Concepts

### Threads and Branching

eggthreads organizes conversations as a tree of threads. Each thread maintains
its own message history and can spawn child threads for parallel exploration
or delegation.

- **Root threads**: Top-level conversations created with `create_root_thread()`
- **Child threads**: Branch conversations created with `create_child_thread()`
- **Subtrees**: All descendants of a thread, managed with `collect_subtree()`

### Event Sourcing

All state changes are recorded as immutable events in the database. This enables:

- Full audit trail of conversation history
- Snapshot rebuilding from events
- Continue/recovery from interrupted states

Key event types:
- `msg.create` - New message added
- `msg.edit` - Message content modified
- `tool_call.*` - Tool execution lifecycle
- `model.switch` - Model configuration changes
- `control.*` - Thread state control (pause, resume, interrupt)

### Tool Call Workflow

Tool calls follow a state machine:

1. **TC1**: Tool call requested, waiting for approval
2. **TC2.1**: Approved (ready to execute)
3. **TC2.2**: Denied (will not execute)
4. **TC3**: Executing
5. **TC4**: Finished, waiting for output approval
6. **TC5**: Output decision made, waiting for publish
7. **TC6**: Published (final tool message exists)

The output approval step (TC4 → TC5) allows review of tool results before
they're shown to the LLM. Use `build_tool_call_states()` to inspect current
tool call states.

### Runner Actionable States

The `ThreadRunner` responds to three actionable states:

- **RA1**: LLM turn needed (user message awaiting response)
- **RA2**: Tool execution needed (assistant tool calls pending)
- **RA3**: User command execution (user-initiated tool calls)

## Table of Contents

- [Thread Lifecycle](#thread-lifecycle)
- [Continue & Recovery](#continue--recovery)
- [Messages](#messages)
- [Thread Queries](#thread-queries)
- [Thread Control](#thread-control)
- [Model Configuration](#model-configuration)
- [Working Directory](#working-directory)
- [Tool Calls & Approval](#tool-calls--approval)
- [Tools Configuration](#tools-configuration)
- [User Commands (Bash)](#user-commands-bash)
- [Sandbox](#sandbox)
- [Token Statistics](#token-statistics)
- [Execution](#execution)
- [Database](#database)
- [Tool State & Runner Actionable](#tool-state--runner-actionable)
- [Tools Registry](#tools-registry)

---

## Thread Lifecycle

### `create_root_thread(db: 'ThreadsDB', name: 'Optional[str]' = None, initial_model_key: 'Optional[str]' = None, models_path: 'str' = 'models.json') -> 'str'`

Create a new root thread (top-level conversation).

A root thread has no parent and serves as the entry point for a
conversation tree. Child threads can be branched from it using
``create_child_thread()``.

Args:
    db: ThreadsDB instance for database operations.
    name: Optional human-readable name for the thread.
    initial_model_key: Model key to use for this thread. If None,
        defaults to the ``default_model`` from models.json.
    models_path: Path to models.json configuration file.

Returns:
    The new thread's unique ID (ULID format).

### `create_child_thread(db: 'ThreadsDB', parent_id: 'str', name: 'Optional[str]' = None, initial_model_key: 'Optional[str]' = None, models_path: 'str' = 'models.json') -> 'str'`

Create a child thread branching from a parent thread.

Child threads inherit the parent's model configuration by default
and are tracked in the parent-child relationship for subtree
operations.

Args:
    db: ThreadsDB instance for database operations.
    parent_id: ID of the parent thread to branch from.
    name: Optional human-readable name for the thread.
    initial_model_key: Model key to use for this thread. If None,
        inherits from the parent thread's current model.
    models_path: Path to models.json configuration file.

Returns:
    The new child thread's unique ID (ULID format).

### `delete_thread(db: 'ThreadsDB', thread_id: 'str') -> 'None'`

Delete a thread and cascade related rows via foreign keys.

Removes the thread from threads; ON DELETE CASCADE removes
- children rows that reference it (as parent or child)
- events rows for the thread
- open_streams row for the thread

### `duplicate_thread(db: 'ThreadsDB', source_thread_id: 'str', name: 'Optional[str]' = None) -> 'str'`

Duplicate a thread's event log into a new root thread.

This creates a new *root* thread whose events and snapshot are a
copy of ``source_thread_id`` at the time of invocation. The new
thread shares no open stream with the original (no rows are added
to ``open_streams``) but otherwise has identical history: all
``msg.create``, ``stream.*``, and ``tool_call.*`` events are
replayed with fresh event_ids, preserving msg_id and invoke_id so
that runner/actionable semantics (RA1/RA2/RA3, tool states, etc.)
behave as if the thread had been executed separately.

The duplicate is intended as a "checkpoint" copy: a frozen backup
of the conversation that can be inspected or resumed independently
of the original.

### `duplicate_thread_up_to(db: 'ThreadsDB', source_thread_id: 'str', up_to_msg_id: 'str', name: 'Optional[str]' = None) -> 'str'`

Duplicate a thread's event log up to a specific message.

Like duplicate_thread, but only copies events up to and including the
message with the given msg_id. This is useful for creating a checkpoint
at a specific point in the conversation.

Args:
    db: ThreadsDB instance
    source_thread_id: Thread to duplicate
    up_to_msg_id: Message ID to stop at (inclusive)
    name: Optional name for the new thread

Returns:
    The new thread's ID

---

## Continue & Recovery

### `continue_thread(db: 'ThreadsDB', thread_id: 'str', msg_id: 'Optional[str]' = None) -> 'ContinueResult'`

Continue a thread from a specific point or auto-detected continue point.

This function marks messages after the continue point with `skipped_on_continue=True`
via msg.edit events. The RA1 detection will ignore these messages, allowing the
thread to be re-run from the continue point.

Args:
    db: ThreadsDB instance
    thread_id: Thread to continue
    msg_id: Optional message ID to continue from. If None, auto-detect.

Returns:
    ContinueResult with details of the operation

### `continue_thread_async(db: 'ThreadsDB', thread_id: 'str', msg_id: 'Optional[str]' = None, delay_sec: 'Optional[float]' = None) -> 'ContinueResult'`

Async version of continue_thread with optional delay.

If delay_sec is specified, waits for the specified time before applying
the continue operation. This is useful for API rate limit scenarios where
you want to retry after a delay.

Args:
    db: ThreadsDB instance
    thread_id: Thread to continue
    msg_id: Optional message ID to continue from
    delay_sec: Optional delay in seconds before applying the continue.
               The thread will be picked up by the runner after this delay.

Returns:
    ContinueResult with details of the operation

### `find_continue_point(db: 'ThreadsDB', thread_id: 'str') -> 'Optional[str]'`

Auto-detect the best msg_id to continue from.

Searches backward through the thread to find an appropriate point to resume.
The algorithm prioritizes finding a stable state:

1. After the last published tool result (TC6 state) - safest point
2. After the last complete assistant message (with no pending tool calls)
3. After the last user message that doesn't have keep_user_turn

Returns:
    The msg_id to continue from, or None if the thread should continue
    from the very beginning (no messages to skip).

### `is_thread_continuable(db: 'ThreadsDB', thread_id: 'str') -> 'bool'`

Check if a thread can be continued.

A thread is continuable if:
- It exists
- It is not currently running (no active open_streams lease)
- There are messages after the last RA1 boundary that can be skipped

Note: A thread in 'waiting_user' state is technically continuable,
but /continue would effectively be a no-op.

### `diagnose_thread(db: 'ThreadsDB', thread_id: 'str') -> 'ThreadDiagnosis'`

Diagnose thread state and suggest fixes.

Checks for common issues:
1. Unclosed streams (interrupted streaming)
2. Unpublished tool calls (incomplete tool execution)
3. Consecutive assistant messages (API will reject)
4. Error messages at the end
5. Thread stuck in unexpected state

Returns a diagnosis with suggested continue point to fix issues.

### `ContinueResult`

Result of continue_thread operation.

**Fields:**

- `success`: `bool` (required)
- `continue_from_msg_id`: `Optional[str]` (required)
- `skipped_msg_ids`: `List[str]` (required)
- `message`: `str` (required)
- `diagnosis`: `Optional['ThreadDiagnosis']` (required)

### `ThreadDiagnosis`

Diagnosis of thread state for auto-fix.

**Fields:**

- `is_healthy`: `bool` (required)
- `issues`: `List[str]` (required)
- `suggested_continue_point`: `Optional[str]` (required)
- `details`: `Dict[str, Any]` (required)

---

## Messages

### `append_message(db: 'ThreadsDB', thread_id: 'str', role: 'str', content: 'str', extra: 'Optional[Dict[str, Any]]' = None) -> 'str'`

Append a user/assistant/system message to a thread.

This helper is intentionally thin: policy decisions about which
messages are sent to the provider (e.g. via ``no_api``) are handled
elsewhere, primarily in ``thread_state`` / ``discover_runner_actionable``
and ``ThreadRunner._sanitize_messages_for_api``.

### `edit_message(db: 'ThreadsDB', thread_id: 'str', msg_id: 'str', new_content: 'str', extra: 'Optional[Dict[str, Any]]' = None) -> 'None'`

Edit an existing message's content.

Appends a ``msg.edit`` event that updates the message content.
The original message is preserved in the event log for audit purposes.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread containing the message.
    msg_id: ID of the message to edit.
    new_content: New content to replace the existing content.
    extra: Optional additional payload fields for the edit event.

### `delete_message(db: 'ThreadsDB', thread_id: 'str', msg_id: 'str') -> 'None'`

Mark a message as deleted.

Appends a ``msg.delete`` event. The snapshot builder interprets
this to exclude the message from the reconstructed conversation.
The original message remains in the event log for audit purposes.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread containing the message.
    msg_id: ID of the message to delete.

### `create_snapshot(db: 'ThreadsDB', thread_id: 'str') -> 'str'`

Rebuild and persist the thread snapshot from events.

Processes all events for the thread and constructs a snapshot
representing the current conversation state. The snapshot is
stored in the threads table for fast access.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread to snapshot.

Returns:
    The snapshot JSON string.

---

## Thread Queries

### `list_threads(db: 'ThreadsDB') -> 'list[ThreadRow]'`

List all threads in the database.

Args:
    db: ThreadsDB instance for database operations.

Returns:
    List of ThreadRow objects for all threads.

### `list_root_threads(db: 'ThreadsDB') -> 'list[str]'`

List all root threads (threads with no parent).

Root threads are top-level conversations that were created with
``create_root_thread()`` and are not children of any other thread.

Args:
    db: ThreadsDB instance for database operations.

Returns:
    List of thread IDs for all root threads.

### `list_children_ids(db: 'ThreadsDB', parent_id: 'str') -> 'list[str]'`

List all direct child thread IDs for a parent thread.

Only returns immediate children, not grandchildren or deeper
descendants. Use ``collect_subtree()`` to get all descendants.

Args:
    db: ThreadsDB instance for database operations.
    parent_id: ID of the parent thread.

Returns:
    List of child thread IDs.

### `list_children_with_meta(db: 'ThreadsDB', parent_id: 'str') -> 'list[tuple[str, str, str, str]]'`

Return list of (child_id, name, short_recap, created_at) for a parent.

### `get_parent(db: 'ThreadsDB', child_id: 'str') -> 'Optional[str]'`

Get the parent thread ID for a child thread.

Args:
    db: ThreadsDB instance for database operations.
    child_id: ID of the child thread.

Returns:
    Parent thread ID, or None if the thread is a root thread
    or doesn't exist.

### `is_thread_runnable(db: 'ThreadsDB', thread_id: 'str') -> 'bool'`

Public API to check if a thread is runnable.

This now delegates to discover_runner_actionable so that the
ThreadRunner and external callers share the same notion of
runnable work (RA1/RA2/RA3).

### `collect_subtree(db: 'ThreadsDB', root_id: 'str') -> 'list[str]'`

Return all thread_ids in the subtree rooted at ``root_id`` (BFS).

### `list_active_threads(db: 'ThreadsDB', subtree: 'list[str]') -> 'list[str]'`

Return list of thread_ids that are currently running or runnable.

---

## Thread Control

### `pause_thread(db: 'ThreadsDB', thread_id: 'str', reason: 'str' = 'user') -> 'None'`

Pause a thread to prevent further execution.

Sets the thread status to 'paused' and emits a ``control.pause``
event. The runner will not process paused threads until they
are resumed with ``resume_thread()``.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread to pause.
    reason: Human-readable reason for pausing (default: 'user').

### `resume_thread(db: 'ThreadsDB', thread_id: 'str', reason: 'str' = 'user') -> 'None'`

Resume a paused thread to allow execution.

Sets the thread status to 'active' and emits a ``control.resume``
event. The runner will resume processing the thread if there is
actionable work pending.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread to resume.
    reason: Human-readable reason for resuming (default: 'user').

### `interrupt_thread(db: 'ThreadsDB', thread_id: 'str', reason: 'str' = 'user') -> 'Optional[str]'`

Hard-preempt current step by dropping the current lease.

Writers that gate on (thread_id, invoke_id) will fail on the next
heartbeat because the open_streams row for that (thread, invoke)
no longer exists. A new runner can immediately acquire a fresh
lease for the thread.

### `wait_subtree_idle(db: 'ThreadsDB', root_id: 'str', poll_sec: 'float' = 0.1, quiet_checks: 'int' = 3) -> 'None'`

Wait until no threads in the subtree are running or runnable for N checks.

---

## Model Configuration

### `set_thread_model(db: 'ThreadsDB', thread_id: 'str', model_key: 'str', reason: 'str' = 'user', concrete_model_info: 'Optional[Dict[str, Any]]' = None, models_path: 'str' = 'models.json') -> 'None'`

Append a model.switch event to a thread.

This is the authoritative record of model selection for a thread.
The ThreadRunner and UIs should not infer the active model from
message payloads; they should instead call current_thread_model(),
which uses these events.

If concrete_model_info is not provided, it will be computed from
models.json (if eggllm is available). If eggllm is not available,
the field will be omitted.

### `current_thread_model(db: 'ThreadsDB', thread_id: 'str') -> 'Optional[str]'`

Return the effective model for a thread.

Precedence:
  1. Most recent model.switch event (by event_seq) in this thread
     whose payload contains a non-empty model_key.
  2. threads.initial_model_key for this thread, if set and non-empty.
  3. None (caller may then fall back to the LLM client's default).

This helper must be the single source of truth for determining the
active model for a thread in eggthreads-based applications.

### `current_thread_model_info(db: 'ThreadsDB', thread_id: 'str') -> 'Optional[Dict[str, Any]]'`

Return the concrete_model_info dict from the most recent model.switch event.

Returns None if no model.switch event exists or if the payload lacks
concrete_model_info.

---

## Working Directory

### `get_thread_working_directory(db: 'ThreadsDB', thread_id: 'str') -> 'Path'`

Get the effective working directory for a thread.

Resolves the working directory by checking ``thread.config`` events
for this thread and its ancestors. If no explicit configuration
exists, returns the current process working directory.

Args:
    db: ThreadsDB instance for database operations.
    thread_id: ID of the thread.

Returns:
    Resolved Path to the thread's working directory.

### `set_thread_working_directory(db: 'ThreadsDB', thread_id: 'str', working_dir: 'str', reason: 'str' = 'user') -> 'None'`

Set the working directory for a thread.

The directory must be a subdirectory of the current process working directory.
It cannot be inside the .egg folder.

### `set_subtree_working_directory(db: 'ThreadsDB', root_thread_id: 'str', working_dir: 'str', reason: 'str' = 'user') -> 'None'`

Apply working directory configuration to all threads in a subtree.

---

## Tool Calls & Approval

### `approve_tool_calls_for_thread(db, thread_id, decision='all-in-turn', reason=None, tool_call_id=None)`

Approve tool calls for a thread with a given decision.

Creates a tool_call.approval event that can be used by the runner to
automatically approve tool calls according to the decision.

Args:
    db: ThreadsDB instance
    thread_id: target thread
    decision: one of 'all-in-turn', 'granted', 'denied', 'global_approval',
              'revoke_global_approval', 'prompt'
    reason: optional human-readable reason for the decision
    tool_call_id: optional specific tool call ID to approve/deny.
                  If omitted, the decision applies to the whole thread
                  (or to the current turn, depending on the decision).

### `list_tool_calls_for_thread(db: 'ThreadsDB', thread_id: 'str') -> 'List[ToolCallState]'`

Return ToolCallState objects for all tool calls in this thread.

### `list_tool_calls_for_message(db: 'ThreadsDB', thread_id: 'str', msg_id: 'str') -> 'List[ToolCallState]'`

Return ToolCallState objects for tool calls declared in a given message.

### `build_tool_call_states(db: 'ThreadsDB', thread_id: 'str') -> 'Dict[str, ToolCallState]'`

Scan events for a thread and reconstruct ToolCallState per tool_call_id.

This is intentionally stateless and computed on demand; threads are
typically small enough that this is acceptable, and it avoids schema
changes.

Note: This function respects the skipped_on_continue flag: tool calls
from messages that have been marked as skipped are not included.

---

## Tools Configuration

### `ToolsConfig`

Represents the effective tools configuration for a thread.

Attributes:
  - llm_tools_enabled: if False, RA1 will not expose tools to the
    LLM for this thread (``tools=None`` / ``tool_choice=None``).
  - disabled_tools: set of tool *names* (as used in ToolRegistry)
    that must not be exposed to the LLM and must not be executed
    when tool calls are processed (RA2/RA3).
  - has_explicit_config: True if at least one ``tools.config``
    event has been applied for this thread. This allows callers to
    decide whether to overlay model-level defaults when there is no
    explicit per-thread configuration.
  - allow_raw_tool_output: when False (default), tool outputs are
    *masked for secrets* when constructing the provider API request
    (see ThreadRunner._sanitize_messages_for_api). When True, tool
    outputs are sent to the provider as-is (still with control-char
    sanitization for safety).

    This flag does not prevent tool outputs from being stored in the
    local database or shown in the local UI; its primary purpose is
    to prevent accidental secret leakage to the LLM provider.

**Fields:**

- `llm_tools_enabled`: `bool` = `True`
- `disabled_tools`: `Set[str]` (default factory)
- `has_explicit_config`: `bool` = `False`
- `allow_raw_tool_output`: `bool` = `True`

### `get_thread_tools_config(db: 'ThreadsDB', thread_id: 'str') -> 'ToolsConfig'`

Return the effective ToolsConfig for a thread.

This walks ``tools.config`` events in order and applies their
payloads to an initially permissive configuration.

### `set_thread_tools_enabled(db: 'ThreadsDB', thread_id: 'str', enabled: 'bool') -> 'None'`

Enable or disable LLM tools for a thread (RA1 exposure).

When ``enabled`` is False, RA1 will stop exposing tools to the LLM
in this thread (``tools=None`` / ``tool_choice=None``), but
user-initiated commands (RA3) can still be modelled as tool calls
and executed locally according to per-tool disabled lists.

### `disable_tool_for_thread(db: 'ThreadsDB', thread_id: 'str', name: 'str') -> 'None'`

Mark a tool as disabled for this thread.

Disabled tools are hidden from the LLM and, when a tool call is
attempted (assistant- or user-originated), they are treated as
immediately finished with a synthetic "tool disabled" output
instead of being executed.

### `enable_tool_for_thread(db: 'ThreadsDB', thread_id: 'str', name: 'str') -> 'None'`

Remove a tool from the disabled set for this thread.

### `set_subtree_tools_enabled(db: 'ThreadsDB', root_thread_id: 'str', enabled: 'bool') -> 'None'`

Enable or disable LLM tools for all threads in a subtree.

This is a convenience wrapper around :func:`set_thread_tools_enabled`
that walks the subtree rooted at ``root_thread_id`` and appends a
``tools.config`` event for each thread.

### `disable_tool_for_subtree(db: 'ThreadsDB', root_thread_id: 'str', name: 'str') -> 'None'`

Disable a tool for all threads in a subtree.

### `enable_tool_for_subtree(db: 'ThreadsDB', root_thread_id: 'str', name: 'str') -> 'None'`

Enable a tool for all threads in a subtree.

### `set_thread_allow_raw_tool_output(db: 'ThreadsDB', thread_id: 'str', allow: 'bool') -> 'None'`

Enable or disable raw (unfiltered) tool output for a thread.

When ``allow`` is False (default), tool outputs are masked for
secret-like values when constructing provider API messages.

When True, tool outputs are sent to the provider without secret
masking (but still with control-character sanitization).

---

## User Commands (Bash)

### `execute_bash_command(db: 'ThreadsDB', thread_id: 'str', script: 'str', hidden: 'bool' = False) -> 'str'`

Execute a bash command as a user tool call (RA3).

This mimics the UI's $ (visible) and $$ (hidden) commands. It appends a
user message with a tool_call for the 'bash' tool, automatically approves
it, and returns the tool_call_id.

Args:
    db: ThreadsDB instance.
    thread_id: The thread where the command should be executed.
    script: Bash script to run.
    hidden: If True, the command is marked no_api and its output will not
            be shown to the LLM (corresponds to '$$').

Returns:
    The tool_call_id of the created tool call, which can be used to later
    retrieve the result via get_user_command_result.

### `execute_bash_command_hidden(db: 'ThreadsDB', thread_id: 'str', script: 'str') -> 'str'`

Convenience wrapper for execute_bash_command with hidden=True.

### `execute_bash_command_async(db: 'ThreadsDB', thread_id: 'str', script: 'str', hidden: 'bool' = False, timeout_sec: 'float' = 30.0, poll_interval: 'float' = 0.1) -> 'Optional[str]'`

Execute a bash command as a user tool call and wait for its result asynchronously.

Returns the tool message content if the tool call finishes within timeout_sec,
otherwise None.

### `get_user_command_result(db: 'ThreadsDB', thread_id: 'str', tool_call_id: 'str') -> 'Optional[str]'`

Retrieve the tool message content for a user command tool call.

Returns the content of the tool message that corresponds to the given
tool_call_id, if such a message has been published (state TC6). If the
tool call is not yet published, returns None.

Args:
    db: ThreadsDB instance.
    thread_id: The thread containing the tool call.
    tool_call_id: The tool call ID returned by execute_bash_command.

Returns:
    The content string of the tool message, or None if not yet published.

### `wait_for_user_command_result(db: 'ThreadsDB', thread_id: 'str', tool_call_id: 'str', timeout_sec: 'float' = 30.0, poll_interval: 'float' = 0.1) -> 'Optional[str]'`

Wait for a user command tool call to finish and return its result.

Polls the thread's tool call state until the tool call is published (TC6)
or the timeout expires. Returns the tool message content if published,
otherwise None.

Args:
    db: ThreadsDB instance.
    thread_id: The thread containing the tool call.
    tool_call_id: The tool call ID returned by execute_bash_command.
    timeout_sec: Maximum seconds to wait.
    poll_interval: Seconds between polls.

Returns:
    The content string of the tool message, or None on timeout.

### `wait_for_user_command_result_async(db: 'ThreadsDB', thread_id: 'str', tool_call_id: 'str', timeout_sec: 'float' = 30.0, poll_interval: 'float' = 0.1) -> 'Optional[str]'`

Async version of wait_for_user_command_result.

---

## Sandbox

### `wrap_argv_for_sandbox(argv: 'List[str]') -> 'List[str]'`

Backward-compatible convenience wrapper.

If called without thread context, we use the default settings
(``.egg/sandbox/default.json``) and the default enabled policy.

### `wrap_argv_for_sandbox_with_config(argv: 'List[str]', *, enabled: 'Optional[bool]', config_name: 'Optional[str]') -> 'List[str]'`

Backward compatible wrapper used by older callers.

We now store full settings JSON in thread events. This helper still
accepts a config name, loads that file (or default.json) and
delegates to :func:`wrap_argv_for_sandbox_with_settings`.

### `wrap_argv_for_sandbox_with_settings(argv: 'List[str]', *, enabled: 'bool', settings: 'Dict[str, object]', working_dir: 'Optional[str | Path]' = None, provider: 'Optional[str]' = None) -> 'List[str]'`

Wrap an argv for sandbox execution with explicit settings.

The provider can be specified via the ``provider`` argument or via a
"provider" key inside ``settings`` (default "docker").  If sandboxing is
disabled or the requested provider is unavailable, the original argv
is returned unchanged.

### `get_sandbox_status() -> 'Dict[str, object]'`

Return global sandbox *availability* status.

There is no process-wide sandbox configuration; this is intended
for UIs to show whether sandboxing can be effective when enabled in
a thread.

### `set_sandbox_globally_enabled(enabled: 'bool') -> 'None'`

Backward-compatible no-op.

Egg previously had a process-wide sandbox toggle. The current
architecture is thread/event based.

We keep this function so existing callers do not break, but it only
changes the default enable policy for threads *created in this
process* that do not have an explicit (or inherited) sandbox config.

### `set_sandbox_config(*, enabled: 'bool', config_name: 'Optional[str]' = None) -> 'None'`

Backward-compatible helper.

This does **not** implement a process-wide configuration anymore.
It only:

  * sets the default enabled policy for the process, and
  * validates that the named config exists under .egg/sandbox.

Callers that want a thread to actually use the config must store it
into the thread via :func:`set_thread_sandbox_config`.

### `get_thread_sandbox_config(db: "'ThreadsDB'", thread_id: 'str') -> 'ThreadSandboxConfig'`

Return the effective sandbox config for a thread.

Resolution order:

1) Latest ``sandbox.config`` event on the thread.
2) Latest ``sandbox.config`` event on the nearest ancestor.
3) ``.egg/sandbox/default.json`` (created if missing).

The returned config contains the full settings dict. Mandatory
protections (e.g. denying writes to ``.egg/sandbox``) are applied at
execution time when we write the *effective* settings file.

### `set_thread_sandbox_config(db: "'ThreadsDB'", thread_id: 'str', *, enabled: 'bool', config_name: 'Optional[str]' = None, settings: 'Optional[Dict[str, object]]' = None, provider: 'Optional[str]' = None, user_control_enabled: 'Optional[bool]' = None, reason: 'str' = 'user') -> 'None'`

Persist sandbox configuration for a thread.

This appends a ``sandbox.config`` event so that the effective
sandbox choice is reproducible across processes.

### `set_subtree_sandbox_config(db: "'ThreadsDB'", root_thread_id: 'str', *, enabled: 'bool', config_name: 'Optional[str]' = None, reason: 'str' = 'user') -> 'None'`

Apply sandbox configuration to all threads in a subtree.

### `get_thread_sandbox_status(db: "'ThreadsDB'", thread_id: 'str') -> 'Dict[str, object]'`

Return sandbox status for a specific thread.

This mirrors :func:`get_sandbox_status` but derives the configured
enabled/settings values from the thread's inherited
``sandbox.config`` event.

### `enable_user_sandbox_control(db: "'ThreadsDB'", thread_id: 'str', reason: 'Optional[str]' = None) -> 'None'`

Allow user commands /toggleSandboxing and /setSandboxConfiguration for this thread.

This is a thread-wide flag that can only be set programmatically via this API.
When disabled, the UI commands that modify sandbox configuration are blocked.

### `disable_user_sandbox_control(db: "'ThreadsDB'", thread_id: 'str', reason: 'Optional[str]' = None) -> 'None'`

Disallow user commands /toggleSandboxing and /setSandboxConfiguration for this thread.

This is a thread-wide flag that can only be set programmatically via this API.
When disabled, the UI commands that modify sandbox configuration are blocked.

### `is_user_sandbox_control_enabled(db: "'ThreadsDB'", thread_id: 'str') -> 'bool'`

Return True if user sandbox control commands are allowed for this thread.

Defaults to True (allowed) when no sandbox.config event exists.

---

## Token Statistics

### `snapshot_token_stats(snapshot: 'Dict[str, Any]') -> 'Dict[str, Any]'`

Compute approximate token statistics for a snapshot.

This is the same structure as before, but internally it is now
implemented via :func:`_token_stats_for_messages` so that the same
logic can be reused for streaming-tail token accounting.

### `streaming_token_stats(db: "'ThreadsDB'", thread_id: 'str') -> 'Dict[str, Any]'`

Compute token stats for the portion of the thread not in the snapshot.

This is meant for *live* monitoring.

It counts:
  * all ``msg.create`` events after the thread's last snapshot, and
  * any currently-streaming ``stream.delta`` events for the thread's
    active invoke.

The output schema matches :func:`snapshot_token_stats`.

Notes
-----
* This is best-effort and approximate.
* When a turn is streaming, we synthesize an in-memory assistant
  message from accumulated deltas.

### `total_token_stats(db: "'ThreadsDB'", thread_id: 'str', *, llm: 'Any' = None) -> 'Dict[str, Any]'`

Return snapshot+streaming token stats (optionally including cost).

This is the recommended helper for UIs that want a single structure
describing the thread's approximate context length and token usage.

If ``llm`` is provided (e.g. an ``eggllm.LLMClient`` instance), we also
attach an approximate USD cost estimate under ``api_usage.cost_usd``.

The merged result is conceptually:

  total ~= snapshot_token_stats + streaming_token_stats

(with careful handling of cached_tokens / last-call metadata).

### `word_count_from_snapshot(db: 'ThreadsDB', thread_id: 'str') -> 'int'`

Return the word count of all messages in the thread snapshot.

### `word_count_from_events(db: 'ThreadsDB', thread_id: 'str') -> 'int'`

Return word count of thread, including events after last snapshot.

---

## Execution

### `ThreadRunner(db: 'ThreadsDB', thread_id: 'str', llm: 'Optional[LLMClient]' = None, owner: 'Optional[str]' = None, purpose: 'str' = 'assistant_stream', config: 'Optional[RunnerConfig]' = None, models_path: 'Optional[str]' = None, all_models_path: 'Optional[str]' = None, tools: 'Optional[ToolRegistry]' = None)`

Runs a single thread by acquiring the per-thread lease (open_streams with invoke_id fence)
and streaming assistant output.

### `SubtreeScheduler(db: 'ThreadsDB', root_thread_id: 'str', llm: 'Optional[LLMClient]' = None, owner: 'Optional[str]' = None, config: 'Optional[RunnerConfig]' = None, models_path: 'Optional[str]' = None, all_models_path: 'Optional[str]' = None, tools: 'Optional[ToolRegistry]' = None)`

Async orchestrator: watches a root thread and runs runnable threads within its subtree, up to concurrency limit.

### `RunnerConfig`

RunnerConfig(lease_ttl_sec: 'int' = 10, heartbeat_sec: 'float' = 1.0, max_concurrent_threads: 'int' = 4)

**Fields:**

- `lease_ttl_sec`: `int` = `10`
- `heartbeat_sec`: `float` = `1.0`
- `max_concurrent_threads`: `int` = `4`

---

## Database

### `ThreadsDB(db_path: 'Path | str' = PosixPath('.egg/threads.sqlite'))`

Thin DB layer adhering to ../egg/SQLITE_PLAN_CLEAN.md schema.

### `ThreadRow`

ThreadRow(thread_id: 'str', name: 'Optional[str]', short_recap: 'str', status: 'str', snapshot_json: 'Optional[str]', snapshot_last_event_seq: 'int', initial_model_key: 'Optional[str]', depth: 'int', created_at: 'str')

**Fields:**

- `thread_id`: `str` (required)
- `name`: `Optional[str]` (required)
- `short_recap`: `str` (required)
- `status`: `str` (required)
- `snapshot_json`: `Optional[str]` (required)
- `snapshot_last_event_seq`: `int` (required)
- `initial_model_key`: `Optional[str]` (required)
- `depth`: `int` (required)
- `created_at`: `str` (required)

---

## Tool State & Runner Actionable

### `thread_state(db: 'ThreadsDB', thread_id: 'str') -> 'str'`

Coarse thread state used by tools and UIs.

Returns one of:
  - "running"                 (streaming or runnable RA present)
  - "waiting_tool_approval"   (TC1 exists, no RA)
  - "waiting_output_approval" (TC4 exists, no RA)
  - "waiting_user"            (idle, waiting for user input)
  - "paused"                  (thread.status == 'paused')

### `discover_runner_actionable(db: 'ThreadsDB', thread_id: 'str') -> 'Optional[RunnerActionable]'`

Determine the next actionable work item for a thread.

This function encapsulates the Runner Actionables (RA1/RA2/RA3) logic
based on the event log and tool call states.

RA1 (LLM) uses messages *after* the last stream.close, while RA2/RA3
operate directly on tool call states so that they can act on tool
calls whose parent message may have been emitted before the last
stream.close (e.g. assistant tool calls created by a prior LLM turn
or user commands that have already finished execution).

---

## Tools Registry

### `ToolRegistry()`

Simple registry for OpenAI function-call compatible tools.

- tools_spec() returns the JSON schema list to pass to the LLM
- execute(name, arguments) dispatches to the registered callable

### `create_default_tools() -> 'ToolRegistry'`

Create a ToolRegistry with the default set of tools.

Returns a registry pre-populated with common tools:
- bash: Execute shell commands
- python: Execute Python scripts
- javascript: Browser JavaScript execution (placeholder)
- spawn_agent: Create child threads for delegation
- spawn_agent_auto: Create auto-approved child threads
- replace_between: File text replacement
- search_tavily: Web search via Tavily API
- wait: Synchronize on child thread completion

Returns:
    ToolRegistry with default tools registered.

---
