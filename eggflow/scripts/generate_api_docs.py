#!/usr/bin/env python3
"""Generate API.md documentation from eggflow source code.

This script extracts function signatures and docstrings from the eggflow
package and generates a Markdown API reference with comprehensive examples.

Usage:
    python scripts/generate_api_docs.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

# Add parent directory to path so we can import eggflow
sys.path.insert(0, str(Path(__file__).parent.parent))

import eggflow
from eggflow import core, eggthreads_tasks

# API categories organized by conceptual grouping
CATEGORIES: Dict[str, List[Tuple[str, Any]]] = {
    "Task Definition": [
        ("Task", core.Task),
        ("FuncTask", core.FuncTask),
        ("as_task", core.as_task),
    ],
    "Execution": [
        ("FlowExecutor", core.FlowExecutor),
        ("TaskStore", core.TaskStore),
    ],
    "Results & Errors": [
        ("Result", core.Result),
        ("TaskError", core.TaskError),
    ],
    "Caching Control": [
        ("NoCache", core.NoCache),
        ("nocache", core.nocache),
        ("Wrapped", core.Wrapped),
        ("wrapped", core.wrapped),
    ],
    "Thread Integration": [
        ("CreateThread", eggthreads_tasks.CreateThread),
        ("ContinueThread", eggthreads_tasks.ContinueThread),
        ("ForkThread", eggthreads_tasks.ForkThread),
        ("ThreadResult", eggthreads_tasks.ThreadResult),
        ("Config", eggthreads_tasks.Config),
    ],
}

# Static content sections
INTRO = '''
# eggflow API Reference

eggflow is a task execution framework with automatic caching and composition.
It enables building complex workflows from simple, cacheable tasks.

'''

QUICK_START = '''
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

'''

COMPOSING_TASKS = '''
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

'''

ERROR_HANDLING = '''
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

'''

CACHING = '''
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

'''

FUNCTION_WRAPPING = '''
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

'''

THREAD_INTEGRATION = '''
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

'''

ADVANCED_PATTERNS = '''
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

'''


def get_signature(obj: Any) -> Optional[str]:
    """Extract function/class signature as a string."""
    try:
        if inspect.isclass(obj):
            # For dataclasses, show fields
            if hasattr(obj, "__dataclass_fields__"):
                fields = []
                for name, field_info in obj.__dataclass_fields__.items():
                    field_type = field_info.type
                    if hasattr(field_type, "__name__"):
                        type_str = field_type.__name__
                    else:
                        type_str = str(field_type).replace("typing.", "")
                    fields.append(f"{name}: {type_str}")
                return f"{obj.__name__}({', '.join(fields)})"
            # For regular classes, get __init__ signature
            sig = inspect.signature(obj.__init__)
            params = list(sig.parameters.values())[1:]  # Skip 'self'
            new_sig = sig.replace(parameters=params)
            return f"{obj.__name__}{new_sig}"
        elif callable(obj):
            sig = inspect.signature(obj)
            return f"{obj.__name__}{sig}"
    except (ValueError, TypeError):
        pass
    return None


def get_docstring(obj: Any) -> Optional[str]:
    """Extract docstring from object."""
    doc = inspect.getdoc(obj)
    if doc:
        return doc
    return None


def format_item(name: str, obj: Any) -> str:
    """Format a single function/class for Markdown output."""
    lines = []

    sig = get_signature(obj)
    if sig:
        lines.append(f"### `{sig}`")
    else:
        lines.append(f"### `{name}`")

    lines.append("")

    doc = get_docstring(obj)
    if doc:
        lines.append(doc)
    else:
        lines.append("*No documentation available.*")

    lines.append("")
    return "\n".join(lines)


def generate_api_reference() -> str:
    """Generate the complete API reference content."""
    lines = []

    # Header and intro
    lines.append(INTRO.strip())
    lines.append("")

    # Quick Start
    lines.append(QUICK_START.strip())
    lines.append("")

    # Composing Tasks
    lines.append(COMPOSING_TASKS.strip())
    lines.append("")

    # Error Handling
    lines.append(ERROR_HANDLING.strip())
    lines.append("")

    # Caching
    lines.append(CACHING.strip())
    lines.append("")

    # Function Wrapping
    lines.append(FUNCTION_WRAPPING.strip())
    lines.append("")

    # Thread Integration
    lines.append(THREAD_INTEGRATION.strip())
    lines.append("")

    # Advanced Patterns
    lines.append(ADVANCED_PATTERNS.strip())
    lines.append("")

    # Table of Contents for API Reference
    lines.append("---")
    lines.append("")
    lines.append("## API Reference")
    lines.append("")
    lines.append("### Table of Contents")
    lines.append("")
    for category in CATEGORIES:
        anchor = category.lower().replace(" ", "-").replace("&", "").replace("(", "").replace(")", "")
        lines.append(f"- [{category}](#{anchor})")
    lines.append("")

    # API Reference by category
    lines.append("---")
    lines.append("")

    for category, items in CATEGORIES.items():
        lines.append(f"### {category}")
        lines.append("")

        for name, obj in items:
            lines.append(format_item(name, obj))

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main():
    """Generate API.md and write to file."""
    output_path = Path(__file__).parent.parent / "API.md"

    content = generate_api_reference()

    output_path.write_text(content, encoding="utf-8")
    print(f"Generated {output_path}")

    # Print statistics
    total_items = sum(len(items) for items in CATEGORIES.values())
    documented = 0
    undocumented = []

    for category, items in CATEGORIES.items():
        for name, obj in items:
            doc = get_docstring(obj)
            if doc:
                documented += 1
            else:
                undocumented.append(f"{category}: {name}")

    print(f"\nTotal API items: {total_items}")
    print(f"Documented: {documented}")
    print(f"Missing docstrings: {len(undocumented)}")

    if undocumented:
        print("\nUndocumented items:")
        for item in undocumented:
            print(f"  - {item}")


if __name__ == "__main__":
    main()
