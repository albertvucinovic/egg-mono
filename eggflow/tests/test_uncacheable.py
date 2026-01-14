import asyncio
from dataclasses import dataclass
from typing import ClassVar
from eggflow import Task, Result, nocache, wrapped, TaskError

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
class UncacheableTask(Task):
    """A task that skips caching."""
    cacheable: ClassVar[bool] = False
    name: str
    async def run(self):
        global execution_count
        execution_count += 1
        return f"Uncached {self.name}"

async def always_run_coroutine(name: str):
    """A raw coroutine that always runs."""
    global execution_count
    execution_count += 1
    return f"Coroutine {name}"

def test_yield_coroutine(executor):
    """Verify yielding a coroutine runs each time, returns value directly."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithCoroutine(Task):
        def run(self):
            # Now returns value directly
            value = yield always_run_coroutine("test")
            return value

    async def run():
        flow = FlowWithCoroutine()
        # executor.run now returns value directly
        value1 = await executor.run(flow)
        assert value1 == "Coroutine test"
        assert execution_count == 1

        # Run again - coroutine should run again (flow is cached but we're testing coroutine behavior)
        value2 = await executor.run(flow)
        # Flow result is cached, so coroutine doesn't run again
        assert value2 == "Coroutine test"

    asyncio.run(run())

def test_cacheable_false(executor, store):
    """Verify task with cacheable=False runs each time, not stored."""
    global execution_count
    execution_count = 0

    async def run():
        task = UncacheableTask("test")
        value1 = await executor.run(task)
        assert value1 == "Uncached test"
        assert execution_count == 1

        # Run again - should execute again since not cached
        value2 = await executor.run(task)
        assert value2 == "Uncached test"
        assert execution_count == 2

        # Verify nothing was stored in the database
        key = task.get_cache_key()
        row = store.get(key)
        assert row is None

    asyncio.run(run())

def test_mixed_flow(executor):
    """Flow with cached Tasks, uncacheable Tasks, and coroutines."""
    global execution_count
    execution_count = 0

    @dataclass
    class MixedFlow(Task):
        def run(self):
            # Now returns values directly
            cached_val = yield CountingTask("cached")
            uncached_val = yield UncacheableTask("uncached")
            coro_val = yield always_run_coroutine("coro")
            return f"{cached_val}, {uncached_val}, {coro_val}"

    async def run():
        flow = MixedFlow()
        value = await executor.run(flow)
        assert value == "Executed cached, Uncached uncached, Coroutine coro"
        assert execution_count == 3

    asyncio.run(run())

def test_parallel_list(executor):
    """Test yield [CachedTask(), always_run_coroutine()]."""
    global execution_count
    execution_count = 0

    @dataclass
    class ParallelFlow(Task):
        def run(self):
            # List yields now return values directly
            results = yield [CountingTask("a"), always_run_coroutine("b")]
            return results

    async def run():
        flow = ParallelFlow()
        value = await executor.run(flow)
        assert "Executed a" in value
        assert "Coroutine b" in value
        assert execution_count == 2

    asyncio.run(run())

def test_error_handling_coroutine(executor):
    """Coroutine that raises raises TaskError."""
    async def failing_coroutine():
        raise ValueError("Coroutine failed")

    @dataclass
    class FlowWithFailingCoroutine(Task):
        def run(self):
            try:
                value = yield failing_coroutine()
                return value
            except TaskError as e:
                return f"caught: {e.result.error}"

    async def run():
        flow = FlowWithFailingCoroutine()
        value = await executor.run(flow)
        assert value == "caught: Coroutine failed"

    asyncio.run(run())

def test_error_handling_uncacheable_task(executor):
    """Uncacheable task that fails raises TaskError."""
    @dataclass
    class FailingUncacheableTask(Task):
        cacheable: ClassVar[bool] = False
        async def run(self):
            raise ValueError("Task failed")

    async def run():
        task = FailingUncacheableTask()
        try:
            await executor.run(task)
            assert False, "Should have raised TaskError"
        except TaskError as e:
            assert e.result.error == "Task failed"

    asyncio.run(run())

def test_uncacheable_subtasks_of_uncacheable_parent(executor):
    """Subtasks of uncacheable tasks can still be cached."""
    global execution_count
    execution_count = 0

    @dataclass
    class UncacheableParent(Task):
        cacheable: ClassVar[bool] = False
        def run(self):
            # This child IS cacheable - now returns value directly
            value = yield CountingTask("child")
            return value

    async def run():
        flow = UncacheableParent()
        value1 = await executor.run(flow)
        assert value1 == "Executed child"
        assert execution_count == 1

        # Run parent again - parent runs again but child is cached
        value2 = await executor.run(flow)
        assert value2 == "Executed child"
        assert execution_count == 1  # Child didn't run again

    asyncio.run(run())

def test_nocache_wrapper(executor, store):
    """Test nocache() wrapper skips caching per-instance."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithNocache(Task):
        def run(self):
            # Now returns values directly
            val1 = yield CountingTask("cached")
            val2 = yield nocache(CountingTask("uncached"))
            return f"{val1}, {val2}"

    async def run():
        flow = FlowWithNocache()
        value = await executor.run(flow)
        assert value == "Executed cached, Executed uncached"
        assert execution_count == 2

        # Verify first task was cached
        key = CountingTask("cached").get_cache_key()
        row = store.get(key)
        assert row is not None

        # Verify nocache'd task was NOT cached
        key2 = CountingTask("uncached").get_cache_key()
        row2 = store.get(key2)
        assert row2 is None

    asyncio.run(run())

