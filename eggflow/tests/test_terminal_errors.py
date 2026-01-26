"""Tests for terminal error propagation in eggflow.

Terminal errors are non-recoverable errors (like context limit exceeded) that
automatically propagate through wrapped() calls. They can only be caught with
explicit try/except, and the executor never retries them.

These tests verify:
1. Terminal errors propagate through wrapped() calls (raise instead of return Result)
2. Non-terminal errors still return Result (normal wrapped() behavior)
3. Terminal errors skip recover() and return cached failure immediately
4. ContextLimitExceededError is properly detected as terminal
5. Custom terminal exceptions with terminal=True attribute work
6. is_terminal property works on both Result and TaskError
"""

import asyncio
import pickle
from dataclasses import dataclass
from typing import List

import pytest

from eggflow import Task, Result, wrapped, TaskError
from eggflow.core import _is_terminal_exception
from eggflow.eggthreads_tasks import ContextLimitExceededError


# =============================================================================
# Test Tasks
# =============================================================================

@dataclass
class SuccessTask(Task):
    """A task that always succeeds."""
    value: str = "success"

    async def run(self):
        return self.value


@dataclass
class NormalFailingTask(Task):
    """A task that fails with a normal (non-terminal) error."""
    error_message: str = "Normal failure"

    async def run(self):
        raise ValueError(self.error_message)


@dataclass
class TerminalFailingTask(Task):
    """A task that fails with a terminal error (ContextLimitExceededError)."""
    error_message: str = "Context limit exceeded"

    async def run(self):
        raise ContextLimitExceededError(self.error_message)


class CustomTerminalError(Exception):
    """Custom exception marked as terminal via attribute."""
    terminal = True

    def __init__(self, message: str):
        super().__init__(message)
        self.terminal = True


@dataclass
class CustomTerminalFailingTask(Task):
    """A task that fails with a custom terminal error."""
    error_message: str = "Custom terminal failure"

    async def run(self):
        raise CustomTerminalError(self.error_message)


@dataclass
class RecoverableTask(Task):
    """A task that tracks recover() calls."""
    recover_calls: List[str]  # Shared list to track calls
    should_recover: bool = True

    async def run(self):
        raise ValueError("Recoverable failure")

    async def recover(self) -> bool:
        self.recover_calls.append("recover_called")
        return self.should_recover


@dataclass
class TerminalRecoverableTask(Task):
    """A terminal-failing task that tracks if recover() is (incorrectly) called."""
    recover_calls: List[str]  # Shared list to track calls

    async def run(self):
        raise ContextLimitExceededError("Terminal failure")

    async def recover(self) -> bool:
        self.recover_calls.append("recover_called_on_terminal")
        return True  # Would retry if called, but should never be called


# =============================================================================
# Tests: Basic Terminal Error Detection
# =============================================================================

def test_is_terminal_exception_context_limit():
    """ContextLimitExceededError should be detected as terminal."""
    e = ContextLimitExceededError("test")
    assert _is_terminal_exception(e) is True


def test_is_terminal_exception_custom_terminal():
    """Exceptions with terminal=True attribute should be detected as terminal."""
    e = CustomTerminalError("test")
    assert _is_terminal_exception(e) is True


def test_is_terminal_exception_normal():
    """Normal exceptions should not be detected as terminal."""
    e = ValueError("test")
    assert _is_terminal_exception(e) is False


def test_result_is_terminal_property():
    """Result.is_terminal should correctly identify terminal results."""
    # Success result
    r1 = Result(value="ok")
    assert r1.is_terminal is False

    # Normal error
    r2 = Result(error="normal error", terminal=False)
    assert r2.is_terminal is False

    # Terminal error
    r3 = Result(error="terminal error", terminal=True)
    assert r3.is_terminal is True

    # Error=None with terminal=True should not be terminal (no error)
    r4 = Result(value="ok", terminal=True)
    assert r4.is_terminal is False


def test_task_error_is_terminal_property():
    """TaskError.is_terminal should correctly identify terminal errors."""
    r1 = Result(error="normal")
    e1 = TaskError("normal", r1, terminal=False)
    assert e1.is_terminal is False

    r2 = Result(error="terminal", terminal=True)
    e2 = TaskError("terminal", r2, terminal=True)
    assert e2.is_terminal is True


