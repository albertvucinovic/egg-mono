import asyncio
import hashlib
from dataclasses import dataclass
from eggflow import Task, CreateThread, ContinueThread, ForkThread

@dataclass
class EvaluateCandidate(Task):
    """Runs a candidate thread against a benchmark."""
    candidate_thread_id: str
    benchmark_question: str
    expected_answer_keyword: str

    def run(self):
        # yield now returns value directly
        test_fork = yield ForkThread(self.candidate_thread_id)

        answer_res = yield ContinueThread(test_fork, self.benchmark_question)

        val = answer_res.content
        score = 0
        if "mock" in val.lower():
            if self.expected_answer_keyword.lower() in val.lower():
                score = 10
            else:
                h = int(hashlib.md5(test_fork.encode()).hexdigest(), 16)
                score = h % 10
        else:
            score = 10 if self.expected_answer_keyword.lower() in val.lower() else 0

        return score

@dataclass
class MutateCandidate(Task):
    """Takes a thread, forks it, and asks the LLM to improve its own system prompt."""
    parent_thread_id: str
    feedback: str

    def run(self):
        # yield now returns value directly
        child_id = yield ForkThread(self.parent_thread_id)

        mutation_prompt = (
            f"REFLECTION: Your previous performance scored {self.feedback}.\n"
            "TASK: Propose a slightly different System Prompt / Persona to improve reasoning.\n"
            "OUTPUT: ONLY the new System Prompt."
        )

        new_prompt_res = yield ContinueThread(child_id, mutation_prompt)

        new_candidate = yield CreateThread(
            prompt="I am ready.",
            system_prompt=new_prompt_res.content
        )

        return new_candidate.thread_id

@dataclass
class GEPA(Task):
    initial_prompt: str
    generations: int = 3
    population_size: int = 4

    def run(self):
        # yield now returns value directly (ThreadResult)
        root_res = yield CreateThread(prompt="I am ready.", system_prompt=self.initial_prompt)
        population = []
        fork_specs = [ForkThread(root_res.thread_id) for _ in range(self.population_size)]
        # List yields return values directly
        fork_ress = yield fork_specs
        population = fork_ress  # Already values

        benchmark = [
            ("What is 2+2?", "4"),
            ("Capital of France?", "Paris")
        ]

        for gen in range(self.generations):
            scores = []
            for tid in population:
                test_tasks = [EvaluateCandidate(tid, q, a) for q, a in benchmark]
                results = yield test_tasks
                # results are values now
                total_score = sum(results)
                scores.append((total_score, tid))

            scores.sort(key=lambda x: x[0], reverse=True)
            keep_n = max(1, self.population_size // 2)
            top_half = scores[:keep_n]

            if gen == self.generations - 1:
                return top_half[0][1]

            new_population = []
            for score, tid in top_half:
                needed = self.population_size // len(top_half)
                mutations = [
                    MutateCandidate(tid, f"Score {score}")
                    for _ in range(needed)
                ]
                children = yield mutations
                # children are values now
                new_population.extend(children)

            while len(new_population) < self.population_size:
                m = yield MutateCandidate(top_half[0][1], "Padding")
                new_population.append(m)

            population = new_population

        return population[0]

def test_gepa(executor):
    async def run():
        algo = GEPA(initial_prompt="You are a helpful assistant.", generations=2, population_size=4)
        # executor.run now returns value directly
        value = await executor.run(algo)
        assert value is not None
    asyncio.run(run())
