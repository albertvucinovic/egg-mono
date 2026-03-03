# eggflow API Reference

eggflow is a task execution framework with automatic caching and composition.
It enables building complex workflows from simple, cacheable tasks.

## Quick Start

```python
import asyncio
from eggflow import Task, TaskStore, FlowExecutor, Result

# Define a simple task
@dataclass
class Greet(Task):
    name: str

    async def run(self):
        return f"Hello, {self.name}!"

# Execute with caching
async def main():
    store = TaskStore("cache.db")
    executor = FlowExecutor(store)

    # First call executes and caches
    result = await executor.run(Greet(name="World"))
    print(result)  # "Hello, World!"

    # Second call returns cached result
    result = await executor.run(Greet(name="World"))
    print(result)  # "Hello, World!" (from cache)

asyncio.run(main())
```

## Composing Tasks

Tasks can yield other tasks for sequential or parallel composition.

### Sequential Composition

```python
@dataclass
class FetchAndProcess(Task):
    url: str

    def run(self):
        # Yield subtasks sequentially - each returns its value
        raw_data = yield FetchData(url=self.url)
        parsed = yield ParseData(data=raw_data)
        result = yield TransformData(data=parsed)
        return result
```

### Parallel Composition

```python
@dataclass
class FetchMultiple(Task):
    urls: List[str]

    def run(self):
        # Yield a list for parallel execution
        results = yield [FetchData(url=u) for u in self.urls]
        return results  # List of results in same order
```

### Mixed Composition

```python
@dataclass
class ComplexWorkflow(Task):
    items: List[str]

    def run(self):
        # Parallel fetch
        raw_results = yield [FetchItem(item=i) for i in self.items]

        # Sequential processing of parallel results
        processed = []
        for raw in raw_results:
            p = yield ProcessItem(data=raw)
            processed.append(p)

        # Final aggregation
        return yield AggregateResults(items=processed)
```

## Error Handling

### Default Behavior (Exceptions)

By default, failed tasks raise `TaskError`:

```python
@dataclass
class MayFail(Task):
    should_fail: bool

    async def run(self):
        if self.should_fail:
            raise ValueError("Something went wrong!")
        return "success"

# This raises TaskError if task fails
try:
    result = await executor.run(MayFail(should_fail=True))
except TaskError as e:
    print(f"Task failed: {e}")
    print(f"Error details: {e.result.error}")
```

### Using wrapped() for Error Handling

Use `wrapped()` to get a `Result` object instead of raising:

```python
@dataclass
class SafeWorkflow(Task):
    def run(self):
        # Get Result object (won't raise)
        result = yield wrapped(MayFail(should_fail=True))

        if result.is_success:
            return result.value
        else:
            # Handle error gracefully
            print(f"Task failed: {result.error}")
            return "fallback value"
```

### Retry Pattern

```python
@dataclass
class RetryingTask(Task):
    max_attempts: int = 3

    def run(self):
        for attempt in range(self.max_attempts):
            result = yield wrapped(UnreliableTask())
            if result.is_success:
                return result.value
            print(f"Attempt {attempt + 1} failed: {result.error}")

        raise RuntimeError(f"All {self.max_attempts} attempts failed")
```

## Caching

### Automatic Caching

Tasks are cached by default based on their class name and attributes:

```python
@dataclass
class ExpensiveComputation(Task):
    x: int
    y: int

    async def run(self):
        # This only runs once per (x, y) combination
        await asyncio.sleep(5)  # Simulate expensive work
        return self.x * self.y

# First call: executes (5 seconds)
result = await executor.run(ExpensiveComputation(x=3, y=4))

# Second call: returns cached result (instant)
result = await executor.run(ExpensiveComputation(x=3, y=4))
```

### Skipping Cache with nocache()

```python
@dataclass
class GetCurrentTime(Task):
    async def run(self):
        return datetime.now().isoformat()

def run(self):
    # Always execute fresh, never use cache
    current_time = yield nocache(GetCurrentTime())
    return current_time
```

### Uncacheable Tasks

Mark a task class as permanently uncacheable:

```python
@dataclass
class AlwaysFresh(Task):
    cacheable: ClassVar[bool] = False  # Never cached

    async def run(self):
        return random.random()
```

### Custom Cache Keys

Override `get_cache_key()` for custom caching logic:

```python
@dataclass
class CustomCached(Task):
    url: str
    timeout: int  # Don't include in cache key

    def get_cache_key(self) -> str:
        # Only cache by URL, ignore timeout
        import hashlib
        return hashlib.sha256(f"CustomCached:{self.url}".encode()).hexdigest()
```

## Wrapping Functions with as_task()

Convert existing functions to cacheable tasks at the call site:

### Basic Usage

