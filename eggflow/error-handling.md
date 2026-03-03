# Error Handling in eggflow

This document explains eggflow's error handling model, with a focus on **terminal errors** - a mechanism for handling non-recoverable failures that should never be silently swallowed.

## Table of Contents

1. [The Problem](#the-problem)
2. [Solution: Terminal Errors](#solution-terminal-errors)
3. [How It Works](#how-it-works)
4. [API Reference](#api-reference)
5. [Examples](#examples)
6. [Creating Custom Terminal Errors](#creating-custom-terminal-errors)
7. [Best Practices](#best-practices)

---

## The Problem

In eggflow, `wrapped()` converts task failures into `Result` objects instead of raising exceptions. This is useful for graceful error handling:

```python
result = yield wrapped(SomeTask())
if result.is_success:
    process(result.value)
else:
    handle_error(result.error)
```

However, this creates a problem with **non-recoverable errors** like context limit exceeded:

```python
# BAD: Must check for terminal errors EVERYWHERE (easy to forget!)
def run(self):
    result = yield wrapped(SomeTask())
    if not result.is_success:
        if 'context limit' in result.error.lower():  # Fragile string matching!
            return FailureResult(error=result.error)
        # Handle other errors...

    # Later in the same method...
    result2 = yield wrapped(AnotherTask())
    if not result2.is_success:
        if 'context limit' in result2.error.lower():  # Must remember AGAIN!
            return FailureResult(error=result2.error)
        # ...
```

**Problems with this approach:**

1. **Easy to forget**: Miss one check → infinite loop (task keeps retrying)
2. **Fragile**: Relies on string matching in error messages
3. **Verbose**: Same boilerplate repeated everywhere
4. **Error-prone**: Different code paths may handle it differently

---

## Solution: Terminal Errors

Terminal errors are marked as **non-recoverable** and automatically propagate through `wrapped()` calls. They can only be caught with explicit `try/except`.

```python
# GOOD: Handle terminal errors ONCE at the top level
def run(self):
    try:
        return (yield from self._main_logic())
    except TaskError as e:
        if e.is_terminal:
            return FailureResult(error=str(e))
        raise

def _main_logic(self):
    # No manual checking needed! Terminal errors auto-propagate.
    yield wrapped(SomeTask())
    yield wrapped(AnotherTask())
    yield wrapped(YetAnotherTask())
    return SuccessResult()
```

**Benefits:**

1. **Impossible to forget**: Terminal errors propagate automatically
2. **Type-safe**: Uses exception types, not string matching
3. **Clean code**: Handle once at the appropriate level
4. **No infinite loops**: Executor never retries terminal errors

---

## How It Works

### Normal Errors vs Terminal Errors

| Aspect | Normal Errors | Terminal Errors |
|--------|---------------|-----------------|
| `wrapped()` behavior | Returns `Result` with error | **Raises** `TaskError` |
| `recover()` called? | Yes, task may retry | **No**, never retries |
| Handling | Check `result.is_success` | Use `try/except TaskError` |
| Propagation | Stops at `wrapped()` | Punches through `wrapped()` |

### Flow Diagram

```
Task raises ContextLimitExceededError
                ↓
    _execute_task catches it
                ↓
    Creates Result(error=..., terminal=True)
                ↓
    Stores in cache as FAILED
                ↓
    Returns Result to caller
                ↓
        ┌───────────────────────────────────┐
        │  Is caller using wrapped()?       │
        └───────────────────────────────────┘
                ↓                    ↓
              Yes                   No
                ↓                    ↓
    ┌─────────────────────┐    Raises TaskError
    │ Is result.terminal? │    (normal behavior)
    └─────────────────────┘
          ↓           ↓
        True        False
          ↓           ↓
    Raises TaskError  Returns Result
    (punches through) (normal wrapped behavior)
```

### What Happens on Re-execution

When a task with a terminal error is re-executed:

1. Executor loads cached `Result` from database
2. Checks `cached_result.is_terminal`
3. If terminal: **Returns immediately** (skips `recover()`)
4. If not terminal: Calls `recover()`, may retry

```python
# In FlowExecutor._execute_task:
if row and row['status'] == "FAILED":
    cached_result = pickle.loads(row['result_blob'])

    if cached_result and cached_result.is_terminal:
        # Terminal error - never retry, return cached failure immediately
        return cached_result

    # Non-terminal: call recover(), potentially retry
    # ...
```

---

## API Reference

### `TaskError`

Exception raised when a task fails.

```python
class TaskError(Exception):
    def __init__(self, message: str, result: Result, terminal: bool = False):
        self.result = result      # The Result object
        self.terminal = terminal  # Is this terminal?

    @property
    def is_terminal(self) -> bool:
        """Check if this is a terminal error that should not be retried."""
        return self.terminal
```

### `Result.is_terminal`

Property to check if a Result contains a terminal error.

```python
@property
def is_terminal(self) -> bool:
    """Check if this is a terminal error that auto-propagates through wrapped()."""
    return self.error is not None and self.terminal
```

### `Result.terminal`

Field on Result dataclass.

```python
@dataclass
class Result:
    value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    terminal: bool = False  # If True, error is terminal (non-recoverable)
```

### `_is_terminal_exception(e)`

Internal helper to detect terminal exceptions.

```python
def _is_terminal_exception(e: Exception) -> bool:
    """Check if an exception type is inherently terminal."""
    # Checks for ContextLimitExceededError
    # Checks for terminal=True attribute on exception
```

---

## Examples

### Example 1: Basic Terminal Error Handling

```python
from eggflow import Task, wrapped, TaskError

@dataclass
class SolveWithRetries(Task):
    max_attempts: int = 5

    def run(self):
        try:
            return (yield from self._solve_loop())
        except TaskError as e:
            if e.is_terminal:
                # Terminal error - return graceful failure
                return SolveResult(
                    success=False,
                    error=f"Terminal error: {e}"
                )
            # Non-terminal TaskError - re-raise for normal handling
            raise

    def _solve_loop(self):
        for attempt in range(self.max_attempts):
            # Terminal errors auto-propagate through wrapped()
            result = yield wrapped(AttemptSolution(attempt=attempt))

            if result.is_success:
                return SolveResult(success=True, value=result.value)

            # Non-terminal error - try again
            continue

        return SolveResult(success=False, error="Max attempts exceeded")
```

### Example 2: Nested Task Composition

```python
@dataclass
class OuterTask(Task):
    def run(self):
        try:
            # Terminal errors from ANY nested task propagate here
            step1 = yield wrapped(Step1Task())
            step2 = yield wrapped(Step2Task())
            step3 = yield wrapped(Step3Task())
            return CombinedResult(step1.value, step2.value, step3.value)
        except TaskError as e:
            if e.is_terminal:
                return FailedResult(error=str(e))
            raise

@dataclass
class Step2Task(Task):
    def run(self):
        # This task yields more subtasks
        # Terminal errors propagate through ALL layers
        a = yield wrapped(SubTaskA())
        b = yield wrapped(SubTaskB())  # If this has terminal error...
        c = yield wrapped(SubTaskC())  # ...this never runs
        return Step2Result(a.value, b.value, c.value)
```

### Example 3: Selective Terminal Error Handling

```python
@dataclass
class SmartSolver(Task):
    def run(self):
        try:
            return (yield from self._try_fast_path())
        except TaskError as e:
            if e.is_terminal:
                # Try fallback strategy for terminal errors
                return (yield from self._try_fallback())
            raise

    def _try_fast_path(self):
        # May hit context limit with large inputs
        result = yield wrapped(FastButExpensiveTask())
        return result.value

    def _try_fallback(self):
        # Fallback uses less context
        result = yield wrapped(SlowButCheapTask())
        return result.value
```

### Example 4: Distinguishing Error Types

```python
@dataclass
class RobustTask(Task):
    def run(self):
        try:
            result = yield wrapped(UnreliableTask())
            if not result.is_success:
                # Non-terminal error - we can handle it
                return self._handle_recoverable_error(result.error)
            return result.value

        except TaskError as e:
            if e.is_terminal:
                # Terminal error - cannot recover, must fail gracefully
                return FailureResult(
                    error=str(e),
                    is_permanent=True
                )
            # Unexpected non-terminal TaskError
            raise
```

---

## Creating Custom Terminal Errors

To create your own terminal error type:

```python
class MyTerminalError(Exception):
    """Custom terminal error for my application."""
    terminal = True  # Class attribute

    def __init__(self, message: str):
        super().__init__(message)
        self.terminal = True  # Instance attribute (belt and suspenders)
```

The executor checks for terminal errors using:

```python
def _is_terminal_exception(e: Exception) -> bool:
    # Check for known terminal types
    if isinstance(e, ContextLimitExceededError):
        return True

    # Check for terminal attribute
    return getattr(e, 'terminal', False)
```

### Built-in Terminal Errors

Currently, these exceptions are terminal:

| Exception | Module | Description |
|-----------|--------|-------------|
| `ContextLimitExceededError` | `eggflow.eggthreads_tasks` | Thread exceeded context token limit |

---

## Best Practices

### 1. Handle Terminal Errors at the Right Level

Handle terminal errors at the level where you can take meaningful action:

```python
# GOOD: Handle at task boundary where you can return appropriate result
@dataclass
class TopLevelTask(Task):
    def run(self):
        try:
            return (yield from self._do_work())
        except TaskError as e:
            if e.is_terminal:
                return GracefulFailure(error=str(e))
            raise
```

### 2. Don't Catch Terminal Errors Too Early

```python
# BAD: Catching too early loses context
def _do_subtask(self):
    try:
        yield wrapped(SomeTask())
    except TaskError as e:
        if e.is_terminal:
            pass  # Swallowed! Caller doesn't know what happened
```

### 3. Use Terminal Errors for True Non-Recoverables

Terminal errors should represent situations where:
- Retry will definitely fail again
- Continuing would waste resources
- The error represents an intentional limit (not a transient failure)

```python
# GOOD candidates for terminal errors:
# - Context/token limit exceeded
# - Rate limit with no retry window
# - Authentication permanently revoked
# - Resource permanently deleted

# BAD candidates for terminal errors:
# - Network timeout (transient)
# - Temporary rate limit (can retry later)
# - Validation error (might be fixed by user)
```

### 4. Proactive Checks Are Still Useful

Even with auto-propagation, proactive checks can save resources:

```python
def run(self):
    # Proactive check - fail fast before expensive LLM call
    if self._is_near_context_limit():
        return EarlyFailure(error="Near context limit")

    # This might raise terminal error, but we saved the LLM call
    yield wrapped(ExpensiveLLMTask())
```

### 5. Test Terminal Error Propagation

```python
# Test that terminal errors propagate correctly
def test_terminal_error_propagates():
    class TerminalFailingTask(Task):
        async def run(self):
            raise ContextLimitExceededError("Test limit exceeded")

    class OuterTask(Task):
        def run(self):
            # This should raise, not return Result
            result = yield wrapped(TerminalFailingTask())
            return result  # Should never reach here

    with pytest.raises(TaskError) as exc_info:
        await executor.run(OuterTask())

    assert exc_info.value.is_terminal
```

---

## Migration Guide

If you have existing code with manual context limit checks:

### Before (Manual Checking)

```python
result = yield wrapped(SomeTask())
if not result.is_success:
    if 'context limit' in result.error.lower():
        return FailureResult(error=result.error)
    raise TaskError(result.error, result)
```

### After (Auto-Propagation)

```python
# In run():
try:
    return (yield from self._main_logic())
except TaskError as e:
    if e.is_terminal:
        return FailureResult(error=str(e))
    raise

# In _main_logic():
result = yield wrapped(SomeTask())
if not result.is_success:
    # Only non-terminal errors reach here
    raise TaskError(result.error, result)
```

---

## Summary

| Concept | Description |
|---------|-------------|
| **Terminal Error** | Non-recoverable error that auto-propagates through `wrapped()` |
| **`Result.is_terminal`** | Check if Result contains terminal error |
| **`TaskError.is_terminal`** | Check if TaskError is terminal |
| **`terminal=True`** | Attribute on exceptions/results marking them as terminal |
| **Behavior** | Terminal errors RAISE through `wrapped()`, skip `recover()` |
| **Handling** | Use `try/except TaskError` with `e.is_terminal` check |

**Key Principle**: Safe by default, escapable when needed. Terminal errors propagate automatically, but you can always catch them with `try/except` when you know what you're doing.