# =============================================================================
# Tests: Terminal Error Propagation Through wrapped()
# =============================================================================

def test_wrapped_returns_result_for_normal_error(executor):
    """wrapped() should return Result for normal (non-terminal) errors."""
    @dataclass
    class CheckNormalError(Task):
        def run(self):
            result = yield wrapped(NormalFailingTask())
            # Should reach here - wrapped() returns Result
            assert isinstance(result, Result)
            assert result.is_success is False
            assert result.is_terminal is False
            assert "Normal failure" in result.error
            return "checked"

    async def run():
        value = await executor.run(CheckNormalError())
        assert value == "checked"

    asyncio.run(run())


def test_wrapped_raises_for_terminal_error(executor):
    """wrapped() should RAISE TaskError for terminal errors, not return Result."""
    @dataclass
    class CheckTerminalError(Task):
        def run(self):
            try:
                result = yield wrapped(TerminalFailingTask())
                # Should NOT reach here - terminal error raises
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                assert "Context limit exceeded" in str(e)
                return "caught_terminal"

    async def run():
        value = await executor.run(CheckTerminalError())
        assert value == "caught_terminal"

    asyncio.run(run())


def test_wrapped_raises_for_custom_terminal_error(executor):
    """wrapped() should RAISE TaskError for custom terminal errors."""
    @dataclass
    class CheckCustomTerminal(Task):
        def run(self):
            try:
                yield wrapped(CustomTerminalFailingTask())
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                assert "Custom terminal failure" in str(e)
                return "caught_custom_terminal"

    async def run():
        value = await executor.run(CheckCustomTerminal())
        assert value == "caught_custom_terminal"

    asyncio.run(run())


def test_terminal_error_propagates_through_multiple_layers(executor):
    """Terminal errors should propagate through multiple levels of nesting."""
    @dataclass
    class InnerTask(Task):
        def run(self):
            # This wrapped() call should raise, not return Result
            yield wrapped(TerminalFailingTask())
            return "inner_done"

    @dataclass
    class MiddleTask(Task):
        def run(self):
            # Terminal error from InnerTask propagates here
            yield wrapped(InnerTask())
            return "middle_done"

    @dataclass
    class OuterTask(Task):
        def run(self):
            try:
                yield wrapped(MiddleTask())
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                return "caught_at_outer"

    async def run():
        value = await executor.run(OuterTask())
        assert value == "caught_at_outer"

    asyncio.run(run())


# =============================================================================
# Tests: Terminal Errors Skip recover()
# =============================================================================

def test_normal_error_calls_recover(executor, store):
    """Normal errors should call recover() when task is re-executed."""
    recover_calls = []
    task = RecoverableTask(recover_calls=recover_calls, should_recover=True)

    async def run():
        # First run - task fails
        result = await executor.run(wrapped(task))
        assert result.is_success is False
        assert len(recover_calls) == 0  # recover() not called on first run

        # Second run - should call recover() before retrying
        result2 = await executor.run(wrapped(task))
        # Since recover() returns True and task still fails, we get failure
        assert result2.is_success is False
        assert len(recover_calls) == 1  # recover() WAS called

    asyncio.run(run())


def test_terminal_error_skips_recover(executor, store):
    """Terminal errors should NOT call recover() - return cached failure immediately."""
    recover_calls = []
    task = TerminalRecoverableTask(recover_calls=recover_calls)

    async def run():
        # First run - task fails with terminal error
        try:
            await executor.run(wrapped(task))
        except TaskError as e:
            assert e.is_terminal is True

        # Second run - should NOT call recover(), return cached terminal failure
        try:
            await executor.run(wrapped(task))
        except TaskError as e:
            assert e.is_terminal is True

        # recover() should NEVER have been called
        assert len(recover_calls) == 0

    asyncio.run(run())


def test_cached_terminal_result_returned_immediately(executor, store):
    """Re-executing a terminal-failed task should return cached result without running."""
    run_count = [0]

    @dataclass
    class CountingTerminalTask(Task):
        async def run(self):
            run_count[0] += 1
            raise ContextLimitExceededError("Terminal!")

    task = CountingTerminalTask()

    async def run():
        # First run
        try:
            await executor.run(wrapped(task))
        except TaskError:
            pass
        assert run_count[0] == 1

        # Second run - should use cache, not run again
        try:
            await executor.run(wrapped(task))
        except TaskError:
            pass
        assert run_count[0] == 1  # Still 1, didn't run again

    asyncio.run(run())


