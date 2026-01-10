import pytest
import asyncio
import os
import pickle
from dataclasses import dataclass
from eggflow import FlowExecutor, TaskStore, Task, Result, CreateThread, ContinueThread, ForkThread, Config, ThreadResult

def async_test(coro):
    def wrapper(*args, **kwargs):
        return asyncio.run(coro(*args, **kwargs))
    return wrapper

@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_flow.db"
    return TaskStore(str(db_file))

@pytest.fixture
def executor(store):
    return FlowExecutor(store)

@pytest.fixture(autouse=True)
def mock_mode():
    Config.MOCK_MODE = True
    yield
    Config.MOCK_MODE = True

@dataclass
class SimpleEcho(Task):
    message: str
    async def run(self):
        return f"Echo: {self.message}"

def test_simple_execution(executor):
    async def run():
        task = SimpleEcho("Hello")
        res = await executor.run(task)
        assert res.is_success
        assert res.value == "Echo: Hello"
    asyncio.run(run())

def test_caching(executor, store):
    async def run():
        task = SimpleEcho("CacheMe")
        res1 = await executor.run(task)
        assert res1.value == "Echo: CacheMe"
        
        key = task.get_cache_key()
        store.conn.execute(
            "UPDATE tasks SET result_blob=? WHERE cache_key=?", 
            (pickle.dumps(Result(value="Hacked")), key)
        )
        store.conn.commit()
        
        res2 = await executor.run(task)
        assert res2.value == "Hacked"
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
            res = yield FlakyTask(attempt=i)
            if res.is_success:
                return res.value
        return "Failed"

def test_retry_logic(executor):
    async def run():
        wf = RetryWorkflow()
        res = await executor.run(wf)
        assert res.value == "Success"
        
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
        res = await executor.run(t)
        assert isinstance(res.value, ThreadResult)
        assert "Mock Reply" in res.value.content
    asyncio.run(run())

@dataclass
class ForkFlow(Task):
    def run(self):
        root_res = yield CreateThread("Root")
        root_data = root_res.value
        
        fork_id_res = yield ForkThread(root_data.thread_id)
        fork_id = fork_id_res.value
        
        cont_res = yield ContinueThread(thread_id=fork_id, content="Child")
        return cont_res.value

def test_mock_fork_thread(executor):
    async def run():
        res = await executor.run(ForkFlow())
        assert isinstance(res.value, ThreadResult)
        assert "fork" in res.value.thread_id
        assert "Mock Reply" in res.value.content
    asyncio.run(run())
