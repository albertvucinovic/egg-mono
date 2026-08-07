# eggflow

`eggflow` is a small asynchronous task-composition framework with a durable
SQLite cache. Tasks can call or yield other tasks, independent tasks can run in
parallel, completed values survive process restarts, and failed or interrupted
work can define recovery behavior.

It has no required dependency on Egg. An optional adapter composes durable
[`eggthreads`](../eggthreads/README.md) interactions, and
[`eggopt`](../eggopt/README.md) uses both layers for restartable agentic
workflows.

## Install

```bash
pip install -e ./eggflow
```

Include the optional Egg thread tasks:

```bash
pip install -e "./eggflow[eggthreads]"
```

Python 3.10+ is required.

## Core model

- `Task`: unit of work, cached by a content-derived key by default.
- `TaskStore`: SQLite storage for running, completed, and failed task records.
- `FlowExecutor`: executes tasks and resolves their compositions.
- `TaskError`: exposes a failed subtask's `Result`.
- `wrapped(...)`: return a `Result` instead of raising a normal task failure.
- `nocache(...)`: bypass reuse for one execution.
- `keyed(...)`, `rekeyed(...)`, and `keyed_scope(...)`: make cache identity
  explicit when surrounding context affects a task.
- `Task.recover()`: repair or refuse a retry after failed/interrupted work.
- `Task.restore(value)`: verify or rematerialize external state on cache reuse.

## Quick start

```python
import asyncio
from dataclasses import dataclass
from eggflow import FlowExecutor, Task, TaskStore

@dataclass
class Greet(Task):
    name: str

    async def run(self):
        return f"Hello, {self.name}!"

async def main():
    store = TaskStore("flow.sqlite")
    executor = FlowExecutor(store)
    print(await executor.run(Greet("World")))

asyncio.run(main())
```

The default cache key hashes the task class and dataclass/instance attributes.
Override `get_cache_key()` when identity must be narrower or more explicit.
Task values must be pickle-safe to persist.

## Composition

A generator-style `run()` yields subtasks sequentially:

```python
from dataclasses import dataclass
from eggflow import Task

@dataclass
class Pipeline(Task):
    source: str

    def run(self):
        raw = yield Fetch(self.source)
        clean = yield Normalize(raw)
        return (yield Save(clean))
```

Yield a list to run independent work concurrently:

```python
from dataclasses import dataclass
from eggflow import Task

@dataclass
class FanOut(Task):
    items: tuple[str, ...]

    def run(self):
        return (yield [Fetch(item) for item in self.items])
```

Tasks may also call `await SomeTask(...).execute()` while an executor context is
active. Use one composition style per boundary so task ownership stays clear.

## Failure, retry, and cache controls

```python
from dataclasses import dataclass
from eggflow import Task, nocache, wrapped

@dataclass
class Robust(Task):
    def run(self):
        result = yield wrapped(RemoteCall())
        if result.is_success:
            return result.value
        return (yield nocache(Fallback()))

    async def recover(self) -> bool:
        await repair_partial_state()
        return True
```

Here `RemoteCall`, `Fallback`, and `repair_partial_state` are domain-defined
operations.

Terminal failures, including context-limit failures from the optional
Eggthreads adapter, are not blindly retried. For values that reference files or
other materializations, override `restore(...)` to validate cached reuse and
raise a clear error when repair is impossible.

## Function tasks

`as_task()` wraps a callable as a task:

```python
from eggflow import as_task

value = await executor.run(as_task(parse_document, "input.md", strict=True))
```

`executor` is an active `FlowExecutor`; `parse_document` is an ordinary callable.

Include all result-affecting state in the callable arguments or an explicit
cache key.

## Eggthreads integration

The optional adapter exports `CreateThread`, `ForkThread`, `ContinueThread`,
`PICTask`, and related result/configuration types. It lets a flow cache and
recover agent interactions while Eggthreads remains responsible for thread and
runner semantics. New agentic compositions should also consider Eggopt's
higher-level `run_operation`, `ActorCritic`, and `ThreadTool` boundaries.

## Development

```bash
pip install -e "./eggflow[dev,eggthreads]"
pytest -q eggflow/tests
```

Additional reference: [`eggflow/API.md`](API.md).
