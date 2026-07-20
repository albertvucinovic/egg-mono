# Egg

<p align="center">
  <img src="egg-harness-honest.png" alt="An egg-shaped agent workstation connected to tools in a busy workshop" width="680">
</p>

<p align="center">
  <strong>A local agent workspace with durable threads, tool execution, and terminal and web clients.</strong>
</p>

Egg is an open-source environment for working with LLM agents on real projects. Instead of keeping a conversation in an ephemeral chat buffer, Egg stores it as a SQLite-backed thread with ordered events, snapshots, tool calls, artifacts, and optional child threads. The terminal client (**Egg**) and browser client (**EggW**) share the same runtime and thread model.

Egg is most useful when work needs to branch, use tools, survive restarts, or remain inspectable over time. It is an active source project—not a hosted service, a polished binary release, or an IDE replacement.

## Why Egg

- **Durable, branchable work.** Threads and parent/child relationships persist locally, with event history and snapshots for inspection and recovery.
- **Terminal and web clients.** Choose a Rich-based terminal UI or a FastAPI + Next.js web UI for the same workspace.
- **Tools with lifecycle and approval.** Shell and Python execution, persistent REPLs, web access, attachments, artifacts, image generation, and child-agent coordination are tracked as tool calls—not hidden in prose.
- **First-class child agents.** Spawn workers, wait for them, inspect their state, send follow-ups, and use their outputs under explicit access rules.
- **Compaction without history loss.** Move the provider context forward while retaining the complete stored transcript.
- **Multiple model backends.** `eggllm` normalizes configured OpenAI-compatible Chat Completions, OpenAI Responses, and Anthropic Messages endpoints, including local servers.
- **Long-running work.** Pagination, bounded initial rendering, invocation ownership, cancellation, and recovery paths support large or interrupted threads.

## How state is organized

```text
project directory
└── .egg/
    ├── threads.sqlite         conversations, events, snapshots, tool state
    └── ...                    attachments, artifacts, and runtime data

Egg / EggW
    └── main thread
        ├── research worker
        ├── implementation worker
        └── review worker
```

An agent can inspect and edit the current project, run commands, keep Python or Bash state alive between calls, delegate bounded work to child threads, and return generated files as artifacts. You can inspect the same work in either client and decide which tools require approval.

## Quick start

### Prerequisites

- Python 3.10+ and the Python `venv` module (CI uses Python 3.11)
- Bash and Make
- Node.js 18.17+ and npm for EggW
- A provider credential, ChatGPT OAuth login, or a local OpenAI-compatible endpoint

The launch scripts are currently best tested on Linux and WSL. EggW also uses `nc`, `setsid`, and GNU-compatible `readlink -f`. Docker is optional; it is used for Docker-backed sandbox/session execution and local SearXNG search.

### Clone and configure

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
cp dot.env.example .env
```

Edit `.env` and configure a provider you use. For example:

```bash
export OPENAI_API_KEY=***
export DEFAULT_MODEL="GPT 5.3 Codex high"
```

Choose a display name from `eggconfig/eggconfig/data/models.json`, or select one later with `/model`. Other configured provider keys include `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, and `DEEPSEEK_API_KEY`.

For a local OpenAI-compatible server, configure the local endpoint instead:

```bash
export LOCAL_API_KEY=***
export API_BASE=http://localhost:10000/v1/chat/completions
export API_MODEL=your-model-id
export DEFAULT_MODEL="your configured local model name"
```

You can also start a client and run `/login` for the bundled ChatGPT OAuth path. Tokens are stored in `~/.eggllm/auth.json`. Availability depends on the account and upstream service; API-key and local-provider configuration remain fully supported alternatives.

> `.env` is sourced as shell code by the launchers. Keep it private and use shell-compatible assignments such as `export KEY=value`.

### Run Egg

Terminal:

```bash
./egg/egg.sh
```

Browser:

```bash
./eggw/eggw.sh
```

The first launch creates `venv/` and installs the Python packages if needed. EggW also installs frontend dependencies, starts local FastAPI and Next.js development servers, and opens the printed URL when possible.

Both launchers use the directory **from which you invoke them** as the working project. To use Egg on another repository:

```bash
cd /path/to/your/project
/path/to/egg-mono/egg/egg.sh
# or: /path/to/egg-mono/eggw/eggw.sh
```

Project state is stored under that directory's `.egg/` folder.

<details>
<summary><strong>Explicit installation</strong></summary>

If you prefer setup to be separate from launch:

```bash
python3 -m venv venv
source venv/bin/activate
make install
```

Then use either launcher or the installed `egg` console command. Do not use `pip install -e .`: the root package is workspace metadata, while `make install` installs the monorepo components in dependency order.

</details>

## Core capabilities

### Threads and context

- Create, duplicate, delete, pause, resume, and navigate threads.
- Spawn child threads for parallel work or review.
- Wait for children, inspect their status, and continue failed subthreads.
- Compact provider context from a chosen message while preserving stored history.
- Inspect current provider-context tokens separately from full-thread tokens.
- Continue from an earlier message when a run needs repair or redirection.

### Tools and persistent sessions

The runtime includes tools for:

- one-shot Bash and Python execution;
- persistent Python and Bash REPL channels;
- web search and readable-page fetching;
- local attachments and provider-output artifacts;
- image generation;
- user questions and approval-preserving interaction;
- child-agent spawning, messaging, status, and waiting;
- bounded reading of long tool outputs;
- packaged workflow skills.

