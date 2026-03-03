# System Design: Crash Recovery in eggthreads + eggflow

This document describes the crash recovery design for systems using eggthreads (conversation threads) with eggflow (task orchestration).

## Overview

When an LLM conversation is interrupted (crash, Ctrl+C, process kill), the thread may be left in an inconsistent state. The recovery system must:

1. Detect the inconsistent state
2. Clean up partial work
3. Reset boundaries so the LLM can retry
4. Be idempotent (safe to run multiple times)

## Key Concepts

### RA1 Boundary

The **RA1 boundary** determines which user/tool messages trigger a new LLM turn. It's computed by `_last_stream_close_seq()` in `tool_state.py`:

- `stream.close` events from LLM streams advance the boundary
- `control.interrupt` events with `purpose='llm'` advance the boundary
- `control.interrupt` events with `purpose='continue'` reset the boundary to BEFORE the continue point

Messages after the boundary trigger RA1 (LLM call). Messages at or before the boundary are considered "answered".

### Thread Health vs Thread Completion

**Thread health** (checked by `diagnose_thread`):
- Structural issues: unclosed streams, unpublished tool calls
- A thread can be "healthy" but incomplete (no assistant response yet)

**Thread completion** (what tasks expect):
- The expected response exists (e.g., assistant message)
- Semantic correctness, not just structural

This distinction matters for recovery: a thread may be structurally healthy but have its RA1 boundary in the wrong place.

### Lease Expiration

Runners hold leases on threads via the `open_streams` table. Leases expire after `lease_ttl_sec` (default 10s) if not renewed via heartbeat.

After a crash:
- The lease row remains in `open_streams` with an expired `lease_until`
- All lease checks must compare `lease_until > now_iso`
- Expired leases should be treated as "no lease"

**Functions that check lease expiration:**
- `continue_thread` - Only blocks if lease is active
- `is_thread_continuable` - Only returns False if lease is active
- `list_active_threads` - Only counts thread as "running" if lease is active
- `SubtreeScheduler.run_forever` - Skips threads with active leases held by others
- `try_open_stream` - Takes over expired leases instead of failing on UNIQUE constraint

**Critical:** If any of these functions forget to check expiration, threads can get stuck in limbo (neither running nor idle).

### Lease Takeover in try_open_stream

After a crash, expired lease rows remain in `open_streams`. When a new runner tries to acquire a lease:

1. **First try UPDATE** - Take over an expired lease (most common case after crash)
2. **Check for active lease** - Return False if another process holds it
3. **INSERT if no row** - Only insert when the thread has never had a lease

This order prevents `UNIQUE constraint failed` errors when recovering threads with expired leases.

## Recovery Flow

### In eggflow (Task Level)

```
Task FAILED → eggflow re-executes → recover() called → returns bool
                                                        ↓
                                         True → run() executes
                                         False → stays FAILED
```

The `recover()` method on `Task` is called before re-running a FAILED task. It returns:
- `True`: State fixed, retry should succeed
- `False`: Permanent failure, don't retry

### In eggthreads (Thread Level)

```python
# PICTask._ensure_thread_healthy()
diagnosis = diagnose_thread(db, thread_id)
if not diagnosis.is_healthy:
    result = continue_thread(db, thread_id, diagnosis.suggested_continue_point)
```

For tasks expecting LLM responses (like `WaitForLLMResponse`), additional recovery:

```python
# Even if healthy, ensure RA1 boundary allows the user message to trigger LLM
if last_user_msg_id:
    continue_thread(db, thread_id, last_user_msg_id)
```

## Why `continue_thread` with Explicit msg_id

When `continue_thread(db, thread_id, msg_id)` is called with an explicit `msg_id`:

1. **Skips health check**: The `if msg_id is None` branch (which returns early for healthy threads) is not taken
2. **Marks messages as skipped**: Messages after `msg_id` get `skipped_on_continue=True`
3. **Emits boundary reset**: `control.interrupt` with `purpose='continue'` and `continue_from_msg_id`
4. **RA1 sees reset**: `_last_stream_close_seq` sets boundary to `msg_seq - 1`

This ensures the user message becomes visible to RA1 detection again.

## Common Scenarios

### Scenario 1: Crash During LLM Streaming

**State after crash:**
- `stream.open` event exists (no matching `stream.close`)
- Partial assistant message may exist
- Lease in `open_streams` (will expire)

**Recovery:**
1. `diagnose_thread` detects unclosed streams → `is_healthy=False`
2. `suggested_continue_point` = last user message
3. `continue_thread` marks partial response as skipped, resets RA1 boundary
4. Scheduler re-triggers LLM for the user message

### Scenario 2: Crash Before LLM Started

**State after crash:**
- User message exists, no assistant response
- No `stream.open` events
- Thread appears "healthy"

**Recovery:**
1. `diagnose_thread` sees no issues → `is_healthy=True`
2. But RA1 boundary might be wrong (previous `control.interrupt` with `purpose='llm'`)
3. `recover()` explicitly calls `continue_thread(db, tid, user_msg_id)`
4. Boundary reset, scheduler triggers LLM

### Scenario 3: Crash During Tool Execution

**State after crash:**
- Tool call exists but unpublished (TC2-TC5 state)
- May have partial output

**Recovery:**
1. `diagnose_thread` detects unpublished tool calls → `is_healthy=False`
2. `continue_thread` marks incomplete tool response as skipped
3. Tool call appears unanswered, RA2 or RA1 re-triggers

## Design Principles

1. **No special cases in eggthreads**: Recovery uses the same APIs as normal operation (`diagnose_thread`, `continue_thread`)

2. **Lease expiration via heartbeat**: Don't need `interrupt_thread` for crash recovery - leases expire naturally

3. **Idempotent recovery**: Calling `continue_thread` multiple times is safe (already-skipped messages are ignored)

4. **Separation of concerns**:
   - `diagnose_thread`: Detect structural issues
   - `continue_thread`: Fix issues and reset boundaries
   - `recover()`: Task-specific cleanup logic

## API Reference

### `diagnose_thread(db, thread_id) -> ThreadDiagnosis`

Checks for:
- Unclosed streams
- Unpublished tool calls
- Consecutive assistant messages
- Error messages at the end

Returns `ThreadDiagnosis(is_healthy, issues, suggested_continue_point, details)`.

### `continue_thread(db, thread_id, msg_id=None) -> ContinueResult`

If `msg_id` is None: auto-detect continue point, return early if healthy.
If `msg_id` is provided: always reset boundary to before that message.

Emits `control.interrupt` with `purpose='continue'` to reset RA1 boundary.

### `is_thread_continuable(db, thread_id) -> bool`

Returns True if thread can be continued (exists, no active lease).
Checks lease expiration - expired leases don't block.

## Testing Recovery

See `tests/test_continue_thread.py` for recovery test cases covering:
- Crash during streaming
- Crash with expired lease
- Multiple recovery attempts
- RA1 boundary reset scenarios
