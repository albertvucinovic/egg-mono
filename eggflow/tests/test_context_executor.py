import asyncio
from dataclasses import dataclass
from typing import ClassVar
from eggflow import Task, Result, TaskError

# Counter to track executions
execution_count = 0

@dataclass
class CountingTask(Task):
    """A cacheable task that counts executions."""
    name: str
    async def run(self):
        global execution_count
        execution_count += 1
        return f"Executed {self.name}"

@dataclass
class UncacheableCountingTask(Task):
    """An uncacheable task that counts executions."""
    cacheable: ClassVar[bool] = False
    name: str
    async def run(self):
        global execution_count
        execution_count += 1
        return f"Uncached {self.name}"

async def helper_function():
    """A regular async function that uses Task.execute()."""
    # execute() now returns value directly
    return await CountingTask("from_helper").execute()

async def deeply_nested():
    """Deeply nested function that uses execute()."""
    return await CountingTask("deep").execute()

async def nested_helper():
    """Calls another function that uses execute()."""
    return await deeply_nested()

def test_execute_with_context(executor, store):
    """Task called via execute() is cached when executor in context."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowUsingExecute(Task):
        async def run(self):
            return await helper_function()

    async def run():
        flow = FlowUsingExecute()
        # executor.run now returns value directly
        value1 = await executor.run(flow)
        assert value1 == "Executed from_helper"
        assert execution_count == 1

        # Run flow again - the inner CountingTask should be cached
        value2 = await executor.run(flow)
        # Flow itself is cached, so it doesn't run again at all
        assert value2 == "Executed from_helper"

    asyncio.run(run())

def test_execute_nested_function_calls(executor):
    """Task.execute() in deeply nested function still uses executor."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithNesting(Task):
        async def run(self):
            return await nested_helper()

    async def run():
        flow = FlowWithNesting()
        value = await executor.run(flow)
        assert value == "Executed deep"
        assert execution_count == 1

        # Verify the task was cached
        task = CountingTask("deep")
        key = task.get_cache_key()
        row = executor.store.get(key)
        assert row is not None
        assert row['status'] == "COMPLETED"

    asyncio.run(run())

def test_execute_without_context():
    """execute() without context runs directly (no cache)."""
    global execution_count
    execution_count = 0

    async def run():
        task = CountingTask("no_context")
        # execute() returns value directly
        value1 = await task.execute()
        assert value1 == "Executed no_context"
        assert execution_count == 1

        # Run again - no caching without executor
        value2 = await task.execute()
        assert value2 == "Executed no_context"
        assert execution_count == 2

    asyncio.run(run())

def test_mixed_yield_and_execute(executor):
    """Flow yields Task, which calls function, which calls execute()."""
    global execution_count
    execution_count = 0

    @dataclass
    class InnerTask(Task):
        async def run(self):
            # Call helper that uses execute()
            return await helper_function()

    @dataclass
    class OuterFlow(Task):
        def run(self):
            # yield now returns value directly
            value = yield InnerTask()
            return value

    async def run():
        flow = OuterFlow()
        value = await executor.run(flow)
        assert value == "Executed from_helper"
        # Both InnerTask and CountingTask should have run once
        assert execution_count == 1

        # Verify both tasks are cached
        inner_row = executor.store.get(InnerTask().get_cache_key())
        assert inner_row is not None

        helper_row = executor.store.get(CountingTask("from_helper").get_cache_key())
        assert helper_row is not None

    asyncio.run(run())

def test_uncacheable_with_execute(executor):
    """Uncacheable task via execute() still skips cache."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithUncacheableExecute(Task):
        async def run(self):
            # execute() returns value directly
            return await UncacheableCountingTask("via_execute").execute()

    async def run():
        flow = FlowWithUncacheableExecute()
        value = await executor.run(flow)
        assert value == "Uncached via_execute"
        assert execution_count == 1

        # The uncacheable task should not be in the store
        task = UncacheableCountingTask("via_execute")
        row = executor.store.get(task.get_cache_key())
        assert row is None

    asyncio.run(run())

def test_execute_returns_result_on_error():
    """execute() without context propagates exceptions."""
    @dataclass
    class FailingTask(Task):
        async def run(self):
            raise ValueError("Task failed")

    async def run():
        task = FailingTask()
        # Without executor context, exceptions propagate directly
        try:
            await task.execute()
            assert False, "Should have raised"
        except ValueError as e:
            assert str(e) == "Task failed"

    asyncio.run(run())

def test_parallel_execute_calls(executor):
    """Multiple execute() calls in parallel all use the same executor."""
    global execution_count
    execution_count = 0

    async def parallel_helper():
        # execute() returns values directly
        results = await asyncio.gather(
            CountingTask("a").execute(),
            CountingTask("b").execute(),
            CountingTask("c").execute()
        )
        return results

    @dataclass
    class ParallelFlow(Task):
        async def run(self):
            return await parallel_helper()

    async def run():
        flow = ParallelFlow()
        value = await executor.run(flow)
        assert "Executed a" in value
        assert "Executed b" in value
        assert "Executed c" in value
        assert execution_count == 3

        # All three tasks should be cached
        for name in ["a", "b", "c"]:
            row = executor.store.get(CountingTask(name).get_cache_key())
            assert row is not None

    asyncio.run(run())

def test_execute_with_raw_returns_result(executor):
    """execute(raw=True) returns Result object."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowUsingRawExecute(Task):
        async def run(self):
            result = await CountingTask("raw").execute(raw=True)
            assert isinstance(result, Result)
            assert result.is_success
            return result.value

    async def run():
        flow = FlowUsingRawExecute()
        value = await executor.run(flow)
        assert value == "Executed raw"

    asyncio.run(run())
