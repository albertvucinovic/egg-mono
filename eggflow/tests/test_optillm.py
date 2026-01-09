import asyncio
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread, ForkThread

@dataclass
class BestOfN(TaskSpec):
    prompt: str
    n: int = 5
    grader_model: str = "gpt-4o-mini"
    
    def run(self):
        # 1. Generate N samples in parallel
        # We use 'seed' to differentiate them if the backend supports it, 
        # or rely on temperature if eggthreads is configured that way.
        print(f"--- Sampling {self.n} solutions ---")
        tasks = [
            CreateThread(
                prompt=self.prompt, 
                seed=i,
                model_key="gpt-4o"
            ) for i in range(self.n)
        ]
        
        # Yielding a list runs them in parallel
        results = yield tasks
        
        # 2. Prepare for Grading
        candidates = []
        for i, res in enumerate(results):
            val = res.value
            # For the test, we'll strip newlines or truncate for display
            display_val = (val[:50] + '...') if len(val) > 50 else val
            candidates.append(f"Option {i}:\n{val}\n")
            print(f"  > Sample {i}: {display_val}")

        # 3. Grade/Select
        # We start a fresh thread to act as the judge
        judge_prompt = (
            f"Compare the following {self.n} responses to the prompt: '{self.prompt}'\n\n"
            + "---\n".join(candidates) +
            "\n---\nSelect the best option. Return ONLY the index (e.g. 'Option 1')."
        )
        
        print("\n--- Grading ---")
        selection_res = yield CreateThread(
            prompt=judge_prompt,
            model_key=self.grader_model
        )
        
        selection = selection_res.value
        print(f"Judge Selected: {selection}")
        
        # Parse selection (Naively)
        best_idx = 0
        for i in range(self.n):
            if f"Option {i}" in selection:
                best_idx = i
                break
                
        return results[best_idx].value

async def main():
    store = JobStore("optillm_test.db")
    executor = EggFlowExecutor(store)
    
    task = BestOfN(prompt="Explain the theory of relativity in one sentence.", n=3)
    res = await executor.run(task)
    print(f"\nFinal Selected Answer: {res.value}")

if __name__ == "__main__":
    asyncio.run(main())
