import asyncio
import pickle
from dataclasses import dataclass
from eggflow import Task, Result

@dataclass
class CriticalTask(Task):
    name: str

    async def run(self):
        return f"Secret Data for {self.name}"

def test_corruption_handling(executor, store):
    async def run():
        task_a = CriticalTask(name="A")
        res = await executor.run(task_a)
        assert res.is_success
        assert res.value == "Secret Data for A"

        key = task_a.get_cache_key()
        store.conn.execute(
            "UPDATE tasks SET result_blob=? WHERE cache_key=?",
            (b'GARBAGE_DATA', key)
        )
        store.conn.commit()

        res = await executor.run(task_a)
        assert not res.is_success
        assert "corrupt" in res.metadata or "error" in str(res.error).lower()
    asyncio.run(run())