```python
def fetch_data(url: str) -> str:
    response = requests.get(url)
    return response.text

@dataclass
class MyWorkflow(Task):
    def run(self):
        # Wrap function call as task
        data = yield as_task(fetch_data, "https://api.example.com/data")
        return data
```

### Custom Cache Keys

```python
class APIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def query(self, prompt: str, temperature: float = 0.7) -> str:
        # Make API call...
        return result

@dataclass
class UseAPI(Task):
    client: APIClient
    prompt: str

    def run(self):
        # Include relevant state in cache_key
        result = yield as_task(
            self.client.query,
            self.prompt,
            temperature=0.5,
            cache_key=(self.prompt, 0.5)  # Exclude api_key from cache
        )
        return result
```

### Async Functions

```python
async def async_fetch(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

@dataclass
class AsyncWorkflow(Task):
    def run(self):
        # Works with async functions too
        data = yield as_task(async_fetch, "https://api.example.com/data")
        return data
```

## eggthreads Integration

eggflow provides tasks for working with eggthreads conversation threads.

### Creating a Thread

```python
from eggflow import CreateThread, ThreadResult

@dataclass
class GenerateCode(Task):
    description: str

    def run(self):
        result: ThreadResult = yield CreateThread(
            prompt=f"Write Python code to: {self.description}",
            model_key="claude-3",
            output_files=["solution.py"]
        )

        return {
            "code": result.artifacts.get("solution.py", ""),
            "explanation": result.content
        }
```

### Continuing a Thread

```python
@dataclass
class RefineCode(Task):
    thread_id: str
    feedback: str

    def run(self):
        result = yield ContinueThread(
            thread_id=self.thread_id,
            content=f"Please improve the code based on this feedback: {self.feedback}",
            output_files=["solution.py"]
        )
        return result.artifacts.get("solution.py", "")
```

### Forking for Parallel Exploration

```python
@dataclass
class ExploreAlternatives(Task):
    thread_id: str
    alternatives: List[str]

    def run(self):
        # Fork the thread for each alternative
        fork_ids = yield [
            ForkThread(source_thread_id=self.thread_id)
            for _ in self.alternatives
        ]

        # Continue each fork with different prompts
        results = yield [
            ContinueThread(thread_id=fid, content=alt)
            for fid, alt in zip(fork_ids, self.alternatives)
        ]

        return results
```

### Mock Mode for Testing

```python
import os

# Enable mock mode (default)
os.environ["EGGFLOW_REAL_LLM"] = "false"

# Or disable for real LLM calls
os.environ["EGGFLOW_REAL_LLM"] = "true"
os.environ["EGG_DB_PATH"] = "/path/to/eggthreads.db"
```

## Advanced Patterns

### Tree of Thoughts

```python
@dataclass
class TreeOfThoughts(Task):
    problem: str
    num_branches: int = 3
    depth: int = 2

    def run(self):
        # Generate initial thoughts
        thoughts = yield [
            GenerateThought(problem=self.problem, seed=i)
            for i in range(self.num_branches)
        ]

        # Evaluate and select best
        scores = yield [EvaluateThought(thought=t) for t in thoughts]
        best_idx = scores.index(max(scores))

        if self.depth > 1:
            # Recurse on best thought
            return yield TreeOfThoughts(
                problem=thoughts[best_idx],
                num_branches=self.num_branches,
                depth=self.depth - 1
            )

        return thoughts[best_idx]
```

### Iterative Refinement

```python
@dataclass
class IterativeRefinement(Task):
    initial: str
    max_iterations: int = 5
    threshold: float = 0.9

    def run(self):
        current = self.initial

        for i in range(self.max_iterations):
            # Evaluate current solution
            score = yield Evaluate(solution=current)
            if score >= self.threshold:
                return current

            # Get critique and improve
            critique = yield Critique(solution=current)
            current = yield Improve(solution=current, critique=critique)

        return current
```

### N-Best Sampling

```python
@dataclass
class NBestSampling(Task):
    prompt: str
    n: int = 5

    def run(self):
        # Generate N candidates in parallel
        candidates = yield [
            Generate(prompt=self.prompt, seed=i)
            for i in range(self.n)
        ]

        # Score all candidates
        scores = yield [Score(candidate=c) for c in candidates]

        # Return best
        best_idx = scores.index(max(scores))
        return candidates[best_idx]
```

---

## API Reference

### Table of Contents

