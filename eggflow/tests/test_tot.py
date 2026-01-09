import asyncio
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread, ForkThread

@dataclass
class TreeOfThoughts(TaskSpec):
    problem: str
    depth: int = 3
    branch_factor: int = 3

    def run(self):
        # 1. Root
        print(f"--- ToT Root: {self.problem} ---")
        root = yield CreateThread(
            prompt=f"Let's solve this step by step. Problem: {self.problem}",
            model_key="gpt-4o"
        )
        current_best_id = root.metadata['thread_id']
        
        for step in range(self.depth):
            print(f"\n--- Step {step+1}/{self.depth} ---")
            
            # 2. Branch (Fork)
            # Create N copies of the current state
            fork_specs = [ForkThread(current_best_id) for _ in range(self.branch_factor)]
            fork_results = yield fork_specs
            fork_ids = [r.value for r in fork_results]
            
            # 3. Expand (Generate Next Thought)
            # Ask each fork to generate the next step
            expand_specs = [
                ContinueThread(tid, "Generate the next logical step. Be concise.")
                for tid in fork_ids
            ]
            expand_results = yield expand_specs
            
            # 4. Evaluate
            # We fork each expanded thread one more time to ask a self-evaluation question?
            # Or just ask a separate grader thread to evaluate the text.
            # Let's use a separate grader thread to keep it clean.
            scores = []
            for i, res in enumerate(expand_results):
                content = res.value
                tid = fork_ids[i]
                
                # Mock grading logic: longer is better? Or check for keywords?
                # In real life: CreateThread("Rate this...")
                grader_res = yield CreateThread(
                    prompt=f"Rate the following step for the problem '{self.problem}':\n{content}\nReturn a number 0-10.",
                    model_key="gpt-4o-mini"
                )
                
                # Mock parsing score
                try:
                    # Just grab first digit found
                    score = int(''.join(filter(str.isdigit, grader_res.value))) % 11
                except:
                    score = 0
                
                # Mock override for testing if not using real LLM
                if "Mock" in content:
                    # Deterministic pseudo-random score based on thread ID
                    import hashlib
                    score = int(hashlib.md5(tid.encode()).hexdigest(), 16) % 10
                
                print(f"  Branch {i} (Score {score}): {content[:30]}...")
                scores.append((score, tid))
            
            # 5. Prune
            scores.sort(key=lambda x: x[0], reverse=True)
            best_score, best_tid = scores[0]
            print(f"  > Winner: Branch with score {best_score}")
            
            current_best_id = best_tid
            
        # Final Result
        final = yield ContinueThread(current_best_id, "Summarize the final solution.")
        return final.value

async def main():
    store = JobStore("tot_test.db")
    executor = EggFlowExecutor(store)
    
    tot = TreeOfThoughts("How do I build a dyson sphere?")
    res = await executor.run(tot)
    print(f"\nFinal Solution:\n{res.value}")

if __name__ == "__main__":
    asyncio.run(main())
