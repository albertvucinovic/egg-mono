# eggflow

`eggflow` is a small async task execution framework with SQLite-backed caching.
It lets you define tasks, compose them with `yield`, run independent subtasks in
parallel, and reuse cached results across process restarts.

It is useful on its own and can also sit beside `eggthreads` for cached agent
workflows.

## Core concepts

- **Task**: a dataclass-like unit of work. Subclass `Task` and implement
  `run()`.
- **TaskStore**: SQLite cache storage.
- **FlowExecutor**: executes tasks, resolves yielded subtasks, and stores
  results.
- **Result**: success/error wrapper with metadata.
- **TaskError**: raised for failed subtasks unless you request wrapped results.

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
    store = TaskStore("cache.db")
    executor = FlowExecutor(store)
    result = await executor.run(Greet("World"))
    print(result)

asyncio.run(main())
```

## Compose tasks with `yield`

Inside `run()`, yield another task to execute it. The yielded result is returned
as the value directly. Workflows that use `yield` are normal generator methods
(`def run`), while simple tasks can use `async def run`:

```python
@dataclass
class Pipeline(Task):
    def run(self):
        data = yield FetchData(url="https://example.com/data.json")
        processed = yield ProcessData(data)
        return yield SaveResult(processed)
```

Yield a list to run tasks in parallel:

```python
@dataclass
class ParallelWork(Task):
    def run(self):
        results = yield [FetchData("a"), FetchData("b"), FetchData("c")]
        return results
```

## Handling failures

By default failed subtasks raise `TaskError`:

```python
from eggflow import TaskError

@dataclass
class SafeFlow(Task):
    def run(self):
        try:
            return yield RiskyTask()
        except TaskError as e:
            return f"Failed: {e.result.error}"
```

Use `wrapped()` to receive the `Result` object instead:

```python
from eggflow import wrapped

@dataclass
class RetryFlow(Task):
    def run(self):
        for attempt in range(3):
            result = yield wrapped(UnreliableTask(attempt=attempt))
            if result.is_success:
                return result.value
        return "all attempts failed"
```

## Cache controls

Skip cache for one execution:

```python
from eggflow import nocache

@dataclass
class FreshFlow(Task):
    def run(self):
        fresh_data = yield nocache(FetchLiveData())
        return fresh_data
```

Make a task permanently uncacheable:

```python
from typing import ClassVar

@dataclass
class AlwaysFresh(Task):
    cacheable: ClassVar[bool] = False

    async def run(self):
        return current_time()
```

## Wrap existing functions

```python
from eggflow import as_task

async def fetch_url(url: str, timeout: int = 30):
    return "..."

@dataclass
class MyFlow(Task):
    def run(self):
        data = yield as_task(fetch_url, "https://example.com", timeout=60)
        keyed = yield as_task(fetch_url, "https://example.com", cache_key=("example",))
        return data, keyed
```

For methods, include relevant instance state in `cache_key` if it affects the
result.

## Calling tasks from helpers

Tasks can execute through the current executor with `.execute()`:

```python
async def helper():
    return await SomeTask("arg").execute()
```

## Optional eggthreads integration

Install with:

```bash
pip install -e "./eggflow[eggthreads]"
```

Use this when a flow needs to launch or coordinate Egg threads while still
benefiting from task caching/crash recovery.

## Tests

```bash
pytest -q eggflow/tests
```
