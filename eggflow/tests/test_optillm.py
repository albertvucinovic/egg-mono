import asyncio
from dataclasses import dataclass
from eggflow import Task, CreateThread

@dataclass
class BestOfN(Task):
    prompt: str
    n: int = 5
    grader_model: str = "gpt-4o-mini"

    def run(self):
        tasks = [
            CreateThread(
                prompt=self.prompt,
                seed=i,
                model_key="gpt-4o"
            ) for i in range(self.n)
        ]

        # List yields return values directly
        results = yield tasks

        candidates = []
        for i, res in enumerate(results):
            # res is ThreadResult directly
            val = res.content
            candidates.append(f"Option {i}:\n{val}\n")

        judge_prompt = (
            f"Compare the following {self.n} responses to the prompt: '{self.prompt}'\n\n"
            + "---\n".join(candidates) +
            "\n---\nSelect the best option. Return ONLY the index (e.g. 'Option 1')."
        )

        # yield returns value directly
        selection_res = yield CreateThread(
            prompt=judge_prompt,
            model_key=self.grader_model
        )

        selection = selection_res.content

        best_idx = 0
        for i in range(self.n):
            if f"Option {i}" in selection:
                best_idx = i
                break

        return results[best_idx].content

def test_best_of_n(executor):
    async def run():
        task = BestOfN(prompt="Explain the theory of relativity in one sentence.", n=3)
        # executor.run now returns value directly
        value = await executor.run(task)
        assert value is not None
    asyncio.run(run())
