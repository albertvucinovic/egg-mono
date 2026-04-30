# RLM Skill: Persistent REPL + Recursive Subthreads

> Use persistent REPL variables for large context/tool outputs, then chunk,
> delegate to subthreads when useful, and synthesize compact findings.

Use this skill when the task involves long context, large tool output, expensive
intermediate data, or a need to repeatedly inspect/transform data over multiple
turns.

## Core idea

Treat large input/output as data in a persistent Python REPL, not as text to keep
copying through chat. Put it in variables, inspect it with code, and only bring
small previews or final findings back into the model context.

```python
from eggtools import bash
log = bash("git ls-files")
len(log), log.splitlines()[:20]
```

Then search/slice/aggregate the variable:

```python
lines = log.splitlines()
py_files = [x for x in lines if x.endswith('.py')]
py_files[:50]
```

## Optional bootstrap helpers

There is no special RLM runtime module. The stable runtime API is the small
`eggtools` bridge (`bash`, `python`, `spawn_agent`, `spawn_agent_auto`, `wait`,
`web_search`, `fetch_url`, ...). When these helpers are useful, paste/adapt this
snippet into `python_repl`; because the REPL is persistent, you normally only
need to define it once per session.

```python
from typing import Any, Iterable, Optional, Sequence
from eggtools import spawn_agent, spawn_agent_auto, wait


def preview(obj: Any, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Return a bounded string preview of any object."""
    text = obj if isinstance(obj, str) else repr(obj)
    lines = text.splitlines()
    clipped = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        clipped = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars]
        clipped = True
    if clipped:
        out += "\n... [preview truncated]"
    return out


def chunk_text(text: str, *, n: Optional[int] = None, max_chars: Optional[int] = None, by: str = "lines") -> list[str]:
    """Split text into chunks for subthread processing."""
    text = str(text or "")
    if not text:
        return []
    if max_chars is not None and max_chars > 0:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        units = text.splitlines(True) if by == "lines" else list(text)
        for unit in units:
            if current and current_len + len(unit) > max_chars:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(unit)
            current_len += len(unit)
        if current:
            chunks.append("".join(current))
        return chunks
    if n is None or n <= 0:
        n = 10
    if by == "lines":
        lines = text.splitlines(True)
        size = max(1, (len(lines) + n - 1) // n)
        return ["".join(lines[i:i + size]) for i in range(0, len(lines), size)]
    size = max(1, (len(text) + n - 1) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


def chunk_list(items: Sequence[Any], *, n: Optional[int] = None, max_items: Optional[int] = None) -> list[list[Any]]:
    """Split a sequence into list chunks."""
    seq = list(items)
    if not seq:
        return []
    if max_items is not None and max_items > 0:
        size = max_items
    else:
        if n is None or n <= 0:
            n = 10
        size = max(1, (len(seq) + n - 1) // n)
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def llm_query(prompt: str, *, label: str = "llm_query", timeout_sec: Optional[float] = None,
              auto: bool = True, **kwargs: Any) -> str:
    """Run a child LM query and return wait(...) output."""
    spawn = spawn_agent_auto if auto else spawn_agent
    tid = spawn(str(prompt), label=label, **kwargs)
    return wait([tid], timeout_sec=timeout_sec)


def llm_query_batched(prompts: Iterable[str], *, label: str = "llm_query", timeout_sec: Optional[float] = None,
                      auto: bool = True, **kwargs: Any) -> str:
    """Run many child LM queries concurrently and return combined wait(...) output."""
    spawn = spawn_agent_auto if auto else spawn_agent
    tids = [spawn(str(prompt), label=f"{label}_{i}", **kwargs) for i, prompt in enumerate(prompts)]
    return wait(tids, timeout_sec=timeout_sec) if tids else ""
```

## Patterns

### 1. Capture large tool output

```python
from eggtools import bash
log = bash("git ls-files")
print(preview(log))
```

Do not print the whole value unless the user explicitly asks for it.

### 2. Deterministic filtering first

Use Python before asking subagents/LLMs:

```python
lines = log.splitlines()
test_files = [p for p in lines if 'test' in p.lower()]
config_files = [p for p in lines if p.endswith(('.toml', '.yml', '.yaml', '.json'))]
print(len(test_files), len(config_files))
print(preview('\n'.join(config_files)))
```

### 3. Chunk for semantic processing

Use this when each chunk needs semantic interpretation, not just regex/filtering.
Prefer passing the chunk text directly unless it is too large; this avoids
assuming child threads share the parent's REPL.

```python
chunks = chunk_text(log, n=6)
prompts = [
    "Extract suspicious errors, root-cause clues, and timestamps from this chunk. "
    "Return compact bullets only.\n\n" + chunk
    for chunk in chunks
]
partials = llm_query_batched(prompts, label="log_chunk", timeout_sec=600)
print(preview(partials))
```

Then combine in the parent REPL:

```python
final = llm_query(
    "Synthesize these chunk findings into a concise root-cause analysis:\n" + partials,
    label="synthesis",
    timeout_sec=600,
)
print(final)
```

If the context is too large to pass directly, write chunks to files and pass file
paths, or explicitly spawn children with `share_session=True, share_repl=True`
and give them code/instructions for the shared REPL variable. Use shared REPLs
only deliberately.

### 4. Ad-hoc shared-REPL worker pattern

When a shared REPL is definitely enabled and appropriate, keep the orchestration
plain and visible instead of relying on a hidden helper API:

```python
chunks = chunk_text(log, n=4)
partials = {}
tids = []
for i in range(len(chunks)):
    tids.append(spawn_agent_auto(
        "Use python_repl in the shared REPL. Analyze only chunks[%d]. "
        "Store compact bullets in partials[%d]. Do not print the raw chunk. "
        "Return a short status." % (i, i),
        label=f"chunk_{i}",
        share_session=True,
        share_repl=True,
        allowed_tools=["python_repl"],
    ))
print(wait(tids, timeout_sec=600))
summary_input = "\n\n".join(f"Chunk {i}: {partials.get(i, '')}" for i in range(len(chunks)))
print(llm_query("Synthesize:\n" + summary_input, timeout_sec=600))
```

### 5. Recursive query over context

For one-off recursive RLM-style analysis, just spawn a child with clear
instructions and either pass bounded context, a file path, or shared-session
settings:

```python
answer = llm_query(
    "Use python_repl as a persistent workspace. Inspect/slice/search the context; "
    "do not print raw context. Find the likely root cause and cite evidence.\n\n"
    + preview(log, max_lines=200, max_chars=20000),
    label="rlm_query",
    timeout_sec=600,
    allowed_tools=["python_repl", "bash_repl"],
)
print(answer)
```

## Heuristics

- Use Python for exact filtering, counting, parsing, joining, sorting, and
  verification.
- Use subthreads/LLM calls for semantic classification, summarization, ambiguous
  evidence extraction, and synthesis.
- Prefer fewer larger subcalls over thousands of tiny calls.
- Keep intermediate outputs in variables like `log`, `docs`, `chunks`,
  `partials`, `candidates`, `evidence`, and `final`.
- Print bounded previews with `preview()`.
- Mention important persistent variables to the user when they may matter later.
- Do not store secrets in REPL variables unless necessary for the task.
