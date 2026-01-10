import asyncio
import pickle
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, Result

@dataclass
class CriticalTask(TaskSpec):
    name: str
    
    async def run(self):
        print(f"  [Task] Executing Critical Task '{self.name}'")
        return f"Secret Data for {self.name}"

async def main():
    store = JobStore("corruption_test.db")
    executor = EggFlowExecutor(store)
    
    # 1. Run Task Successfully
    print("--- Step 1: Initial Run ---")
    task_a = CriticalTask(name="A", rerun_on_corruption=True)
    res = await executor.run(task_a)
    print(f"Result: {res.value}")
    
    # 2. Corrupt the DB Entry
    print("\n--- Step 2: Corrupting DB ---")
    key = task_a.get_cache_key()
    store.conn.execute(
        "UPDATE jobs SET result_blob=? WHERE cache_key=?", 
        (b'GARBAGE_DATA', key)
    )
    store.conn.commit()
    
    # 3. Rerun (Policy: rerun_on_corruption=True)
    print("--- Step 3: Rerun with Auto-Healing ---")
    res = await executor.run(task_a)
    if res.is_success:
        print(f"Recovered: {res.value}")
    else:
        print(f"Failed: {res.error}")

    # 4. Strict Task (Policy: rerun_on_corruption=False)
    print("\n--- Step 4: Strict Task ---")
    task_b = CriticalTask(name="B", rerun_on_corruption=False)
    await executor.run(task_b) # First run ok
    
    # Corrupt it
    key_b = task_b.get_cache_key()
    store.conn.execute("UPDATE jobs SET result_blob=? WHERE cache_key=?", (b'GARBAGE_DATA', key_b))
    store.conn.commit()
    
    # Rerun
    print("--- Step 5: Rerun Strict Task ---")
    res_b = await executor.run(task_b)
    if res_b.is_success:
        print(f"Recovered: {res_b.value}")
    else:
        print(f"Strict Policy Triggered Error: {res_b.error}")

if __name__ == "__main__":
    asyncio.run(main())
