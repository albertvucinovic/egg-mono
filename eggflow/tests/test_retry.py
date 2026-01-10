import asyncio
import os
from dataclasses import dataclass, field
from eggflow import FlowExecutor, TaskStore, Task, Result

@dataclass
class UnreliableTask(Task):
    name: str
    # 'attempt' must be defined here to be part of cache key
    attempt: int = 0
    
    async def run(self):
        print(f"  [Task] Running '{self.name}' (Attempt {self.attempt})")
        if self.attempt < 2:
            raise Exception("Artificial Failure!")
        return f"Success on attempt {self.attempt}"

@dataclass
class RobustJob(Task):
    target_name: str
    max_retries: int = 3

    def run(self):
        print(f"Workflow: Starting robust execution for {self.target_name}...")
        for i in range(self.max_retries):
            # Pass 'attempt' to ensure distinct cache key
            task = UnreliableTask(name=self.target_name, attempt=i)
            res = yield task
            
            if res.is_success:
                print(f"Workflow: Succeeded on attempt {i}!")
                return res.value
            
            print(f"Workflow: Attempt {i} failed with: {res.error}")
            
        return f"Workflow Failed"

async def main():
    if os.path.exists("retry_test.db"):
        os.remove("retry_test.db")
        
    store = TaskStore("retry_test.db")
    executor = FlowExecutor(store)
    
    print(">>> STARTING RETRY WORKFLOW")
    job = RobustJob("MyCriticalData")
    res = await executor.run(job)
    print(f"\nFinal Result: {res.value}")

if __name__ == "__main__":
    asyncio.run(main())
