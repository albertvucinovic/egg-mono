import asyncio
import warnings
from dataclasses import dataclass
from eggflow import Task, Result, taskmethod, as_task, unwrap, TaskError, wrapped

# Counter to track executions
execution_count = 0

# Note: @taskmethod is deprecated but tests still verify backwards compatibility
class SimpleService:
    """Service with no instance state in cache key."""

    @taskmethod()
    async def process(self, data: str):
        global execution_count
        execution_count += 1
        return f"processed: {data}"

class ModelService:
    """Service with model as part of cache key."""

    def __init__(self, model: str):
        self.model = model
        self.debug = False  # Not in cache key

    @taskmethod('model')
    async def generate(self, prompt: str):
        global execution_count
        execution_count += 1
        return f"[{self.model}] {prompt}"

class MultiAttrService:
    """Service with multiple attrs in cache key."""

    def __init__(self, model: str, temperature: float):
        self.model = model
        self.temperature = temperature
        self.logger = None  # Not in cache key

    @taskmethod('model', 'temperature')
    async def complete(self, prompt: str):
        global execution_count
        execution_count += 1
        return f"[{self.model}@{self.temperature}] {prompt}"

def test_taskmethod_basic(executor):
    """Basic taskmethod with no instance attrs."""
    global execution_count
    execution_count = 0

    @dataclass
    class Flow(Task):
        def run(self):
            service = SimpleService()
            # yield now returns value directly
            value = yield service.process("hello")
            return value

    async def run():
        flow = Flow()
        # executor.run now returns value directly
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == "processed: hello"
        assert execution_count == 1

    asyncio.run(run())

def test_taskmethod_caching(executor):
    """Same args = cache hit."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = SimpleService()

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # First call
            value1 = await executor.run(service.process("hello"))
            assert value1 == "processed: hello"
            assert execution_count == 1

            # Second call - same args, should be cached
            value2 = await executor.run(service.process("hello"))
            assert value2 == "processed: hello"
            assert execution_count == 1  # No new execution

            # Different args - cache miss
            value3 = await executor.run(service.process("world"))
            assert value3 == "processed: world"
            assert execution_count == 2

    asyncio.run(run())

def test_taskmethod_instance_attr(executor):
    """Instance attr affects cache key."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service_gpt = ModelService("gpt-4")
        service_claude = ModelService("claude")

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # Call with gpt-4
            value1 = await executor.run(service_gpt.generate("hello"))
            assert value1 == "[gpt-4] hello"
            assert execution_count == 1

            # Same prompt, different model - cache miss
            value2 = await executor.run(service_claude.generate("hello"))
            assert value2 == "[claude] hello"
            assert execution_count == 2

            # Same model and prompt - cache hit
            value3 = await executor.run(service_gpt.generate("hello"))
            assert value3 == "[gpt-4] hello"
            assert execution_count == 2  # No new execution

    asyncio.run(run())

def test_taskmethod_non_cache_attr_ignored(executor):
    """Attrs not in cache_attrs don't affect cache key."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service1 = ModelService("gpt-4")
        service1.debug = True

        service2 = ModelService("gpt-4")
        service2.debug = False

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value1 = await executor.run(service1.generate("hello"))
            assert value1 == "[gpt-4] hello"
            assert execution_count == 1

            # Different debug value, but same model - cache hit
            value2 = await executor.run(service2.generate("hello"))
            assert value2 == "[gpt-4] hello"
            assert execution_count == 1  # Cached!

    asyncio.run(run())

def test_taskmethod_multiple_attrs(executor):
    """Multiple instance attrs in cache key."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service1 = MultiAttrService("gpt-4", 0.7)
        service2 = MultiAttrService("gpt-4", 0.9)
        service3 = MultiAttrService("claude", 0.7)

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value1 = await executor.run(service1.complete("hello"))
            assert value1 == "[gpt-4@0.7] hello"
            assert execution_count == 1

            # Same model, different temperature - cache miss
            value2 = await executor.run(service2.complete("hello"))
            assert value2 == "[gpt-4@0.9] hello"
            assert execution_count == 2

            # Different model, same temperature - cache miss
            value3 = await executor.run(service3.complete("hello"))
            assert value3 == "[claude@0.7] hello"
            assert execution_count == 3

            # Original params - cache hit
            value4 = await executor.run(service1.complete("hello"))
            assert value4 == "[gpt-4@0.7] hello"
            assert execution_count == 3

    asyncio.run(run())

def test_taskmethod_in_flow(executor):
    """Use taskmethod inside a flow with yields."""
    global execution_count
    execution_count = 0

    @dataclass
    class Pipeline(Task):
        def run(self):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                service = ModelService("gpt-4")
            # yield now returns value directly
            val1 = yield service.generate("step 1")
            val2 = yield service.generate("step 2")
            val3 = yield service.generate("step 1")  # cached
            return [val1, val2, val3]

    async def run():
        flow = Pipeline()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == ["[gpt-4] step 1", "[gpt-4] step 2", "[gpt-4] step 1"]
        assert execution_count == 2  # Third was cached

    asyncio.run(run())

