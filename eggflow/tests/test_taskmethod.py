import asyncio
from dataclasses import dataclass
from eggflow import Task, Result, as_task, TaskError, wrapped

# Counter to track executions
execution_count = 0

# --- as_task tests ---

class ExternalService:
    """Simulates an external class we can't modify."""

    def __init__(self, model: str):
        self.model = model
        self.call_count = 0

    async def generate(self, prompt: str):
        global execution_count
        execution_count += 1
        self.call_count += 1
        return f"[{self.model}] {prompt}"

    def compute(self, x: int):
        global execution_count
        execution_count += 1
        return x * 2

def test_as_task_basic(executor):
    """as_task wraps an existing method as a Task."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    async def run():
        value = await executor.run(as_task(service.generate, "hello"))
        assert value == "[gpt-4] hello"
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_caching(executor):
    """as_task caches based on args."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    async def run():
        value1 = await executor.run(as_task(service.generate, "hello"))
        assert value1 == "[gpt-4] hello"
        assert execution_count == 1

        # Same args - cache hit
        value2 = await executor.run(as_task(service.generate, "hello"))
        assert value2 == "[gpt-4] hello"
        assert execution_count == 1

        # Different args - cache miss
        value3 = await executor.run(as_task(service.generate, "world"))
        assert value3 == "[gpt-4] world"
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_with_cache_key(executor):
    """as_task with cache_key includes instance state in key."""
    global execution_count
    execution_count = 0

    service_gpt = ExternalService("gpt-4")
    service_claude = ExternalService("claude")

    async def run():
        # Call with gpt-4 - use explicit cache_key
        value1 = await executor.run(as_task(service_gpt.generate, "hello", cache_key=(service_gpt.model, "hello")))
        assert value1 == "[gpt-4] hello"
        assert execution_count == 1

        # Same prompt, different model - cache miss due to different cache_key
        value2 = await executor.run(as_task(service_claude.generate, "hello", cache_key=(service_claude.model, "hello")))
        assert value2 == "[claude] hello"
        assert execution_count == 2

        # Same model and prompt - cache hit
        value3 = await executor.run(as_task(service_gpt.generate, "hello", cache_key=(service_gpt.model, "hello")))
        assert value3 == "[gpt-4] hello"
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_in_flow(executor):
    """as_task works inside a flow with yields."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    @dataclass
    class FlowWithAsTask(Task):
        def run(self):
            val1 = yield as_task(service.generate, "step 1", cache_key=(service.model, "step 1"))
            val2 = yield as_task(service.generate, "step 2", cache_key=(service.model, "step 2"))
            val3 = yield as_task(service.generate, "step 1", cache_key=(service.model, "step 1"))  # cached
            return [val1, val2, val3]

    async def run():
        flow = FlowWithAsTask()
        value = await executor.run(flow)
        assert value == ["[gpt-4] step 1", "[gpt-4] step 2", "[gpt-4] step 1"]
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_with_execute(executor):
    """as_task works with .execute()."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    @dataclass
    class FlowWithAsTaskExecute(Task):
        async def run(self):
            val1 = await as_task(service.generate, "hello", cache_key=(service.model, "hello")).execute()
            val2 = await as_task(service.generate, "hello", cache_key=(service.model, "hello")).execute()
            return [val1, val2]

    async def run():
        flow = FlowWithAsTaskExecute()
        value = await executor.run(flow)
        assert value == ["[gpt-4] hello", "[gpt-4] hello"]
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_sync_method(executor):
    """as_task works with sync methods."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    async def run():
        value = await executor.run(as_task(service.compute, 5))
        assert value == 10
        assert execution_count == 1

        # Cache hit
        value2 = await executor.run(as_task(service.compute, 5))
        assert value2 == 10
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_with_kwargs(executor):
    """as_task handles keyword arguments."""
    global execution_count
    execution_count = 0

    class KwargsExternalService:
        async def process(self, data: str, prefix: str = ""):
            global execution_count
            execution_count += 1
            return f"{prefix}{data}"

    service = KwargsExternalService()

    async def run():
        value1 = await executor.run(as_task(service.process, "hello"))
        assert value1 == "hello"
        assert execution_count == 1

        # Different kwarg - cache miss
        value2 = await executor.run(as_task(service.process, "hello", prefix=">>> "))
        assert value2 == ">>> hello"
        assert execution_count == 2

        # Same - cache hit
        value3 = await executor.run(as_task(service.process, "hello", prefix=">>> "))
        assert value3 == ">>> hello"
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_with_nocache(executor):
    """as_task works with nocache wrapper."""
    global execution_count
    execution_count = 0

    from eggflow import nocache

    service = ExternalService("gpt-4")

    async def run():
        value1 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 1

        # With nocache - runs again
        value2 = await executor.run(nocache(as_task(service.generate, "hello")))
        assert execution_count == 2

        # Without nocache - cached
        value3 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_plain_function(executor):
    """as_task works with plain functions."""
    global execution_count
    execution_count = 0

    def standalone_func(x):
        global execution_count
        execution_count += 1
        return x * 2

    async def run():
        value = await executor.run(as_task(standalone_func, 5))
        assert value == 10
        assert execution_count == 1

    asyncio.run(run())

# --- as_task with functions tests ---

async def async_compute(x: int, y: int):
    global execution_count
    execution_count += 1
    return x + y

def sync_compute(x: int, y: int):
    global execution_count
    execution_count += 1
    return x * y

def test_as_task_function_basic(executor):
    """as_task works with plain functions."""
    global execution_count
    execution_count = 0

    async def run():
        value = await executor.run(as_task(async_compute, 3, 4))
        assert value == 7
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_function_caching(executor):
    """as_task caches function calls by args."""
    global execution_count
    execution_count = 0

    async def run():
        value1 = await executor.run(as_task(async_compute, 3, 4))
        assert value1 == 7
        assert execution_count == 1

        # Same args - cache hit
        value2 = await executor.run(as_task(async_compute, 3, 4))
        assert value2 == 7
        assert execution_count == 1

        # Different args - cache miss
        value3 = await executor.run(as_task(async_compute, 5, 6))
        assert value3 == 11
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_function_cache_key(executor):
    """as_task with cache_key uses only specified values."""
    global execution_count
    execution_count = 0

    async def run():
        # Cache by first arg only
        value1 = await executor.run(as_task(async_compute, 3, 4, cache_key=(3,)))
        assert value1 == 7
        assert execution_count == 1

        # Different second arg, same cache_key - cache hit!
        value2 = await executor.run(as_task(async_compute, 3, 100, cache_key=(3,)))
        assert value2 == 7  # Cached result from first call
        assert execution_count == 1

        # Different first arg - cache miss
        value3 = await executor.run(as_task(async_compute, 5, 4, cache_key=(5,)))
        assert value3 == 9
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_function_sync(executor):
    """as_task works with sync functions."""
    global execution_count
    execution_count = 0

    async def run():
        value = await executor.run(as_task(sync_compute, 3, 4))
        assert value == 12
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_function_in_flow(executor):
    """as_task with functions works inside flows."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowWithFuncTask(Task):
        def run(self):
            val1 = yield as_task(async_compute, 1, 2)
            val2 = yield as_task(async_compute, 3, 4)
            val3 = yield as_task(async_compute, 1, 2)  # cached
            return [val1, val2, val3]

    async def run():
        flow = FlowWithFuncTask()
        value = await executor.run(flow)
        assert value == [3, 7, 3]
        assert execution_count == 2

    asyncio.run(run())

