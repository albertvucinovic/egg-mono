import asyncio
import hashlib
from dataclasses import dataclass
from eggflow import Task, CreateThread

@dataclass
class AnalyzeLocalFile(Task):
    """
    Analyzes a local file.
    Note: We include file_hash in the spec to ensure cache invalidation
    if the file content changes.
    """
    file_path: str
    file_hash: str

    def run(self):
        try:
            with open(self.file_path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            return "Error: File not found."

        prompt = (
            f"Analyze this data:\n{content[:100]}...\n"
            "Write a summary to 'summary.txt' and return 'Done'."
        )

        res = yield CreateThread(
            prompt=prompt,
            model_key="gpt-4o",
            output_files=["summary.txt"]
        )

        artifacts = res.metadata.get('artifacts', {})
        return artifacts.get("summary.txt", "No summary generated.")

def compute_hash(path):
    try:
        return hashlib.md5(open(path, "rb").read()).hexdigest()
    except:
        return "0"

def test_file_analysis(executor, tmp_path):
    async def run():
        data_file = tmp_path / "data.log"
        data_file.write_text("System OK. CPU 10%. Memory 20%.")

        task1 = AnalyzeLocalFile(str(data_file), compute_hash(str(data_file)))
        res1 = await executor.run(task1)
        assert res1.is_success
    asyncio.run(run())

def test_file_caching(executor, tmp_path):
    async def run():
        data_file = tmp_path / "data.log"
        data_file.write_text("System OK. CPU 10%. Memory 20%.")

        file_hash = compute_hash(str(data_file))
        task1 = AnalyzeLocalFile(str(data_file), file_hash)
        res1 = await executor.run(task1)

        task2 = AnalyzeLocalFile(str(data_file), file_hash)
        res2 = await executor.run(task2)

        assert res1.value == res2.value
    asyncio.run(run())

def test_file_change_invalidates_cache(executor, tmp_path):
    async def run():
        data_file = tmp_path / "data.log"
        data_file.write_text("System OK. CPU 10%. Memory 20%.")

        task1 = AnalyzeLocalFile(str(data_file), compute_hash(str(data_file)))
        res1 = await executor.run(task1)

        data_file.write_text("System FAILURE. CPU 99%.")

        task2 = AnalyzeLocalFile(str(data_file), compute_hash(str(data_file)))
        res2 = await executor.run(task2)

        assert task1.get_cache_key() != task2.get_cache_key()
    asyncio.run(run())