# =============================================================================
# Tests: Result.terminal Flag Preserved in Cache
# =============================================================================

def test_terminal_flag_preserved_in_cache(executor, store):
    """The terminal flag should be preserved when Result is cached and retrieved."""
    task = TerminalFailingTask()

    async def run():
        # Run task - fails with terminal error
        try:
            await executor.run(wrapped(task))
        except TaskError:
            pass

        # Check the cached result directly
        key = task.get_cache_key()
        row = store.get(key)
        assert row is not None
        assert row['status'] == 'FAILED'

        cached_result = pickle.loads(row['result_blob'])
        assert cached_result.is_terminal is True
        assert cached_result.error is not None

    asyncio.run(run())


def test_non_terminal_flag_preserved_in_cache(executor, store):
    """Non-terminal errors should have terminal=False in cache."""
    task = NormalFailingTask()

    async def run():
        # Run task - fails with normal error
        result = await executor.run(wrapped(task))
        assert result.is_success is False

        # Check the cached result directly
        key = task.get_cache_key()
        row = store.get(key)
        cached_result = pickle.loads(row['result_blob'])
        assert cached_result.is_terminal is False

    asyncio.run(run())


# =============================================================================
# Tests: Integration with Real Workflow Patterns
# =============================================================================

@dataclass
class SolveResult:
    success: bool
    error: str = ""


@dataclass
class RobustSolver(Task):
    """Simulates the pattern used in CheckSolutionPassesTraining."""
    max_attempts: int = 3

    def run(self):
        try:
            return (yield from self._solve_loop())
        except TaskError as e:
            if e.is_terminal:
                return SolveResult(success=False, error=f"Terminal: {e}")
            raise

    def _solve_loop(self):
        for attempt in range(self.max_attempts):
            # Simulate work that might hit context limit
            result = yield wrapped(SuccessTask(value=f"attempt_{attempt}"))
            if result.is_success:
                return SolveResult(success=True)
        return SolveResult(success=False, error="Max attempts")


def test_robust_solver_success_path(executor):
    """RobustSolver should succeed when subtasks succeed."""
    async def run():
        value = await executor.run(RobustSolver())
        assert value.success is True

    asyncio.run(run())


@dataclass
class RobustSolverWithTerminalFailure(Task):
    """RobustSolver that encounters terminal error."""
    def run(self):
        try:
            return (yield from self._solve_loop())
        except TaskError as e:
            if e.is_terminal:
                return SolveResult(success=False, error=f"Terminal: {e}")
            raise

    def _solve_loop(self):
        # First task succeeds
        yield wrapped(SuccessTask())
        # Second task has terminal failure
        yield wrapped(TerminalFailingTask())
        # Should never reach here
        return SolveResult(success=True)


def test_robust_solver_handles_terminal_error(executor):
    """RobustSolver should catch terminal error and return graceful failure."""
    async def run():
        value = await executor.run(RobustSolverWithTerminalFailure())
        assert value.success is False
        assert "Terminal" in value.error
        assert "Context limit exceeded" in value.error

    asyncio.run(run())


# =============================================================================
# Tests: Edge Cases
# =============================================================================

def test_terminal_error_in_list_execution(executor):
    """Terminal errors should propagate even when tasks are run in parallel list."""
    @dataclass
    class ParallelTerminalTest(Task):
        def run(self):
            try:
                # Run multiple tasks in parallel, one is terminal
                results = yield [
                    wrapped(SuccessTask()),
                    wrapped(TerminalFailingTask()),
                    wrapped(SuccessTask()),
                ]
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                return "caught_terminal"

    async def run():
        value = await executor.run(ParallelTerminalTest())
        assert value == "caught_terminal"

    asyncio.run(run())


def test_mixed_errors_terminal_takes_precedence(executor):
    """When multiple errors occur, terminal status should be detected."""
    @dataclass
    class MixedErrorTest(Task):
        def run(self):
            # Normal error first
            r1 = yield wrapped(NormalFailingTask())
            assert r1.is_success is False
            assert r1.is_terminal is False

            # Terminal error second - should raise
            try:
                yield wrapped(TerminalFailingTask())
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                return "terminal_caught"

    async def run():
        value = await executor.run(MixedErrorTest())
        assert value == "terminal_caught"

    asyncio.run(run())