- [Task Definition](#task-definition)
- [Execution](#execution)
- [Results & Errors](#results--errors)
- [Caching Control](#caching-control)
- [Thread Integration](#thread-integration)

---

### Task Definition

### `Task(cacheable: ClassVar)`

Base class for cacheable tasks.

Subclass and implement run() to define task behavior.
Tasks are cached by default based on their attributes.

### `FuncTask(cacheable: ClassVar)`

Task that wraps a function or method call with configurable cache key.

Created via as_task() to convert existing functions/methods into cacheable tasks.

### `as_task(func_or_method: Callable, *args, cache_key: Optional[Tuple[Any, ...]] = None, **kwargs) -> eggflow.core.Task`

Wrap a function or method call as a Task.

This allows converting existing functions/methods to cacheable Tasks at the call site.

Usage:
    # Wrap a method - include relevant state in cache_key:
    value = yield as_task(service.generate, "hello", cache_key=(service.model, "hello"))

    # Wrap a function - specify which args affect caching:
    value = yield as_task(fetch_data, url, timeout, cache_key=(url,))

    # Default: all args used for cache key
    value = yield as_task(compute, x, y)

Args:
    func_or_method: A function or bound method
    *args: Arguments to pass
    cache_key: Explicit tuple of values for cache key. If not specified, all args are used.
    **kwargs: Keyword arguments to pass

Returns:
    A FuncTask that can be yielded or executed.

---

### Execution

### `FlowExecutor(store: eggflow.core.TaskStore)`

Executes tasks with automatic caching and composition support.

The executor handles:
- Running tasks and caching results by cache key
- Generator-based task composition (yield subtasks)
- Parallel execution of task lists
- Error propagation and wrapped result handling

### `TaskStore(db_path: str = 'flow.db')`

SQLite-based storage for task results and caching.

Stores task results by cache key with status tracking (PENDING, RUNNING,
COMPLETED, FAILED). Results are serialized using pickle.

---

### Results & Errors

### `Result(value: Any, metadata: Dict, error: Optional)`

Result of a task execution, containing value or error.

Attributes:
    value: The return value if task succeeded, None otherwise.
    metadata: Additional data like artifacts, timing info, etc.
    error: Error message if task failed, None if successful.

### `TaskError(message: str, result: 'Result')`

Raised when a task fails and values are being returned (not wrapped).

---

### Caching Control

### `NoCache(task: eggflow.core.Task)`

Wrapper to skip caching for a specific task execution.

### `nocache(task: eggflow.core.Task) -> eggflow.core.NoCache`

Wrap a task to skip caching for this execution.

Usage:
    value = yield nocache(MyTask("foo"))

### `Wrapped(task: eggflow.core.Task)`

Wrapper to get Result object instead of unwrapped value.

By default, yielding a task returns the value directly and raises TaskError on failure.
Use wrapped() when you need access to the Result object (for error checking, metadata, etc).

### `wrapped(task: eggflow.core.Task) -> eggflow.core.Wrapped`

Wrap a task to return Result object instead of unwrapped value.

By default, yielding a task returns the value directly and raises TaskError on failure.
Use this when you need access to the Result object.

Usage:
    # Get Result object (won't raise on error)
    result = yield wrapped(MyTask("foo"))
    if result.is_success:
        print(result.value)
    else:
        print(result.error)

    # Can combine with nocache
    result = yield wrapped(nocache(MyTask("foo")))

---

### Thread Integration

### `CreateThread(cacheable: ClassVar, prompt: str, model_key: Optional, system_prompt: Optional, seed: int, output_files: List)`

Create a new eggthreads thread and run it to completion.

This task creates a root thread, adds a user message, runs the scheduler
until idle, and returns the assistant's response.

Attributes:
    prompt: The user message to send.
    model_key: Optional model key for the thread (e.g., "gpt-4", "claude-3").
    system_prompt: Optional system prompt to set.
    seed: Random seed for reproducibility in cache key.
    output_files: List of filenames to extract from working directory.

### `ContinueThread(cacheable: ClassVar, thread_id: str, content: str, role: str, output_files: List)`

Continue an existing thread with a new message.

Appends a message to an existing thread (if not already present),
runs the scheduler until idle, and returns the response.

Attributes:
    thread_id: The thread to continue.
    content: Message content to append.
    role: Message role (default "user").
    output_files: List of filenames to extract from working directory.

### `ForkThread(cacheable: ClassVar, source_thread_id: str)`

Fork (duplicate) an existing thread to create a branch.

Creates a copy of the source thread's conversation history,
allowing independent continuation from that point.

Attributes:
    source_thread_id: The thread to duplicate.

### `ThreadResult(thread_id: str, content: str, artifacts: Dict)`

Result from a thread task execution.

Attributes:
    thread_id: The eggthreads thread ID.
    content: The assistant's final response content.
    artifacts: Dict mapping filenames to file contents extracted from working dir.

### `Config(*args, **kwargs)`

Configuration for eggthreads integration.

Attributes:
    EGG_DB_PATH: Path to the eggthreads SQLite database.
    MOCK_MODE: If True, return mock responses without calling LLM.

---
