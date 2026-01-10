import asyncio
from dataclasses import dataclass
from eggflow import Task, CreateThread, ContinueThread

@dataclass
class IterativeWriting(Task):
    topic: str

    def run(self):
        draft_result = yield CreateThread(
            prompt=f"Write a 1-sentence story about {self.topic}.",
            model_key="gpt-4o-mini"
        )
        draft_text = draft_result.value.content
        main_thread_id = draft_result.value.thread_id

        critique_result = yield CreateThread(
            prompt=f"Critique this story for brevity:\n\n{draft_text}",
            model_key="gpt-4o-mini"
        )
        critique_text = critique_result.value.content

        final_result = yield ContinueThread(
            thread_id=main_thread_id,
            content=f"Here is a critique of your draft: {critique_text}\n\nPlease rewrite the story."
        )

        return final_result.value.content

def test_iterative_writing(executor):
    async def run():
        workflow = IterativeWriting("a cyberpunk detective")
        res = await executor.run(workflow)
        assert res.is_success
        assert res.value is not None
    asyncio.run(run())

def test_iterative_writing_caching(executor):
    async def run():
        workflow = IterativeWriting("a space explorer")

        res1 = await executor.run(workflow)
        assert res1.is_success

        res2 = await executor.run(workflow)
        assert res2.is_success
        assert res1.value == res2.value
    asyncio.run(run())