# --- Error handling tests ---

def test_task_error_raised(executor):
    """TaskError is raised on failure by default."""
    @dataclass
    class FailingTask(Task):
        async def run(self):
            raise ValueError("task failed")

    @dataclass
    class FlowCatchingError(Task):
        def run(self):
            try:
                value = yield FailingTask()
                return f"got: {value}"
            except TaskError as e:
                return f"caught error: {e.result.error}"

    async def run():
        flow = FlowCatchingError()
        value = await executor.run(flow)
        assert value == "caught error: task failed"

    asyncio.run(run())

def test_execute_raises_on_error(executor):
    """execute() raises TaskError on failure by default."""
    @dataclass
    class FailTask(Task):
        async def run(self):
            raise ValueError("execute failed")

    @dataclass
    class FlowWithExecuteFail(Task):
        async def run(self):
            try:
                await FailTask().execute()
                return "should not reach"
            except TaskError as e:
                return f"caught: {e.result.error}"

    async def run():
        flow = FlowWithExecuteFail()
        value = await executor.run(flow)
        assert value == "caught: execute failed"

    asyncio.run(run())

# --- wrapped() tests ---

def test_wrapped_returns_result(executor):
    """wrapped() returns Result object instead of value."""
    global execution_count
    execution_count = 0

    @dataclass
    class SimpleTask(Task):
        async def run(self):
            global execution_count
            execution_count += 1
            return "result_value"

    @dataclass
    class FlowWithWrapped(Task):
        def run(self):
            result = yield wrapped(SimpleTask())
            assert isinstance(result, Result)
            assert result.is_success
            return result.value

    async def run():
        flow = FlowWithWrapped()
        value = await executor.run(flow)
        assert value == "result_value"

    asyncio.run(run())

def test_wrapped_error_returns_result(executor):
    """wrapped() returns Result with error instead of raising."""
    @dataclass
    class FailingTask(Task):
        async def run(self):
            raise ValueError("task failed")

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
        assert value == "task failed"

    asyncio.run(run())

def test_wrapped_in_parallel_list(executor):
    """wrapped() works in parallel lists."""
    global execution_count
    execution_count = 0

    @dataclass
    class ValueTask(Task):
        val: int
        async def run(self):
            global execution_count
            execution_count += 1
            return self.val * 2

    @dataclass
    class FlowParallel(Task):
        def run(self):
            # Mix values and wrapped results
            results = yield [ValueTask(1), ValueTask(2), wrapped(ValueTask(3))]
            return results

    async def run():
        flow = FlowParallel()
        value = await executor.run(flow)
        assert value[0] == 2  # value
        assert value[1] == 4  # value
        assert isinstance(value[2], Result)  # Result object
        assert value[2].value == 6

    asyncio.run(run())
