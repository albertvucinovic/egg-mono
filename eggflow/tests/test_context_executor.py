import asyncio
from dataclasses import dataclass
from typing import ClassVar
from eggflow import Task, Result

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
    result = await CountingTask("from_helper").execute()
    return result.value

async def deeply_nested():
    """Deeply nested function that uses execute()."""
    result = await CountingTask("deep").execute()
    return result.value

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
        res1 = await executor.run(flow)
        assert res1.is_success
        assert res1.value == "Executed from_helper"
        assert execution_count == 1

        # Run flow again - the inner CountingTask should be cached
        res2 = await executor.run(flow)
        # Flow itself is cached, so it doesn't run again at all
        assert res2.value == "Executed from_helper"

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
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "Executed deep"
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
        res1 = await task.execute()
        assert res1.is_success
        assert res1.value == "Executed no_context"
        assert execution_count == 1

        # Run again - no caching without executor
        res2 = await task.execute()
        assert res2.value == "Executed no_context"
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
            res = yield InnerTask()
            return res.value

    async def run():
        flow = OuterFlow()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "Executed from_helper"
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
            res = await UncacheableCountingTask("via_execute").execute()
            return res.value

    async def run():
        flow = FlowWithUncacheableExecute()
        res1 = await executor.run(flow)
        assert res1.is_success
        assert res1.value == "Uncached via_execute"
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
        results = await asyncio.gather(
            CountingTask("a").execute(),
            CountingTask("b").execute(),
            CountingTask("c").execute()
        )
        return [r.value for r in results]

    @dataclass
    class ParallelFlow(Task):
        async def run(self):
            return await parallel_helper()

    async def run():
        flow = ParallelFlow()
        res = await executor.run(flow)
        assert res.is_success
        assert "Executed a" in res.value
        assert "Executed b" in res.value
        assert "Executed c" in res.value
        assert execution_count == 3

        # All three tasks should be cached
        for name in ["a", "b", "c"]:
            row = executor.store.get(CountingTask(name).get_cache_key())
            assert row is not None

    asyncio.run(run())
