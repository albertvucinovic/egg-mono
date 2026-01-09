import asyncio
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread, ForkThread

@dataclass
class IterativeWriting(TaskSpec):
    topic: str

    def run(self):
        # 1. Start a thread to write the draft
        print(f"--- Step 1: Drafting '{self.topic}' ---")
        draft_result = yield CreateThread(
            prompt=f"Write a 1-sentence story about {self.topic}.",
            model_key="gpt-4o-mini"
        )
        draft_text = draft_result.value
        main_thread_id = draft_result.metadata['thread_id']
        print(f"Draft:\n{draft_text}\n")

        # 2. Start a NEW helper thread to act as the critic
        print("--- Step 2: Getting Critique (Independent Thread) ---")
        critique_result = yield CreateThread(
            prompt=f"Critique this story for brevity:\n\n{draft_text}",
            model_key="gpt-4o-mini"
        )
        critique_text = critique_result.value
        print(f"Critique:\n{critique_text}\n")

        # 3. Feed the critique BACK into the MAIN thread
        print("--- Step 3: Refining Original Thread ---")
        final_result = yield ContinueThread(
            thread_id=main_thread_id,
            content=f"Here is a critique of your draft: {critique_text}\n\nPlease rewrite the story."
        )
        
        return final_result.value

async def main():
    store = JobStore("critique_flow.db")
    executor = EggFlowExecutor(store)
    
    workflow = IterativeWriting("a cyberpunk detective")
    
    # Run once
    print(">>> INITIAL RUN")
    res = await executor.run(workflow)
    print(f"\n--- Final Result ---\n{res.value}")

    # Run again (Demonstrates Caching)
    print("\n>>> RERUN (Should be cached)")
    import time
    s = time.time()
    res = await executor.run(workflow)
    print(f"Rerun took: {time.time()-s:.4f}s")

if __name__ == "__main__":
    asyncio.run(main())