Tools carry timeouts and lifecycle state. Approval, sandbox, and child-tool policies are explicit; auto-approval is available but optional.

### Two interfaces

**Egg (terminal)** provides streaming output, editable multiline input, autocomplete, approvals, transcript display modes, themes, attachments, and thread controls. Run `/help` for the command catalog.

**EggW (browser)** provides thread-tree navigation, paginated transcripts, live streaming, tool approval, attachments and artifact previews, model and sandbox settings, token and cost information, keyboard-accessible controls, and responsive layouts. See [the EggW documentation](eggw/README.md) for manual startup and deployment details.

## Monorepo architecture

| Package | Role |
| --- | --- |
| [`eggthreads`](eggthreads/README.md) | SQLite thread/event runtime, snapshots, schedulers, leases, tools, approvals, compaction, sessions, artifacts, and recovery |
| [`egg`](egg) | Terminal client built on `eggthreads` and `eggdisplay` |
| [`eggw`](eggw/README.md) | FastAPI backend and Next.js/React web client |
| [`eggllm`](eggllm/README.md) | Model/provider routing and normalized streaming events; it deliberately does not own threads or execute tools |
| [`eggconfig`](eggconfig) | Bundled model and image-generation configuration data |
| [`eggdisplay`](eggdisplay/README.md) | Rich-based editor, panels, layouts, and terminal rendering primitives |
| [`eggflow`](eggflow/README.md) | Separate cached async task-composition library with optional `eggthreads` integration |

```text
Egg or EggW
    │
    ▼
eggthreads ── SQLite events / snapshots / leases / artifacts
    │
    ├── tool and session runtime
    └── eggllm ── configured model provider
```

`eggflow` is useful for explicit cached task graphs, but it is a separate library—not the scheduler underneath ordinary Egg conversations.

## Safety and data

Egg can execute code and operate on the working directory. Treat it like a developer tool with shell access:

- Without an explicitly configured sandbox, execution can occur on the host with the launching user's permissions.
- Review tool requests when you do not trust the prompt, model, or input data.
- Keep `.env`, `~/.eggllm/auth.json`, provider credentials, and project `.egg/` data private.
- “Local workspace” does not imply local inference: cloud model providers receive selected context and may receive tool output or attachments; configured web providers receive search queries and requested URLs.
- To back up a project, include its complete `.egg/` directory. Stop Egg/EggW first or use SQLite's backup facilities so the database and WAL state are copied consistently.

EggW is loopback-only by default. `/health` is public; all other REST, SSE, and WebSocket endpoints require authentication. Non-loopback operation requires explicit `EGGW_PUBLIC=1`, an operator-provided token, exact allowed origins, and an HTTPS browser-facing API URL. Put normal TLS and network controls in front of it. See the [full EggW security checklist](eggw/README.md#security-and-network-configuration).

## Configuration

Canonical bundled configuration lives under `eggconfig/eggconfig/data/`:

- `models.json` — providers, models, aliases, endpoint types, defaults, and parameters;
- `all-models.json` — cached provider catalogs for broader model selection;
- `image-generation-models.json` — image-generation backends.

[`dot.env.example`](dot.env.example) documents provider credentials, local endpoints, the initial model override, and web search/fetch routing. EggW's bind, origin, token, and public-mode settings are documented separately in its README.

## Development

Create and activate a virtual environment, then run the Python suites:

```bash
python3 -m venv venv
source venv/bin/activate
make test
```

`make test` installs development dependencies and runs the seven Python component suites. Cross-client integration tests are separate:

```bash
PYTHONPATH=egg:eggw:eggthreads:eggconfig:eggdisplay:eggllm \
  pytest integration_tests -q
```

EggW frontend checks:

```bash
cd eggw/frontend
npm ci
npm run test:unit
npx tsc --noEmit --pretty false
npm run build
npx playwright install chromium   # once, if needed
npm test                          # starts isolated test servers via Playwright
```

Useful focused commands:

```bash
pytest eggthreads/tests -q
pytest egg/tests -q
pytest eggw/tests -q
make lint   # focused Pyflakes check for eggllm and eggthreads
```

## Project status

Egg is under active development. Its durable runtime, clients, model adapters, tools, sessions, and tests are implemented, but the tradeoffs are real:

- setup assumes a source checkout rather than a packaged desktop application;
- Linux/WSL is the best-tested launch path;
- supporting many providers creates adapter and configuration complexity;
- tool-rich workflows expose more concepts than a conventional chat UI;
- neither client is an IDE integration or a hosted service.

If you want a simple stateless chat box, Egg is probably more machinery than you need. If you want local, inspectable agent work that can branch, use tools, survive restarts, and keep its history, that machinery is the point.

## Documentation

- [EggW: web UI, API, synchronization, and deployment](eggw/README.md)
- [eggthreads: runtime overview and examples](eggthreads/README.md)
- [eggthreads API reference](eggthreads/API.md)
- [eggllm: provider router](eggllm/README.md)
- [eggflow: cached task composition](eggflow/README.md)
- [eggdisplay: terminal UI primitives](eggdisplay/README.md)

Issues and pull requests are welcome. For substantial changes, opening an issue first is useful because runtime, client, and persistence behavior often cross package boundaries.

## License

[MIT](LICENSE) © 2026 Albert Vučinović.
