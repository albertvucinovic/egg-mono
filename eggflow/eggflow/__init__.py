"""eggflow - Task execution framework with caching and composition.

eggflow provides a simple but powerful way to build workflows from
cacheable tasks. Tasks can yield other tasks for composition, and
results are automatically cached to avoid redundant computation.

Key features:

- **Automatic caching**: Task results cached by content-based keys
- **Composition**: Tasks yield subtasks for sequential/parallel execution
- **Error handling**: Use ``wrapped()`` for graceful error handling
- **Function wrapping**: Convert existing functions to tasks with ``as_task()``
- **eggthreads integration**: Run LLM conversations as cached tasks

Quick start::

    from dataclasses import dataclass
    from eggflow import Task, TaskStore, FlowExecutor

    @dataclass
    class Greet(Task):
        name: str

        async def run(self):
            return f"Hello, {self.name}!"

    store = TaskStore("cache.db")
    executor = FlowExecutor(store)
    result = await executor.run(Greet(name="World"))

See API.md for comprehensive documentation.
"""
from .core import (
    Task, Result, TaskStore, FlowExecutor,
    NoCache, nocache,
    Wrapped, wrapped,
    Keyed, keyed,
    Rekeyed, rekeyed,
    keyed_scope,
    TaskError,
    FuncTask, as_task,
)
from .eggthreads_tasks import (
    CreateThread, ContinueThread, ForkThread, Config, ThreadResult,
    PICTask, PICRecoveryError, ContextLimitExceededError,
)
