import asyncio
import hashlib
from dataclasses import dataclass
from eggflow import EggFlowExecutor, JobStore, TaskSpec, CreateThread, ContinueThread

@dataclass
class AnalyzeLocalFile(TaskSpec):
    """
    Analyzes a local file.
    Note: We include file_hash in the spec to ensure cache invalidation 
    if the file content changes.
    """
    file_path: str
    file_hash: str # computed by caller

    def run(self):
        # 1. Read the file content
        try:
            with open(self.file_path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return "Error: File not found."

        # 2. Start thread to analyze it
        # We also ask the thread to write a summary to 'summary.txt'
        prompt = (
            f"Analyze this data:\n{content[:100]}...\n"
            "Write a summary to 'summary.txt' and return 'Done'."
        )
        
        res = yield CreateThread(
            prompt=prompt,
            model_key="gpt-4o",
            output_files=["summary.txt"] # <--- Request Extraction
        )
        
        # 3. Return the extracted artifact directly
        artifacts = res.metadata.get('artifacts', {})
        return artifacts.get("summary.txt", "No summary generated.")

def compute_hash(path):
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except:
        return "0"

async def main():
    store = JobStore("files_test.db")
    executor = EggFlowExecutor(store)

    # Setup dummy file
    with open("data.log", "w") as f:
        f.write("System OK. CPU 10%. Memory 20%.")

    print("--- Run 1: Initial File ---")
    task1 = AnalyzeLocalFile("data.log", compute_hash("data.log"))
    res1 = await executor.run(task1)
    print(f"Result 1: {res1.value}")

    print("\n--- Run 2: Same File (Should be Cached) ---")
    # Even if we change the file on disk momentarily, if we pass the SAME hash 
    # (simulating a 'clean' run where we claim it hasn't changed), we get cached result.
    # But usually we recompute hash.
    # Let's recompute hash (it hasn't changed).
    res2 = await executor.run(AnalyzeLocalFile("data.log", compute_hash("data.log")))
    print(f"Result 2 (Cached): {res2.value}")

    print("\n--- Run 3: File Changed (Should Rerun) ---")
    with open("data.log", "w") as f:
        f.write("System FAILURE. CPU 99%.")
    
    # Hash changes -> New Cache Key -> New Run
    res3 = await executor.run(AnalyzeLocalFile("data.log", compute_hash("data.log")))
    print(f"Result 3: {res3.value}")

if __name__ == "__main__":
    asyncio.run(main())
