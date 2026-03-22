# egg-mono

A modular AI conversation platform. Tree-structured threads, multi-provider LLM routing, sandboxed tool execution — usable from a terminal TUI or a web UI.

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
  ┌─┴──┐
 egg  eggw
```

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
