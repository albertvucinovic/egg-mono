# Egg

Egg is Entropy Gradient's local-first AI workbench. It gives you durable
SQLite-backed conversations, terminal and web frontends, provider-agnostic LLM
routing, tool execution, persistent REPL sessions, and child-agent workflows in
one monorepo.

Egg is useful when you want an assistant that can keep a real project history,
run tools safely, delegate work to subthreads, recover from crashes, and resume
from compacted context without losing the full transcript.

## Highlights

- **Durable threads** — every conversation is an append-only event log in
  `.egg/threads.sqlite`, with snapshots for fast reads and full raw history kept
  for audit/debugging.
- **Terminal and web UIs** — `egg` is a Rich-based terminal TUI; `eggw` is a
  FastAPI + React web UI over the same thread engine.
- **LLM routing** — model aliases and provider settings live in shared
  `models.json` data and support OpenAI-compatible chat/responses providers.
- **Tools** — bash, Python, web search/fetch, skills, compaction, persistent
  REPLs, subagents, and long-output retrieval are registered through one tool
  system.
- **Subagents** — spawn child threads, auto-approved child threads, send follow-up
  guidance, inspect child status, continue descendants, and wait for results.
- **Persistent sessions** — Python and Bash REPL tools can keep state across tool
  calls; Python REPLs are hydrated with transcript helpers for exact inspection.
- **Compaction without data loss** — compaction moves the future provider-context
  start pointer while leaving UI/raw history available.
- **Sandboxing and approval** — tool approval, auto-approval, raw-output hiding,
  Docker/bubblewrap/srt sandboxing, and session containers are first-class.
- **Cost and usage reporting** — `/cost` separates API-confirmed usage from
  estimates and reports cached/input/output token counts when providers return
  them.

## Repository layout

| Package | Purpose |
| --- | --- |
| `egg` | Terminal application (`./egg/egg.sh` or `egg`). Owns terminal-only UI behavior such as themes. |
| `eggw` | Web application (`./eggw/eggw.sh`): FastAPI backend plus React frontend. |
| `eggthreads` | Core thread engine: SQLite schema, event log, snapshots, runners, scheduler, commands, tools, compaction, sandbox/session integration, and subagents. |
| `eggllm` | Lightweight LLM client/router for model aliases, provider configs, streaming, reasoning, usage accounting, and tool-call deltas. |
| `eggdisplay` | Independent Rich/readchar terminal display library used by `egg`. |
| `eggflow` | Async task/caching framework, optionally integrated with `eggthreads`. |
| `eggconfig` | Shared packaged model configuration data. |

```text
        eggconfig/data/models.json
                  │
                eggllm
                  │
              eggthreads
       ┌──────────┼──────────┐
      egg        eggw     headless APIs
       │
  eggdisplay

  eggflow can be used independently or with eggthreads.
```

## Requirements

- Python 3.10+ (the project is primarily exercised on Python 3.11).
- Node.js/npm for the web frontend (`eggw`).
- Docker is recommended for sandboxed tool execution, persistent sessions, and
  the local SearXNG search backend, but Egg can also run without Docker.
- API keys for whichever LLM providers you configure/use.

## Quick start