def test_taskmethod_with_nocache(executor):
    """taskmethod works with nocache wrapper."""
    global execution_count
    execution_count = 0

    from eggflow import nocache

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = SimpleService()

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # First call - cached
            value1 = await executor.run(service.process("hello"))
            assert execution_count == 1

            # With nocache - runs again
            value2 = await executor.run(nocache(service.process("hello")))
            assert execution_count == 2

            # Without nocache - cached
            value3 = await executor.run(service.process("hello"))
            assert execution_count == 2

    asyncio.run(run())

def test_taskmethod_with_kwargs(executor):
    """taskmethod handles keyword arguments."""
    global execution_count
    execution_count = 0

    class KwargsService:
        @taskmethod()
        async def process(self, data: str, prefix: str = ""):
            global execution_count
            execution_count += 1
            return f"{prefix}{data}"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = KwargsService()

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value1 = await executor.run(service.process("hello"))
            assert value1 == "hello"
            assert execution_count == 1

            # Different kwarg - cache miss
            value2 = await executor.run(service.process("hello", prefix=">>> "))
            assert value2 == ">>> hello"
            assert execution_count == 2

            # Same args and kwargs - cache hit
            value3 = await executor.run(service.process("hello", prefix=">>> "))
            assert value3 == ">>> hello"
            assert execution_count == 2

    asyncio.run(run())

def test_taskmethod_sync_method(executor):
    """taskmethod works with sync methods too."""
    global execution_count
    execution_count = 0

    class SyncService:
        @taskmethod()
        def compute(self, x: int):
            global execution_count
            execution_count += 1
            return x * 2

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = SyncService()

    async def run():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value1 = await executor.run(service.compute(5))
            assert value1 == 10
            assert execution_count == 1

            # Cache hit
            value2 = await executor.run(service.compute(5))
            assert value2 == 10
            assert execution_count == 1

    asyncio.run(run())

def test_taskmethod_with_execute(executor):
    """taskmethod works with .execute() in a flow."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = ModelService("gpt-4")

    @dataclass
    class FlowUsingExecute(Task):
        async def run(self):
            # execute() now returns value directly
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                val1 = await service.generate("hello").execute()
                val2 = await service.generate("world").execute()
                val3 = await service.generate("hello").execute()
            return [val1, val2, val3]

    async def run():
        global execution_count
        flow = FlowUsingExecute()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == ["[gpt-4] hello", "[gpt-4] world", "[gpt-4] hello"]
        assert execution_count == 2  # Third was cached

    asyncio.run(run())

def test_taskmethod_execute_cached_false(executor):
    """taskmethod with execute(cached=False) skips cache."""
    global execution_count
    execution_count = 0

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        service = SimpleService()

    @dataclass
    class FlowUsingExecuteUncached(Task):
        async def run(self):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                val1 = await service.process("hello").execute()
                val2 = await service.process("hello").execute(cached=False)
            return [val1, val2]

    async def run():
        global execution_count
        flow = FlowUsingExecuteUncached()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == ["processed: hello", "processed: hello"]
        assert execution_count == 2  # Both ran

    asyncio.run(run())

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
        # executor.run returns value directly
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
        # First call
        value1 = await executor.run(as_task(service.generate, "hello"))
        assert value1 == "[gpt-4] hello"
        assert execution_count == 1

        # Same args - cache hit
        value2 = await executor.run(as_task(service.generate, "hello"))
        assert value2 == "[gpt-4] hello"
        assert execution_count == 1  # Cached!

        # Different args - cache miss
        value3 = await executor.run(as_task(service.generate, "world"))
        assert value3 == "[gpt-4] world"
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_with_cache_attrs(executor):
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
            # yield returns value directly
            val1 = yield as_task(service.generate, "step 1", cache_key=(service.model, "step 1"))
            val2 = yield as_task(service.generate, "step 2", cache_key=(service.model, "step 2"))
            val3 = yield as_task(service.generate, "step 1", cache_key=(service.model, "step 1"))  # cached
            return [val1, val2, val3]

    async def run():
        flow = FlowWithAsTask()
        value = await executor.run(flow)
        assert value == ["[gpt-4] step 1", "[gpt-4] step 2", "[gpt-4] step 1"]
        assert execution_count == 2  # Third was cached

    asyncio.run(run())

def test_as_task_with_execute(executor):
    """as_task works with .execute()."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    @dataclass
    class FlowWithAsTaskExecute(Task):
        async def run(self):
            # execute() returns value directly
            val1 = await as_task(service.generate, "hello", cache_key=(service.model, "hello")).execute()
            val2 = await as_task(service.generate, "hello", cache_key=(service.model, "hello")).execute()
            return [val1, val2]

    async def run():
        flow = FlowWithAsTaskExecute()
        value = await executor.run(flow)
        assert value == ["[gpt-4] hello", "[gpt-4] hello"]
        assert execution_count == 1  # Second was cached

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
        # First call - cached
        value1 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 1

        # With nocache - runs again
        value2 = await executor.run(nocache(as_task(service.generate, "hello")))
        assert execution_count == 2

        # Without nocache - cached
        value3 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_error_on_unbound(executor):
    """as_task now supports unbound functions too."""
    global execution_count
    execution_count = 0

    def standalone_func(x):
        global execution_count
        execution_count += 1
        return x * 2

    async def run():
        # as_task now works with plain functions
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
            # yield returns value directly
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

