import asyncio
from dataclasses import dataclass
from eggflow import Task, CreateThread, ContinueThread

@dataclass
class IterativeWriting(Task):
    topic: str

    def run(self):
        # yield now returns value directly (ThreadResult)
        draft_result = yield CreateThread(
            prompt=f"Write a 1-sentence story about {self.topic}.",
            model_key="gpt-4o-mini"
        )
        draft_text = draft_result.content
        main_thread_id = draft_result.thread_id

        critique_result = yield CreateThread(
            prompt=f"Critique this story for brevity:\n\n{draft_text}",
            model_key="gpt-4o-mini"
        )
        critique_text = critique_result.content

        final_result = yield ContinueThread(
            thread_id=main_thread_id,
            content=f"Here is a critique of your draft: {critique_text}\n\nPlease rewrite the story."
        )

        return final_result.content

def test_iterative_writing(executor):
    async def run():
        workflow = IterativeWriting("a cyberpunk detective")
        # executor.run now returns value directly
        value = await executor.run(workflow)
        assert value is not None
    asyncio.run(run())

def test_iterative_writing_caching(executor):
    async def run():
        workflow = IterativeWriting("a space explorer")

        value1 = await executor.run(workflow)
        value2 = await executor.run(workflow)
        assert value1 == value2
    asyncio.run(run())
