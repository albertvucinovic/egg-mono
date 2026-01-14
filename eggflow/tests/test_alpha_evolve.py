import asyncio
from dataclasses import dataclass
from eggflow import Task, CreateThread, ContinueThread, ForkThread

@dataclass
class ExecuteCode(Task):
    """Simulates running code extracted from the thread."""
    thread_id: str
    iteration: int

    async def run(self):
        if self.iteration >= 2:
            return "Success: Tests Passed."
        return "Error: NameError 'x' is not defined"

@dataclass
class AlphaEvolve(Task):
    problem_description: str
    beam_width: int = 2
    max_depth: int = 4

    def run(self):
        # yield now returns value directly (ThreadResult)
        root = yield CreateThread(
            prompt=f"Write a Python script for: {self.problem_description}",
            model_key="gpt-4o"
        )

        beam = [root.thread_id]

        for depth in range(self.max_depth):
            candidates = []

            for i, tid in enumerate(beam):
                output = yield ExecuteCode(tid, depth)

                if "Error" not in output:
                    return tid

                fork_ids = yield [ForkThread(tid) for _ in range(2)]

                fix_specs = [
                    ContinueThread(
                        f,
                        f"Execution failed with: {output}\nFix the code. Strategy {k+1}."
                    )
                    for k, f in enumerate(fork_ids)
                ]
                yield fix_specs

                candidates.extend(fork_ids)

            beam = candidates[:self.beam_width]

        return beam[0]

def test_alpha_evolve(executor):
    async def run():
        algo = AlphaEvolve("Calculate Fibonacci sequence", beam_width=2, max_depth=5)
        # executor.run now returns value directly
        value = await executor.run(algo)
        assert value is not None
    asyncio.run(run())