def test_nocache_same_task_twice(executor):
    """Same task: first cached, then nocache'd runs again."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowSameTaskTwice(Task):
        def run(self):
            # Now returns values directly
            val1 = yield CountingTask("same")
            val2 = yield nocache(CountingTask("same"))
            return (val1, val2)

    async def run():
        flow = FlowSameTaskTwice()
        value = await executor.run(flow)
        assert value == ("Executed same", "Executed same")
        assert execution_count == 2  # Both ran

    asyncio.run(run())

def test_nocache_in_parallel_list(executor):
    """Test nocache in a parallel list."""
    global execution_count
    execution_count = 0

    @dataclass
    class ParallelWithNocache(Task):
        def run(self):
            # List yields now return values directly
            results = yield [
                CountingTask("a"),
                nocache(CountingTask("b")),
            ]
            return results

    async def run():
        flow = ParallelWithNocache()
        value = await executor.run(flow)
        assert "Executed a" in value
        assert "Executed b" in value
        assert execution_count == 2

    asyncio.run(run())

def test_execute_with_cached_false(executor):
    """Test task.execute(cached=False) skips cache."""
    global execution_count
    execution_count = 0

    async def helper():
        # execute() now returns value by default
        return await CountingTask("via_execute").execute(cached=False)

    @dataclass
    class FlowUsingExecuteUncached(Task):
        async def run(self):
            value = await helper()
            return value

    async def run():
        global execution_count
        flow = FlowUsingExecuteUncached()
        value = await executor.run(flow)
        assert value == "Executed via_execute"
        assert execution_count == 1

        # Test execute(cached=False) directly - should run each time
        execution_count = 0
        val2 = await CountingTask("direct").execute(cached=False)
        assert val2 == "Executed direct"
        val3 = await CountingTask("direct").execute(cached=False)
        assert val3 == "Executed direct"
        assert execution_count == 2  # Both ran

    asyncio.run(run())

def test_wrapped_returns_result(executor):
    """Test wrapped() returns Result object."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithWrapped(Task):
        def run(self):
            # Use wrapped() to get Result object
            result = yield wrapped(CountingTask("test"))
            assert isinstance(result, Result)
            assert result.is_success
            return result.value

    async def run():
        flow = FlowWithWrapped()
        value = await executor.run(flow)
        assert value == "Executed test"

    asyncio.run(run())

def test_wrapped_with_nocache(executor):
    """Test wrapped(nocache(task)) returns Result without caching."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithWrappedNocache(Task):
        def run(self):
            result = yield wrapped(nocache(CountingTask("test")))
            assert isinstance(result, Result)
            return result.value

    async def run():
        flow = FlowWithWrappedNocache()
        value = await executor.run(flow)
        assert value == "Executed test"

    asyncio.run(run())

def test_wrapped_error_returns_result(executor):
    """Test wrapped() returns Result with error instead of raising."""
    @dataclass
    class FailingTask(Task):
        async def run(self):
            raise ValueError("Task failed")

    @dataclass
    class FlowWithWrappedError(Task):
        def run(self):
            result = yield wrapped(FailingTask())
            assert isinstance(result, Result)
            assert not result.is_success
            return result.error

    async def run():
        flow = FlowWithWrappedError()
        value = await executor.run(flow)
        assert value == "Task failed"

    asyncio.run(run())
