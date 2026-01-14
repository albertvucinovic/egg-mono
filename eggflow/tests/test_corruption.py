import asyncio
import pickle
from dataclasses import dataclass
from eggflow import Task, Result, TaskError, wrapped

@dataclass
class CriticalTask(Task):
    name: str

    async def run(self):
        return f"Secret Data for {self.name}"

def test_corruption_handling(executor, store):
    async def run():
        task_a = CriticalTask(name="A")
        value = await executor.run(task_a)
        assert value == "Secret Data for A"

        key = task_a.get_cache_key()
        store.conn.execute(
            "UPDATE tasks SET result_blob=? WHERE cache_key=?",
            (b'GARBAGE_DATA', key)
        )
        store.conn.commit()

        # Use wrapped() to get Result with error info
        res = await executor.run(wrapped(task_a))
        assert not res.is_success
        assert "corrupt" in res.metadata or "error" in str(res.error).lower()
    asyncio.run(run())
