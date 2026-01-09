import asyncio
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, Result

@dataclass
class UnreliableTask(TaskSpec):
    name: str
    
    async def run(self):
        # We use the 'attempt' field (inherited from TaskSpec) to determine behavior
        print(f"  [Task] Running '{self.name}' (Attempt {self.attempt})")
        if self.attempt < 2:
            raise Exception("Artificial Failure!")
        return f"Success on attempt {self.attempt}"

@dataclass
class RobustJob(TaskSpec):
    """A workflow that retries the sub-task up to 3 times."""
    target_name: str
    max_retries: int = 3

    def run(self):
        print(f"Workflow: Starting robust execution for {self.target_name}...")
        
        last_error = None
        for i in range(self.max_retries):
            # 1. Create task for this specific attempt
            # The 'attempt' field changes the hash, creating a new DB entry
            task = UnreliableTask(name=self.target_name, attempt=i)
            
            # 2. Yield to executor
            # If this task ran before and failed, executor returns cached error instantly.
            res = yield task
            
            if res.is_success:
                print(f"Workflow: Succeeded on attempt {i}!")
                return res.value
            
            print(f"Workflow: Attempt {i} failed with: {res.error}")
            last_error = res.error
            
        return f"Workflow Failed after {self.max_retries} tries. Last error: {last_error}"

async def main():
    store = JobStore("retry_test.db")
    executor = EggFlowExecutor(store)
    
    # Clean DB to prove fresh run logic
    store.conn.execute("DELETE FROM jobs")
    store.conn.commit()
    
    print(">>> STARTING RETRY WORKFLOW")
    job = RobustJob("MyCriticalData")
    res = await executor.run(job)
    print(f"\nFinal Result: {res.value}")

if __name__ == "__main__":
    asyncio.run(main())
