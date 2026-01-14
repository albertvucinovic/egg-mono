import asyncio
from dataclasses import dataclass
from eggflow import Task, Result, taskmethod, as_task

# Counter to track executions
execution_count = 0

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
            res = yield service.process("hello")
            return res.value

    async def run():
        flow = Flow()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == "processed: hello"
        assert execution_count == 1

    asyncio.run(run())

def test_taskmethod_caching(executor):
    """Same args = cache hit."""
    global execution_count
    execution_count = 0

    service = SimpleService()

    async def run():
        # First call
        res1 = await executor.run(service.process("hello"))
        assert res1.value == "processed: hello"
        assert execution_count == 1

        # Second call - same args, should be cached
        res2 = await executor.run(service.process("hello"))
        assert res2.value == "processed: hello"
        assert execution_count == 1  # No new execution

        # Different args - cache miss
        res3 = await executor.run(service.process("world"))
        assert res3.value == "processed: world"
        assert execution_count == 2

    asyncio.run(run())

def test_taskmethod_instance_attr(executor):
    """Instance attr affects cache key."""
    global execution_count
    execution_count = 0

    service_gpt = ModelService("gpt-4")
    service_claude = ModelService("claude")

    async def run():
        # Call with gpt-4
        res1 = await executor.run(service_gpt.generate("hello"))
        assert res1.value == "[gpt-4] hello"
        assert execution_count == 1

        # Same prompt, different model - cache miss
        res2 = await executor.run(service_claude.generate("hello"))
        assert res2.value == "[claude] hello"
        assert execution_count == 2

        # Same model and prompt - cache hit
        res3 = await executor.run(service_gpt.generate("hello"))
        assert res3.value == "[gpt-4] hello"
        assert execution_count == 2  # No new execution

    asyncio.run(run())

def test_taskmethod_non_cache_attr_ignored(executor):
    """Attrs not in cache_attrs don't affect cache key."""
    global execution_count
    execution_count = 0

    service1 = ModelService("gpt-4")
    service1.debug = True

    service2 = ModelService("gpt-4")
    service2.debug = False

    async def run():
        res1 = await executor.run(service1.generate("hello"))
        assert res1.value == "[gpt-4] hello"
        assert execution_count == 1

        # Different debug value, but same model - cache hit
        res2 = await executor.run(service2.generate("hello"))
        assert res2.value == "[gpt-4] hello"
        assert execution_count == 1  # Cached!

    asyncio.run(run())

def test_taskmethod_multiple_attrs(executor):
    """Multiple instance attrs in cache key."""
    global execution_count
    execution_count = 0

    service1 = MultiAttrService("gpt-4", 0.7)
    service2 = MultiAttrService("gpt-4", 0.9)
    service3 = MultiAttrService("claude", 0.7)

    async def run():
        res1 = await executor.run(service1.complete("hello"))
        assert res1.value == "[gpt-4@0.7] hello"
        assert execution_count == 1

        # Same model, different temperature - cache miss
        res2 = await executor.run(service2.complete("hello"))
        assert res2.value == "[gpt-4@0.9] hello"
        assert execution_count == 2

        # Different model, same temperature - cache miss
        res3 = await executor.run(service3.complete("hello"))
        assert res3.value == "[claude@0.7] hello"
        assert execution_count == 3

        # Original params - cache hit
        res4 = await executor.run(service1.complete("hello"))
        assert res4.value == "[gpt-4@0.7] hello"
        assert execution_count == 3

    asyncio.run(run())

def test_taskmethod_in_flow(executor):
    """Use taskmethod inside a flow with yields."""
    global execution_count
    execution_count = 0

    @dataclass
    class Pipeline(Task):
        def run(self):
            service = ModelService("gpt-4")
            # First call
            res1 = yield service.generate("step 1")
            # Second call with different prompt
            res2 = yield service.generate("step 2")
            # Third call same as first - should use cache
            res3 = yield service.generate("step 1")
            return [res1.value, res2.value, res3.value]

    async def run():
        flow = Pipeline()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ["[gpt-4] step 1", "[gpt-4] step 2", "[gpt-4] step 1"]
        assert execution_count == 2  # Third was cached

    asyncio.run(run())

def test_taskmethod_with_nocache(executor):
    """taskmethod works with nocache wrapper."""
    global execution_count
    execution_count = 0

    from eggflow import nocache

    service = SimpleService()

    async def run():
        # First call - cached
        res1 = await executor.run(service.process("hello"))
        assert execution_count == 1

        # With nocache - runs again
        res2 = await executor.run(nocache(service.process("hello")))
        assert execution_count == 2

        # Without nocache - cached
        res3 = await executor.run(service.process("hello"))
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

    service = KwargsService()

    async def run():
        res1 = await executor.run(service.process("hello"))
        assert res1.value == "hello"
        assert execution_count == 1

        # Different kwarg - cache miss
        res2 = await executor.run(service.process("hello", prefix=">>> "))
        assert res2.value == ">>> hello"
        assert execution_count == 2

        # Same args and kwargs - cache hit
        res3 = await executor.run(service.process("hello", prefix=">>> "))
        assert res3.value == ">>> hello"
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

    service = SyncService()

    async def run():
        res1 = await executor.run(service.compute(5))
        assert res1.value == 10
        assert execution_count == 1

        # Cache hit
        res2 = await executor.run(service.compute(5))
        assert res2.value == 10
        assert execution_count == 1

    asyncio.run(run())