# --- unwrap tests (deprecated but still functional) ---

def test_unwrap_success(executor):
    """unwrap returns value directly on success (now default behavior)."""
    global execution_count
    execution_count = 0

    @dataclass
    class SuccessTask(Task):
        async def run(self):
            global execution_count
            execution_count += 1
            return "success_value"

    @dataclass
    class FlowWithUnwrap(Task):
        def run(self):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                # unwrap still works but is deprecated
                value = yield unwrap(SuccessTask())
            return f"got: {value}"

    async def run():
        flow = FlowWithUnwrap()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == "got: success_value"

    asyncio.run(run())

def test_unwrap_failure_raises(executor):
    """unwrap raises TaskError that can be caught inside the flow."""
    @dataclass
    class FailingTask(Task):
        async def run(self):
            raise ValueError("task failed")

    @dataclass
    class FlowWithUnwrapFailure(Task):
        def run(self):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    value = yield unwrap(FailingTask())
                return f"got: {value}"
            except TaskError as e:
                return f"caught error: {e.result.error}"

    async def run():
        flow = FlowWithUnwrapFailure()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == "caught error: task failed"

    asyncio.run(run())

def test_unwrap_with_nocache(executor):
    """unwrap works with nocache."""
    global execution_count
    execution_count = 0

    from eggflow import nocache

    @dataclass
    class CountingTask(Task):
        name: str
        async def run(self):
            global execution_count
            execution_count += 1
            return f"value_{self.name}"

    @dataclass
    class FlowWithUnwrapNocache(Task):
        def run(self):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                v1 = yield unwrap(CountingTask("a"))
                v2 = yield unwrap(nocache(CountingTask("a")))  # runs again
            return [v1, v2]

    async def run():
        flow = FlowWithUnwrapNocache()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == ["value_a", "value_a"]
        assert execution_count == 2  # Second ran again due to nocache

    asyncio.run(run())

def test_unwrap_in_parallel_list(executor):
    """unwrap works in parallel lists - but now default returns values."""
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
    class FlowParallelUnwrap(Task):
        def run(self):
            # Now by default all return values
            # Use wrapped() to get Result
            results = yield [ValueTask(1), ValueTask(2), wrapped(ValueTask(3))]
            return results

    async def run():
        flow = FlowParallelUnwrap()
        value = await executor.run(flow)
        assert value[0] == 2  # value
        assert value[1] == 4  # value
        assert isinstance(value[2], Result)  # Result object
        assert value[2].value == 6

    asyncio.run(run())

def test_execute_unwrap_success(executor):
    """execute() returns value directly by default (unwrap parameter deprecated)."""
    global execution_count
    execution_count = 0

    @dataclass
    class SimpleTask(Task):
        async def run(self):
            global execution_count
            execution_count += 1
            return "direct_value"

    @dataclass
    class FlowWithExecuteUnwrap(Task):
        async def run(self):
            # execute() now returns value by default
            value = await SimpleTask().execute()
            return f"got: {value}"

    async def run():
        flow = FlowWithExecuteUnwrap()
        value = await executor.run(flow)
        assert value == "got: direct_value"

    asyncio.run(run())

def test_execute_unwrap_failure(executor):
    """execute() raises TaskError on failure by default."""
    @dataclass
    class FailTask(Task):
        async def run(self):
            raise ValueError("execute failed")

    @dataclass
    class FlowWithExecuteUnwrapFail(Task):
        async def run(self):
            try:
                await FailTask().execute()
                return "should not reach"
            except TaskError as e:
                return f"caught: {e.result.error}"

    async def run():
        flow = FlowWithExecuteUnwrapFail()
        value = await executor.run(flow)
        assert value == "caught: execute failed"

    asyncio.run(run())

def test_as_task_function_with_unwrap(executor):
    """as_task function works with unwrap (deprecated)."""
    global execution_count
    execution_count = 0

    @dataclass
    class FlowFuncUnwrap(Task):
        def run(self):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                value = yield unwrap(as_task(async_compute, 10, 20))
            return f"sum is {value}"

    async def run():
        flow = FlowFuncUnwrap()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = await executor.run(flow)
        assert value == "sum is 30"

    asyncio.run(run())

# --- New wrapped() tests ---

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