def test_uncached_task_terminal_error(executor):
    """Terminal errors should work correctly with uncached tasks."""
    @dataclass
    class UncachedTerminal(Task):
        cacheable = False

        async def run(self):
            raise ContextLimitExceededError("Uncached terminal")

    @dataclass
    class WrapperTask(Task):
        def run(self):
            try:
                yield wrapped(UncachedTerminal())
                return "should_not_reach"
            except TaskError as e:
                assert e.is_terminal is True
                return "caught"

    async def run():
        value = await executor.run(WrapperTask())
        assert value == "caught"

    asyncio.run(run())


# =============================================================================
# Tests: ContextLimitExceededError Specific
# =============================================================================

def test_context_limit_error_attributes():
    """ContextLimitExceededError should have correct terminal attributes."""
    # Class attribute
    assert ContextLimitExceededError.terminal is True

    # Instance attribute
    e = ContextLimitExceededError("test message")
    assert e.terminal is True
    assert str(e) == "test message"


def test_context_limit_error_detected_by_helper():
    """_is_terminal_exception should detect ContextLimitExceededError."""
    e = ContextLimitExceededError("test")
    assert _is_terminal_exception(e) is True

    # Subclass should also be detected
    class SubclassedContextLimit(ContextLimitExceededError):
        pass

    e2 = SubclassedContextLimit("subclass test")
    assert _is_terminal_exception(e2) is True


# =============================================================================
# Tests: Non-wrapped tasks with terminal errors
# =============================================================================

def test_non_wrapped_terminal_error_preserves_flag(executor):
    """Non-wrapped tasks should preserve terminal flag in TaskError.

    This is critical for top-level error handling patterns where the caller
    needs to distinguish terminal errors even for non-wrapped subtasks.
    """
    @dataclass
    class OuterTaskWithNonWrappedSubtask(Task):
        def run(self):
            try:
                # Non-wrapped task - terminal error should still be detectable
                yield TerminalFailingTask()
                return "should_not_reach"
            except TaskError as e:
                # CRITICAL: is_terminal must be True even for non-wrapped tasks
                assert e.is_terminal is True, "Terminal flag not preserved for non-wrapped task!"
                return "caught_terminal"

    async def run():
        value = await executor.run(OuterTaskWithNonWrappedSubtask())
        assert value == "caught_terminal"

    asyncio.run(run())


def test_non_wrapped_normal_error_has_terminal_false(executor):
    """Non-wrapped tasks with normal errors should have terminal=False."""
    @dataclass
    class OuterTaskWithNormalError(Task):
        def run(self):
            try:
                yield NormalFailingTask()
                return "should_not_reach"
            except TaskError as e:
                # Normal errors should NOT be marked terminal
                assert e.is_terminal is False
                return "caught_normal"

    async def run():
        value = await executor.run(OuterTaskWithNormalError())
        assert value == "caught_normal"

    asyncio.run(run())


def test_batch_continues_after_terminal_error_in_non_wrapped(executor):
    """Batch execution should continue when one task catches a terminal error."""
    results_collected = []

    @dataclass
    class TaskThatCatchesTerminal(Task):
        task_id: str

        def run(self):
            try:
                # Non-wrapped subtask that fails terminally
                yield TerminalFailingTask()
                return f"success_{self.task_id}"
            except TaskError as e:
                if e.is_terminal:
                    return f"terminal_caught_{self.task_id}"
                raise

    @dataclass
    class SuccessfulTask(Task):
        task_id: str

        async def run(self):
            return f"success_{self.task_id}"

    @dataclass
    class BatchRunner(Task):
        def run(self):
            # Run tasks that include one with terminal error
            results = yield [
                TaskThatCatchesTerminal(task_id="A"),
                SuccessfulTask(task_id="B"),
                TaskThatCatchesTerminal(task_id="C"),
            ]
            return results

    async def run():
        results = await executor.run(BatchRunner())
        assert len(results) == 3
        assert results[0] == "terminal_caught_A"
        assert results[1] == "success_B"
        assert results[2] == "terminal_caught_C"

    asyncio.run(run())
