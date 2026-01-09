import asyncio
from dataclasses import dataclass
from typing import List, Tuple
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread, ForkThread, Result

# --- Helper: The Fitness Function ---
@dataclass
class EvaluateCandidate(TaskSpec):
    """Runs a candidate thread against a benchmark."""
    candidate_thread_id: str
    benchmark_question: str
    expected_answer_keyword: str

    def run(self):
        # 1. Fork the candidate so the test doesn't pollute the prompt's history
        test_fork = yield ForkThread(self.candidate_thread_id)
        
        # 2. Ask the question
        answer_res = yield ContinueThread(test_fork.value, self.benchmark_question)
        
        # 3. Simple Keyword Grading (In reality, use another LLM to grade)
        # Mocking logic for test without real LLM
        val = answer_res.value
        score = 0
        if "mock" in val.lower(): # Mock usually returns "Mock Reply..."
             if self.expected_answer_keyword.lower() in val.lower():
                 score = 10
             else:
                 # Deterministic mock scoring based on thread ID for variety
                 import hashlib
                 h = int(hashlib.md5(test_fork.value.encode()).hexdigest(), 16)
                 score = h % 10 
        else:
             score = 10 if self.expected_answer_keyword.lower() in val.lower() else 0
             
        return score

@dataclass
class MutateCandidate(TaskSpec):
    """Takes a thread, forks it, and asks the LLM to improve its own system prompt."""
    parent_thread_id: str
    feedback: str

    def run(self):
        # 1. Fork to create the child
        child_id_res = yield ForkThread(self.parent_thread_id)
        child_id = child_id_res.value
        
        # 2. Meta-Prompting
        mutation_prompt = (
            f"REFLECTION: Your previous performance scored {self.feedback}.\n"
            "TASK: Propose a slightly different System Prompt / Persona to improve reasoning.\n"
            "OUTPUT: ONLY the new System Prompt."
        )
        
        new_prompt_res = yield ContinueThread(child_id, mutation_prompt)
        
        # 3. Create a clean thread with the NEW prompt
        new_candidate = yield CreateThread(
            prompt="I am ready.", 
            system_prompt=new_prompt_res.value
        )
        
        return new_candidate.metadata['thread_id']

# --- The Main Algorithm ---
@dataclass
class GEPA(TaskSpec):
    initial_prompt: str
    generations: int = 3
    population_size: int = 4

    def run(self):
        print(f"--- Gen 0: Initialization ---")
        root_res = yield CreateThread(prompt="I am ready.", system_prompt=self.initial_prompt)
        # Start with clones of root
        population = []
        fork_specs = [ForkThread(root_res.metadata['thread_id']) for _ in range(self.population_size)]
        fork_ress = yield fork_specs
        population = [r.value for r in fork_ress]

        benchmark = [
            ("What is 2+2?", "4"),
            ("Capital of France?", "Paris")
        ]

        for gen in range(self.generations):
            print(f"\n--- Gen {gen+1} Loop ---")
            
            # 2. Evaluation (Parallel)
            scores = []
            for tid in population:
                test_tasks = [EvaluateCandidate(tid, q, a) for q, a in benchmark]
                results = yield test_tasks 
                total_score = sum(r.value for r in results)
                scores.append((total_score, tid))
            
            # 3. Selection (Elitism)
            scores.sort(key=lambda x: x[0], reverse=True)
            # Keep top half
            keep_n = max(1, self.population_size // 2)
            top_half = scores[:keep_n]
            print(f"Gen {gen+1} Top Scores: {[s[0] for s in top_half]}")

            if gen == self.generations - 1:
                return top_half[0][1] # Return best thread ID

            # 4. Mutation / Crossover
            new_population = []
            for score, tid in top_half:
                # Refill population
                needed = self.population_size // len(top_half)
                mutations = [
                    MutateCandidate(tid, f"Score {score}")
                    for _ in range(needed)
                ]
                children = yield mutations
                new_population.extend([c.value for c in children])
            
            # Pad if rounding errors
            while len(new_population) < self.population_size:
                 m = yield MutateCandidate(top_half[0][1], "Padding")
                 new_population.append(m.value)
                 
            population = new_population

        return population[0]

async def main():
    store = JobStore("gepa_test.db")
    executor = EggFlowExecutor(store)
    
    algo = GEPA(initial_prompt="You are a helpful assistant.", generations=2, population_size=4)
    res = await executor.run(algo)
    print(f"Winning Thread ID: {res.value}")

if __name__ == "__main__":
    asyncio.run(main())
