import asyncio
import hashlib
from dataclasses import dataclass
from eggflow import Task, CreateThread, ContinueThread, ForkThread

@dataclass
class TreeOfThoughts(Task):
    problem: str
    depth: int = 3
    branch_factor: int = 3

    def run(self):
        # yield now returns value directly (ThreadResult)
        root = yield CreateThread(
            prompt=f"Let's solve this step by step. Problem: {self.problem}",
            model_key="gpt-4o"
        )
        current_best_id = root.thread_id

        for step in range(self.depth):
            fork_specs = [ForkThread(current_best_id) for _ in range(self.branch_factor)]
            # List yields return values directly
            fork_ids = yield fork_specs

            expand_specs = [
                ContinueThread(tid, "Generate the next logical step. Be concise.")
                for tid in fork_ids
            ]
            expand_results = yield expand_specs

            scores = []
            for i, res in enumerate(expand_results):
                # res is ThreadResult directly
                content = res.content
                tid = fork_ids[i]

                grader_res = yield CreateThread(
                    prompt=f"Rate the following step for the problem '{self.problem}':\n{content}\nReturn a number 0-10.",
                    model_key="gpt-4o-mini"
                )

                try:
                    score = int(''.join(filter(str.isdigit, grader_res.content))) % 11
                except:
                    score = 0

                if "Mock" in content:
                    score = int(hashlib.md5(tid.encode()).hexdigest(), 16) % 10

                scores.append((score, tid))

            scores.sort(key=lambda x: x[0], reverse=True)
            best_score, best_tid = scores[0]

            current_best_id = best_tid

        final = yield ContinueThread(current_best_id, "Summarize the final solution.")
        return final.content

def test_tree_of_thoughts(executor):
    async def run():
        tot = TreeOfThoughts("How do I build a dyson sphere?", depth=2, branch_factor=2)
        # executor.run now returns value directly
        value = await executor.run(tot)
        assert value is not None
    asyncio.run(run())
