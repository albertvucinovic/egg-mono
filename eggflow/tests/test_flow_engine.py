import asyncio
import pickle
from dataclasses import dataclass
from eggflow import Task, Result, CreateThread, ContinueThread, ForkThread, ThreadResult, wrapped, TaskError

@dataclass
class SimpleEcho(Task):
    message: str
    async def run(self):
        return f"Echo: {self.message}"

def test_simple_execution(executor):
    async def run():
        task = SimpleEcho("Hello")
        # executor.run now returns value directly
        value = await executor.run(task)
        assert value == "Echo: Hello"
    asyncio.run(run())

def test_caching(executor, store):
    async def run():
        task = SimpleEcho("CacheMe")
        value1 = await executor.run(task)
        assert value1 == "Echo: CacheMe"

        key = task.get_cache_key()
        store.conn.execute(
            "UPDATE tasks SET result_blob=? WHERE cache_key=?",
            (pickle.dumps(Result(value="Hacked")), key)
        )
        store.conn.commit()

        value2 = await executor.run(task)
        assert value2 == "Hacked"
    asyncio.run(run())

@dataclass
class FlakyTask(Task):
    attempt: int = 0
    async def run(self):
        if self.attempt < 2:
            raise ValueError("Fail")
        return "Success"

@dataclass
class RetryWorkflow(Task):
    def run(self):
        for i in range(3):
            # Use wrapped() to get Result for checking success
            res = yield wrapped(FlakyTask(attempt=i))
            if res.is_success:
                return res.value
        return "Failed"

def test_retry_logic(executor):
    async def run():
        wf = RetryWorkflow()
        value = await executor.run(wf)
        assert value == "Success"

        cur = executor.store.conn.execute("SELECT result_blob FROM tasks")
        errors = 0
        successes = 0
        for row in cur:
            r = pickle.loads(row[0])
            if r.error: errors += 1
            else: successes += 1
        assert errors == 2
        assert successes == 2
    asyncio.run(run())

def test_mock_create_thread(executor):
    async def run():
        t = CreateThread(prompt="Hello Egg")
        value = await executor.run(t)
        assert isinstance(value, ThreadResult)
        assert "Mock Reply" in value.content
    asyncio.run(run())

@dataclass
class ForkFlow(Task):
    def run(self):
        # yield now returns value directly
        root_data = yield CreateThread("Root")

        fork_id = yield ForkThread(root_data.thread_id)

        cont_result = yield ContinueThread(thread_id=fork_id, content="Child")
        return cont_result

def test_mock_fork_thread(executor):
    async def run():
        value = await executor.run(ForkFlow())
        assert isinstance(value, ThreadResult)
        assert "fork" in value.thread_id
        assert "Mock Reply" in value.content
    asyncio.run(run())


def test_cached_task_restore_hook_runs_without_reexecuting(executor):
    events = []

    @dataclass
    class Materialized(Task):
        name: str

        def run(self):
            events.append("run")
            return "durable-value"

        def restore(self, value):
            events.append(("restore", value))
            return value

    async def scenario():
        assert await executor.run(Materialized("same")) == "durable-value"
        assert await executor.run(Materialized("same")) == "durable-value"

    asyncio.run(scenario())
    assert events == ["run", ("restore", "durable-value")]


def test_keyed_cached_task_delegates_restore_hook(executor):
    from eggflow import keyed

    events = []

    @dataclass
    class Materialized(Task):
        def run(self):
            events.append("run")
            return "value"

        def restore(self, value):
            events.append(("restore", value))
            return value

    async def scenario():
        assert await executor.run(keyed(Materialized(), "scope")) == "value"
        assert await executor.run(keyed(Materialized(), "scope")) == "value"

    asyncio.run(scenario())
    assert events == ["run", ("restore", "value")]


def test_cached_restore_failure_is_not_misreported_as_pickle_corruption(executor):
    import pytest

    @dataclass
    class Materialized(Task):
        fail_restore: bool = False

        def get_cache_key(self):
            return "same-materialization"

        def run(self):
            return "value"

        def restore(self, value):
            if self.fail_restore:
                raise RuntimeError("durable bytes are missing")
            return value

    async def scenario():
        assert await executor.run(Materialized()) == "value"
        with pytest.raises(
            TaskError, match="Completed Task restore failed.*durable bytes are missing"
        ):
            await executor.run(Materialized(fail_restore=True))

    asyncio.run(scenario())
