import asyncio
from dataclasses import dataclass
from eggflow import Task

@dataclass
class UnreliableTask(Task):
    name: str
    attempt: int = 0

    async def run(self):
        if self.attempt < 2:
            raise Exception("Artificial Failure!")
        return f"Success on attempt {self.attempt}"

@dataclass
class RobustJob(Task):
    target_name: str
    max_retries: int = 3

    def run(self):
        for i in range(self.max_retries):
            task = UnreliableTask(name=self.target_name, attempt=i)
            res = yield task

            if res.is_success:
                return res.value

        return "Workflow Failed"

def test_retry_workflow(executor):
    async def run():
        job = RobustJob("MyCriticalData")
        res = await executor.run(job)
        assert res.is_success
        assert res.value == "Success on attempt 2"
    asyncio.run(run())
