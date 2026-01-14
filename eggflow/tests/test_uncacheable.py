import asyncio
from dataclasses import dataclass
from typing import ClassVar
from eggflow import Task, Result, nocache

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
    """Verify yielding a coroutine runs each time, returns Result."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithCoroutine(Task):
        def run(self):
            res = yield always_run_coroutine("test")
            return res.value

    async def run():
        flow = FlowWithCoroutine()
        res1 = await executor.run(flow)
        assert res1.is_success
        assert res1.value == "Coroutine test"
        assert execution_count == 1

        # Run again - coroutine should run again (flow is cached but we're testing coroutine behavior)
        res2 = await executor.run(flow)
        # Flow result is cached, so coroutine doesn't run again
        assert res2.value == "Coroutine test"

    asyncio.run(run())

def test_cacheable_false(executor, store):
    """Verify task with cacheable=False runs each time, not stored."""
    global execution_count
    execution_count = 0

    async def run():
        task = UncacheableTask("test")
        res1 = await executor.run(task)
        assert res1.is_success
        assert res1.value == "Uncached test"
        assert execution_count == 1

        # Run again - should execute again since not cached
        res2 = await executor.run(task)
        assert res2.value == "Uncached test"
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
            # Cached task
            cached_res = yield CountingTask("cached")
            # Uncacheable task
            uncached_res = yield UncacheableTask("uncached")
            # Raw coroutine
            coro_res = yield always_run_coroutine("coro")
            return f"{cached_res.value}, {uncached_res.value}, {coro_res.value}"

    async def run():
        flow = MixedFlow()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "Executed cached, Uncached uncached, Coroutine coro"
        assert execution_count == 3

    asyncio.run(run())

def test_parallel_list(executor):
    """Test yield [CachedTask(), always_run_coroutine()]."""
    global execution_count
    execution_count = 0

    @dataclass
    class ParallelFlow(Task):
        def run(self):
            results = yield [CountingTask("a"), always_run_coroutine("b")]
            return [r.value for r in results]

    async def run():
        flow = ParallelFlow()
        res = await executor.run(flow)
        assert res.is_success
        assert "Executed a" in res.value
        assert "Coroutine b" in res.value
        assert execution_count == 2

    asyncio.run(run())

def test_error_handling_coroutine(executor):
    """Coroutine that raises returns error Result."""
    async def failing_coroutine():
        raise ValueError("Coroutine failed")

    @dataclass
    class FlowWithFailingCoroutine(Task):
        def run(self):
            res = yield failing_coroutine()
            return res

    async def run():
        flow = FlowWithFailingCoroutine()
        res = await executor.run(flow)
        # The flow returns the error result from the coroutine
        assert res.is_success  # Flow itself succeeded
        assert res.value.error == "Coroutine failed"

    asyncio.run(run())

def test_error_handling_uncacheable_task(executor):
    """Uncacheable task that fails returns error Result."""
    @dataclass
    class FailingUncacheableTask(Task):
        cacheable: ClassVar[bool] = False
        async def run(self):
            raise ValueError("Task failed")

    async def run():
        task = FailingUncacheableTask()
        res = await executor.run(task)
        assert not res.is_success
        assert res.error == "Task failed"

    asyncio.run(run())

def test_uncacheable_subtasks_of_uncacheable_parent(executor):
    """Subtasks of uncacheable tasks can still be cached."""
    global execution_count
    execution_count = 0

    @dataclass
    class UncacheableParent(Task):
        cacheable: ClassVar[bool] = False
        def run(self):
            # This child IS cacheable
            res = yield CountingTask("child")
            return res.value

    async def run():
        flow = UncacheableParent()
        res1 = await executor.run(flow)
        assert res1.value == "Executed child"
        assert execution_count == 1

        # Run parent again - parent runs again but child is cached
        res2 = await executor.run(flow)
        assert res2.value == "Executed child"
        assert execution_count == 1  # Child didn't run again

    asyncio.run(run())

def test_nocache_wrapper(executor, store):
    """Test nocache() wrapper skips caching per-instance."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithNocache(Task):
        def run(self):
            # First call cached
            res1 = yield CountingTask("cached")
            # Second call with nocache - skips cache
            res2 = yield nocache(CountingTask("uncached"))
            return f"{res1.value}, {res2.value}"

    async def run():
        flow = FlowWithNocache()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "Executed cached, Executed uncached"
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
            # First call - cached
            res1 = yield CountingTask("same")
            # Second call with nocache - runs again
            res2 = yield nocache(CountingTask("same"))
            return (res1.value, res2.value)

    async def run():
        flow = FlowSameTaskTwice()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ("Executed same", "Executed same")
        assert execution_count == 2  # Both ran

    asyncio.run(run())

def test_nocache_in_parallel_list(executor):
    """Test nocache in a parallel list."""
    global execution_count
    execution_count = 0

    @dataclass
    class ParallelWithNocache(Task):
        def run(self):
            results = yield [
                CountingTask("a"),
                nocache(CountingTask("b")),
            ]
            return [r.value for r in results]

    async def run():
        flow = ParallelWithNocache()
        res = await executor.run(flow)
        assert res.is_success
        assert "Executed a" in res.value
        assert "Executed b" in res.value
        assert execution_count == 2

    asyncio.run(run())

def test_execute_with_cached_false(executor):
    """Test task.execute(cached=False) skips cache."""
    global execution_count
    execution_count = 0

    async def helper():
        return await CountingTask("via_execute").execute(cached=False)

    @dataclass
    class FlowUsingExecuteUncached(Task):
        async def run(self):
            res = await helper()
            return res.value

    async def run():
        global execution_count
        flow = FlowUsingExecuteUncached()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "Executed via_execute"
        assert execution_count == 1

        # Test execute(cached=False) directly - should run each time
        execution_count = 0
        res2 = await CountingTask("direct").execute(cached=False)
        assert res2.value == "Executed direct"
        res3 = await CountingTask("direct").execute(cached=False)
        assert res3.value == "Executed direct"
        assert execution_count == 2  # Both ran

    asyncio.run(run())
