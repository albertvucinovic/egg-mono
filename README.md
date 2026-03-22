# egg-mono

A modular AI conversation platform. Tree-structured threads, multi-provider LLM routing, sandboxed tool execution — usable from a terminal TUI, a web UI, or as building blocks for headless agents.

## Packages

| Package | Description |
|---------|-------------|
| **eggthreads** | Core engine — SQLite-backed tree-structured conversation threads with async runners, sandboxing, and tool execution |
| **eggllm** | LLM router with OpenAI-compatible provider abstraction (OpenAI, Anthropic, Google, DeepSeek, Groq, and more) |
| **eggflow** | Task-based execution engine with caching, crash recovery, and optional eggthreads integration |
| **eggdisplay** | Rich-based TUI text editor and display panels |
| **eggconfig** | Shared model configuration data |
| **egg** | CLI chat interface (TUI) |
| **eggw** | Web UI — FastAPI backend + Next.js frontend |

## Architecture

```
eggconfig    eggdisplay
    │            │
eggllm       eggflow
    │        ╱
eggthreads ─┘
     │
   ┌─┼────┐
egg  │    eggw
     │
  your agent
```

The core libraries (`eggthreads`, `eggllm`, `eggflow`, `eggconfig`) have no UI dependencies and can be composed into headless agents that run unattended. `egg` and `eggw` are just two frontends built on top of them.

## Install

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
python3 -m venv venv && source venv/bin/activate
make install
```

Set up API keys:

```bash
cp .env.example .env
# Edit .env with your provider keys
```

### Use as a dependency

Individual packages can be installed directly from GitHub:

```
eggthreads @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggthreads
eggflow[eggthreads] @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggflow
```

## Usage

**Terminal UI:**

```bash
./egg/egg.sh
```

**Web UI:**

```bash
./eggw/eggw.sh
```

**Headless agent:**

The core packages can drive LLM conversations programmatically — no UI needed. Spawn a thread tree, route to any provider, execute tools in sandboxes, and let `eggflow` handle caching and crash recovery. A minimal example:

```python
import asyncio
from eggthreads import (
    ThreadsDB, SubtreeScheduler, RunnerConfig, create_llm_client,
    create_root_thread, create_child_thread, append_message,
    create_snapshot, set_subtree_tools_enabled, wait_subtree_idle,
)

async def main():
    db = ThreadsDB()
    db.init_schema()

    root = create_root_thread(db, name="batch")
    for i in range(3):
        child = create_child_thread(db, root, name=f"agent-{i}")
        append_message(db, child, "system", "You are a helpful assistant.")
        append_message(db, child, "user", f"Write a haiku about topic #{i}.")
        create_snapshot(db, child)

    set_subtree_tools_enabled(db, root, True)

    llm = create_llm_client()
    scheduler = SubtreeScheduler(db, root_thread_id=root, llm=llm,
                                  config=RunnerConfig(max_concurrent_threads=3))
    task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    await wait_subtree_idle(db, root)
    task.cancel()

asyncio.run(main())
```

For a real-world example see [EvolveTropy](https://github.com/albertvucinovic/EvolveTropy), which uses `eggthreads` + `eggflow` to run dozens of sandboxed LLM agents in parallel with full crash recovery.

### Run from anywhere

Add the following to your `~/.bashrc` (or `~/.zshrc`):

```bash
export EGG_MONO_HOME="$HOME/path/to/egg-mono"  # adjust to your clone location
alias egg="$EGG_MONO_HOME/egg/egg.sh"
alias eggw="$EGG_MONO_HOME/eggw/eggw.sh"
```

Then reload your shell (`source ~/.bashrc`) and run `egg` or `eggw` from any directory.

## Development

```bash
make install-dev   # install with test dependencies
make test          # run all tests
make lint          # pyflakes
make clean         # remove build artifacts
```

## License

[MIT](LICENSE)