def test_taskmethod_with_execute(executor):
    """taskmethod works with .execute() in a flow."""
    global execution_count
    execution_count = 0

    service = ModelService("gpt-4")

    @dataclass
    class FlowUsingExecute(Task):
        async def run(self):
            # Use execute() instead of yield
            res1 = await service.generate("hello").execute()
            res2 = await service.generate("world").execute()
            # Same as first - should be cached
            res3 = await service.generate("hello").execute()
            return [res1.value, res2.value, res3.value]

    async def run():
        global execution_count
        flow = FlowUsingExecute()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ["[gpt-4] hello", "[gpt-4] world", "[gpt-4] hello"]
        assert execution_count == 2  # Third was cached

    asyncio.run(run())

def test_taskmethod_execute_cached_false(executor):
    """taskmethod with execute(cached=False) skips cache."""
    global execution_count
    execution_count = 0

    service = SimpleService()

    @dataclass
    class FlowUsingExecuteUncached(Task):
        async def run(self):
            # First call - cached
            res1 = await service.process("hello").execute()
            # Second call - skip cache
            res2 = await service.process("hello").execute(cached=False)
            return [res1.value, res2.value]

    async def run():
        global execution_count
        flow = FlowUsingExecuteUncached()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ["processed: hello", "processed: hello"]
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
        res = await executor.run(as_task(service.generate, "hello"))
        assert res.is_success
        assert res.value == "[gpt-4] hello"
        assert execution_count == 1

    asyncio.run(run())

def test_as_task_caching(executor):
    """as_task caches based on args."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    async def run():
        # First call
        res1 = await executor.run(as_task(service.generate, "hello"))
        assert res1.value == "[gpt-4] hello"
        assert execution_count == 1

        # Same args - cache hit
        res2 = await executor.run(as_task(service.generate, "hello"))
        assert res2.value == "[gpt-4] hello"
        assert execution_count == 1  # Cached!

        # Different args - cache miss
        res3 = await executor.run(as_task(service.generate, "world"))
        assert res3.value == "[gpt-4] world"
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_with_cache_attrs(executor):
    """as_task with cache_attrs includes instance state in key."""
    global execution_count
    execution_count = 0

    service_gpt = ExternalService("gpt-4")
    service_claude = ExternalService("claude")

    async def run():
        # Call with gpt-4
        res1 = await executor.run(as_task(service_gpt.generate, "hello", cache_attrs=('model',)))
        assert res1.value == "[gpt-4] hello"
        assert execution_count == 1

        # Same prompt, different model - cache miss
        res2 = await executor.run(as_task(service_claude.generate, "hello", cache_attrs=('model',)))
        assert res2.value == "[claude] hello"
        assert execution_count == 2

        # Same model and prompt - cache hit
        res3 = await executor.run(as_task(service_gpt.generate, "hello", cache_attrs=('model',)))
        assert res3.value == "[gpt-4] hello"
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
            res1 = yield as_task(service.generate, "step 1", cache_attrs=('model',))
            res2 = yield as_task(service.generate, "step 2", cache_attrs=('model',))
            res3 = yield as_task(service.generate, "step 1", cache_attrs=('model',))  # cached
            return [res1.value, res2.value, res3.value]

    async def run():
        flow = FlowWithAsTask()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ["[gpt-4] step 1", "[gpt-4] step 2", "[gpt-4] step 1"]
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
            res1 = await as_task(service.generate, "hello", cache_attrs=('model',)).execute()
            res2 = await as_task(service.generate, "hello", cache_attrs=('model',)).execute()
            return [res1.value, res2.value]

    async def run():
        flow = FlowWithAsTaskExecute()
        res = await executor.run(flow)
        assert res.is_success
        assert res.value == ["[gpt-4] hello", "[gpt-4] hello"]
        assert execution_count == 1  # Second was cached

    asyncio.run(run())

def test_as_task_sync_method(executor):
    """as_task works with sync methods."""
    global execution_count
    execution_count = 0

    service = ExternalService("gpt-4")

    async def run():
        res = await executor.run(as_task(service.compute, 5))
        assert res.value == 10
        assert execution_count == 1

        # Cache hit
        res2 = await executor.run(as_task(service.compute, 5))
        assert res2.value == 10
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
        res1 = await executor.run(as_task(service.process, "hello"))
        assert res1.value == "hello"
        assert execution_count == 1

        # Different kwarg - cache miss
        res2 = await executor.run(as_task(service.process, "hello", prefix=">>> "))
        assert res2.value == ">>> hello"
        assert execution_count == 2

        # Same - cache hit
        res3 = await executor.run(as_task(service.process, "hello", prefix=">>> "))
        assert res3.value == ">>> hello"
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
        res1 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 1

        # With nocache - runs again
        res2 = await executor.run(nocache(as_task(service.generate, "hello")))
        assert execution_count == 2

        # Without nocache - cached
        res3 = await executor.run(as_task(service.generate, "hello"))
        assert execution_count == 2

    asyncio.run(run())

def test_as_task_error_on_unbound(executor):
    """as_task raises TypeError for unbound functions."""
    def standalone_func(x):
        return x

    try:
        as_task(standalone_func, 5)
        assert False, "Should have raised TypeError"
    except TypeError as e:
        assert "bound method" in str(e)