Clone and install editable packages:

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
python3 -m venv venv
source venv/bin/activate
make install
```

Or let the launcher create/install the monorepo venv on first run:

```bash
./egg/egg.sh
```

Create an environment file if you need provider keys or local defaults:

```bash
cp dot.env.example .env
$EDITOR .env
```

Common variables include:

```bash
export OPENAI_API_KEY=...
export GOOGLE_API_KEY=...
export DEFAULT_MODEL="GPT 5 Nano"
export EGG_WEB_BACKEND=searxng
export SEARXNG_URL=http://localhost:8888
```

## Running Egg

Terminal UI:

```bash
./egg/egg.sh
```

Web UI:

```bash
./eggw/eggw.sh
```

`eggw.sh` starts a backend and frontend, chooses free ports, and stores thread
state in the caller's `.egg/threads.sqlite`, just like the terminal app.

Installed console script after `make install`:

```bash
egg
```

## Where state lives

Egg stores local state in the working directory where you start it:

```text
.egg/threads.sqlite      # threads, events, snapshots, tool state, settings
.egg/egg_outputs/        # stashed long tool outputs, when applicable
.egg/rlm_sessions/       # optional persistent REPL session bridge/runtime state
```

Compaction never deletes the old transcript. It records a new API-context start
point so future provider calls are shorter while the UI and raw event log remain
inspectable.

## Terminal workflow cheat sheet

Core:

```text
/help                         show slash commands
/reload                       restart Egg and reopen the current thread
/model [key]                  inspect or switch the active model
/cost                         show token usage, cache usage, and cost
/theme [name]                 list or switch terminal themes
```

Threads, agents, and compaction:

```text
/threads                      list threads
/thread <selector>            switch threads
/newThread <name>             create a new root thread
/continue [msg_id=<id>]       continue from the current or selected point
/compact [msg_id]             move provider context start
/compactWithSummary           ask for a summary and compact to it
/spawnChildThread <text>      create a child thread
/spawnAutoApprovedChildThread <text>
/waitForThreads <threads>     wait for child-thread results
/setThreadPriority ...        tune scheduler priority/settings
```

Tools, sessions, and sandboxing:

```text
/toolsStatus                  show tool configuration and availability
/toolsOn / /toolsOff          enable or disable model tool calls
/toggleAutoApproval           toggle global tool auto-approval
/sessionOn provider=docker    enable persistent REPL sessions
/pythonRepl <code>            run code in the persistent Python REPL
/bashRepl <script>            run script in the persistent Bash REPL
/toggleSandboxing             toggle sandboxing for the subtree
/getSandboxingConfig          show effective sandbox config
```

Display/input:

```text
/displayMode full-screen|inline
/displayVerbosity max|medium|min
/toggleBorders
/togglePanel chat|children|system
/paste
/enterMode send|newline
```

Input shortcuts:

```text
$ <command>       queue a visible bash tool call
$$ <command>      queue a hidden/local bash tool call
Ctrl+C            cancel/interrupt active work when possible
Ctrl+D            send input
Alt+Enter         insert a newline
```

## Built-in model-visible tools

Egg's default tool registry includes:

- `bash` and `python` for one-off execution;
- `python_repl` and `bash_repl` for persistent sessions;
- `web_search` and `fetch_url` for web retrieval;
- `skill` for packaged workflow instructions;
- `compact_thread` for context management;
- `spawn_agent`, `spawn_agent_auto`, `send_message_to_child`,
  `continue_subthread`, `get_child_status`, and `wait` for subagents;
- `answer_user_while_preserving_llm_turn` and
  `get_user_message_while_preserving_llm_turn` for interactive long-running
  assistant workflows;
- `read_long_tool_output` for bounded retrieval of stashed large outputs.

Tool schemas automatically receive a canonical `timeout` argument when useful,
and the terminal UI shows dynamic timeout countdowns for running tools.

## Sandboxing, sessions, and search

Sandbox providers are configured through `eggthreads` and surfaced in the UI:

| Provider | Notes |
| --- | --- |
| Docker | Recommended default when available. Build the local image with `./eggthreads/docker/create-image.sh`. |
| bubblewrap | Lightweight Linux sandbox. |
| srt | Anthropic sandbox runtime integration. |
| off | Disable sandboxing with `EGG_SANDBOX_MODE=off` or `/toggleSandboxing`. |

Persistent REPL sessions can use Docker or memory-backed providers. Docker is
also used by the optional local SearXNG backend. Web search is pluggable via
`EGG_WEB_BACKEND`; SearXNG is the default local backend and Tavily is available
when configured with `TAVILY_API_KEY`.

## Model configuration

Default model data is packaged in `eggconfig/data/models.json` and
`eggconfig/data/all-models.json`. Provider entries can define model aliases,
API bases, reasoning options, usage/cost metadata, prompt-cache fields, and
provider-specific extra body fields.

Use:

```text
/model
/updateAllModels <provider>
```

to inspect/switch models and refresh provider catalogs from the UI.

## Using packages directly

Each package can be installed from its subdirectory, for example:

```text
eggthreads @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggthreads
eggllm @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggllm
eggflow[eggthreads] @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggflow
```

Start with package-specific docs for lower-level usage:

- `eggthreads/README.md` for headless thread/scheduler APIs;
- `eggllm/README.md` for LLM routing;
- `eggflow/README.md` for cached async tasks;
- `eggdisplay/README.md` for the standalone terminal display library;
- `eggw/README.md` for web UI notes.

## Development

Install dev dependencies:

```bash
make install-dev
```

Run the full Python test suite:

```bash
make test
```

Focused examples:

```bash
pytest eggthreads/tests -q
pytest eggdisplay/tests -q
pytest egg/tests -q
pytest eggw/tests -q
cd eggw/frontend && npx tsc --noEmit --pretty false
```

Basic lint target:

```bash
make lint
```

## Design notes

- `eggthreads` owns persistent conversation state; UI clients should not invent
  parallel state machines for thread semantics.
- `eggdisplay` is intentionally independent from Egg-specific code and can be
  used as a generic Rich terminal display library.
- Terminal-only presentation features live in `egg`; browser-only presentation
  features live in `eggw`; shared backend behavior belongs in `eggthreads`.
- Compaction changes provider context, not local history.

## License

[MIT](LICENSE)
