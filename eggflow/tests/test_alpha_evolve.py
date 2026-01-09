import asyncio
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread, ForkThread, Result

@dataclass
class ExecuteCode(TaskSpec):
    """Simulates running code extracted from the thread."""
    thread_id: str
    iteration: int # To simulate progress in mock
    
    async def run(self):
        # Mock Logic: Succeed only on iteration 2 (Depth 3)
        if self.iteration >= 2:
             return "Success: Tests Passed."
        return "Error: NameError 'x' is not defined"

@dataclass
class AlphaEvolve(TaskSpec):
    problem_description: str
    beam_width: int = 2
    max_depth: int = 4

    def run(self):
        # 1. Root: Write initial attempt
        print("--- Initial Coding Attempt ---")
        root = yield CreateThread(
            prompt=f"Write a Python script for: {self.problem_description}",
            model_key="gpt-4o"
        )
        
        # Current Beam: List of ThreadIDs
        beam = [root.metadata['thread_id']]

        for depth in range(self.max_depth):
            print(f"\n--- Depth {depth+1} (Beam Size: {len(beam)}) ---")
            
            candidates = []
            
            # 2. Evaluate & Expand each node in beam
            for i, tid in enumerate(beam):
                # A. Execute Code
                exec_result = yield ExecuteCode(tid, depth)
                output = exec_result.value
                
                if "Error" not in output:
                    print(f"Solution Found in Thread {tid}!")
                    return tid # Success!
                
                # B. Expand (Fix bugs)
                # Fork 2 times
                fork_ids = yield [ForkThread(tid) for _ in range(2)]
                
                # Apply fixes in parallel
                fix_specs = [
                    ContinueThread(
                        f.value, 
                        f"Execution failed with: {output}\nFix the code. Strategy {k+1}."
                    )
                    for k, f in enumerate(fork_ids)
                ]
                yield fix_specs # Run the threads
                
                # Add these new threads to candidates list
                candidates.extend([f.value for f in fork_ids])

            # 3. Selection (Pruning)
            # Just slice
            beam = candidates[:self.beam_width]

        return beam[0] 

async def main():
    store = JobStore("alpha_test.db")
    executor = EggFlowExecutor(store)
    
    algo = AlphaEvolve("Calculate Fibonacci sequence", beam_width=2, max_depth=5)
    res = await executor.run(algo)
    print(f"Final Result Thread: {res.value}")

if __name__ == "__main__":
    asyncio.run(main())
