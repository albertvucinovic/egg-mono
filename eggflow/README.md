# eggflow

A simple task execution framework with automatic caching.

## Core Concepts

- **Task**: A unit of work that can be cached. Define by subclassing `Task` and implementing `run()`.
- **FlowExecutor**: Runs tasks and manages caching via SQLite.
- **yield**: Use `yield` inside `run()` to execute subtasks. Results are returned as values directly.

## Quick Start

```python
from dataclasses import dataclass
from eggflow import Task, TaskStore, FlowExecutor

@dataclass
class Greet(Task):
    name: str

    async def run(self):
        return f"Hello, {self.name}!"

# Run a task
store = TaskStore("cache.db")
executor = FlowExecutor(store)

async def main():
    result = await executor.run(Greet("World"))
    print(result)  # "Hello, World!"
```

## Composing Tasks

Use `yield` to compose tasks. Yielded tasks are cached automatically:

```python
@dataclass
class Pipeline(Task):
    def run(self):
        # Each yield returns the value directly
        data = yield FetchData(url="...")
        processed = yield ProcessData(data)
        return yield SaveResult(processed)
```

## Parallel Execution

Yield a list to run tasks in parallel:

```python
@dataclass
class ParallelWork(Task):
    def run(self):
        results = yield [
            FetchData("url1"),
            FetchData("url2"),
            FetchData("url3"),
        ]
        return results  # List of values
```

## Error Handling

By default, task failures raise `TaskError`:

```python
from eggflow import TaskError

@dataclass
class SafeFlow(Task):
    def run(self):
        try:
            value = yield RiskyTask()
        except TaskError as e:
            return f"Failed: {e.result.error}"
        return value
```

## Getting Result Objects

Use `wrapped()` when you need the `Result` object (for error checking without exceptions, or accessing metadata):

```python
from eggflow import wrapped

@dataclass
class RetryFlow(Task):
    def run(self):
        for i in range(3):
            result = yield wrapped(UnreliableTask(attempt=i))
            if result.is_success:
                return result.value
        return "All attempts failed"
```

## Skipping Cache

Use `nocache()` to skip caching for a specific execution:

```python
from eggflow import nocache

@dataclass
class FreshData(Task):
    def run(self):
        cached_config = yield LoadConfig()
        fresh_data = yield nocache(FetchLiveData())
        return fresh_data
```

## Wrapping Existing Functions

Use `as_task()` to wrap existing functions or methods:

```python
from eggflow import as_task

async def fetch_url(url: str, timeout: int = 30):
    # existing function...
    return data

@dataclass
class MyFlow(Task):
    def run(self):
        # Wrap function call as a cacheable task
        data = yield as_task(fetch_url, "https://api.example.com")

        # Specify which args affect cache key
        data2 = yield as_task(fetch_url, url, timeout=60, cache_key=(url,))

        return data
```

For methods, include relevant instance state in the cache key:

```python
class APIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def query(self, prompt: str):
        return await self._call_api(prompt)

client = APIClient("sk-...")

@dataclass
class QueryFlow(Task):
    def run(self):
        # Include api_key in cache_key so different clients cache separately
        result = yield as_task(
            client.query,
            "Hello",
            cache_key=(client.api_key, "Hello")
        )
        return result
```

## Uncacheable Tasks

Set `cacheable = False` for tasks that should never be cached:

```python
from typing import ClassVar

@dataclass
class AlwaysFresh(Task):
    cacheable: ClassVar[bool] = False

    async def run(self):
        return get_current_time()
```

## Using execute() in Regular Functions

Tasks can be executed from regular async functions using `.execute()`:

```python
async def helper_function():
    # This will use the current executor's cache
    result = await SomeTask("arg").execute()
    return result

@dataclass
class MainFlow(Task):
    async def run(self):
        # Tasks called via execute() inside here are still cached
        return await helper_function()
```

## API Reference

### Core Classes

- `Task` - Base class for tasks. Subclass and implement `run()`.
- `Result` - Contains `value`, `error`, `metadata`, and `is_success` property.
- `TaskStore` - SQLite-based cache storage.
- `FlowExecutor` - Executes tasks with caching.
- `TaskError` - Raised when a task fails (contains `.result`).
- `FuncTask` - Task wrapping a function call.

### Functions

- `as_task(func, *args, cache_key=None, **kwargs)` - Wrap a function/method as a Task.
- `nocache(task)` - Skip caching for this execution.
- `wrapped(task)` - Return `Result` object instead of value.
