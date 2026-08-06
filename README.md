# Egg

<p align="center">
  <img src="egg-harness-honest.png" alt="An egg-shaped agent workstation connected to tools in a busy workshop" width="680">
</p>

<p align="center">
  <strong>A local workspace for durable, tool-using AI agents.</strong>
</p>

Egg turns an ordinary project directory into a persistent agent workspace. Conversations, tool calls, artifacts, and parent/child relationships are stored locally in SQLite, so work can branch, survive restarts, and remain inspectable. Use the terminal client (**Egg**) or the browser client (**EggW**); both run on the same thread and tool runtime.

Egg is source-first software under active development. It is best suited to long-running engineering or research work where agents need real tools and durable state—not to simple stateless chat.

## Highlights

- **Durable threads** — append-only event history, snapshots, recovery, and restart-safe local state.
- **Agent trees** — delegate work to child threads, monitor them, send follow-ups, and wait for results.
- **Real tools** — Bash, Python, persistent REPLs, web search/fetch, attachments, artifacts, and image generation.
- **Explicit control** — tool approval, sandbox policy, cancellation, timeouts, and inspectable lifecycle state.
- **Lossless compaction** — shorten provider context without deleting the stored transcript.
- **Two clients** — a Rich terminal interface and a FastAPI + Next.js web interface.
- **Multiple model backends** — OpenAI-compatible Chat Completions and Responses APIs, Anthropic Messages, ChatGPT OAuth, and local servers through `eggllm`.

## How it works

Egg stores workspace state under the directory where you launch it:

```text
your-project/
├── source files
└── .egg/
    ├── threads.sqlite       threads, events, snapshots, leases, tool state
    └── ...                  attachments, artifacts, and session data
```

A conversation is a thread. A thread may have child threads for delegated or parallel work:

```text
main thread
├── research worker
├── implementation worker
└── review worker
```

The complete history remains local and inspectable. Selected context and tool output are sent to whichever model or web providers you configure.

## Quick start

### Requirements

- Linux or WSL (the best-tested launch path)
- Python 3.10+ with `venv`
- Bash and Make
- A provider API key, ChatGPT OAuth login, or a local OpenAI-compatible model
- Node.js 18.17+ and npm only if you use EggW

Docker is optional. It is used for Docker-backed sandbox/session execution and the bundled local SearXNG service.

### 1. Clone and configure

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
cp dot.env.example .env
```

Add credentials for at least one configured provider to `.env`, for example:

```bash
export OPENAI_API_KEY=***
export EGG_STARTING_MODEL="GPT 5.3 Codex high"
```

Alternatively, start Egg and run `/login` for ChatGPT OAuth. For local or self-hosted models, see [`dot.env.example`](dot.env.example) and the bundled model configuration in [`eggconfig/eggconfig/data/models.json`](eggconfig/eggconfig/data/models.json).

> The launchers source `.env` as shell code. Keep it private and use shell-compatible assignments.

### 2. Launch a client

Terminal client:

```bash
./egg/egg.sh
```

Web client:

```bash
./eggw/eggw.sh
```

On first launch, the scripts create `venv/` and install the Python packages. EggW also installs its frontend dependencies and prints the local browser URL.

### 3. Use Egg in another project

The current working directory—not the Egg checkout—is the workspace:

```bash
cd /path/to/your/project
/path/to/egg-mono/egg/egg.sh
# or
/path/to/egg-mono/eggw/eggw.sh
```

That project receives its own `.egg/` state directory.

<details>
<summary><strong>Install explicitly instead of on first launch</strong></summary>

```bash
python3 -m venv venv
source venv/bin/activate
make install
```

`make install` installs the monorepo packages in dependency order. Do not use `pip install -e .`; the root package contains workspace metadata and does not install the clients.

</details>

## First commands

Run `/help` for the complete command catalog. Useful starting points include:

```text
/model                         show or change the model
/threads                       list root threads and their subtrees
/newThread                     start another root conversation
/spawnChildThread <task>       delegate work to a child thread
/waitForThreads <threads>      wait for child work
/show                          inspect transcript records
/compactWithSummary            reduce provider context, preserving history
/toolsStatus                   inspect tool policy and availability
/toggleSandboxing              change sandbox behavior
/pythonRepl <code>             use the persistent Python REPL
```

The model also receives structured tools for child-agent coordination, command execution, persistent REPLs, web access, attachments, and artifacts.

## Architecture

```text
Terminal client                         Browser client
┌──────────────┐                        ┌──────────────────────┐
│ egg          │                        │ EggW Next.js UI      │
│ + eggdisplay │                        └──────────┬───────────┘
└──────┬───────┘                                   │ HTTP / SSE / WebSocket
       │                                ┌──────────▼───────────┐
       │                                │ EggW FastAPI backend │
       │                                └──────────┬───────────┘
       └──────────────────┬────────────────────────┘
                          ▼
                 ┌─────────────────┐
                 │ eggthreads      │
                 │ threads, events │
                 │ runners, tools  │
                 └───────┬─────────┘
                         │
                         ▼
                 ┌─────────────────┐       ┌─────────────────┐
                 │ eggllm          │◄──────│ eggconfig       │
                 │ provider router │       │ model catalogs  │
                 └───────┬─────────┘       └─────────────────┘
                         │
                         ▼
                 configured model provider

Separate durable workflow stack:

  eggopt ──► eggflow   (cached task composition)
     └─────► eggthreads (agent threads, tools, and durable interactions)
```

| Package | Responsibility |
| --- | --- |
| [`egg`](egg) | Terminal client, rendering, input, and interactive workflows |
| [`eggw`](eggw/README.md) | FastAPI backend and Next.js browser client |
| [`eggthreads`](eggthreads/README.md) | Durable thread runtime, event projection, schedulers, tools, sessions, approvals, compaction, and recovery |
| [`eggllm`](eggllm/README.md) | Provider configuration, model routing, and normalized streaming events |
| [`eggconfig`](eggconfig) | Bundled model and image-generation configuration |
| [`eggdisplay`](eggdisplay/README.md) | Terminal editor, panels, and layout primitives |
| [`eggflow`](eggflow/README.md) | Separate SQLite-cached task composition framework |
| [`eggopt`](eggopt/README.md) | Durable agentic optimization and scientific-discovery compositions: standalone operations, Actor–Critic/Git-Critic loops, GEPA search, and Physics strategy presets |

`eggflow` and `eggopt` are a separate workflow stack, not the scheduler behind
ordinary Egg conversations. `eggopt` composes cached Eggflow tasks with durable
Eggthreads agents; it currently provides a standalone operation boundary,
reusable Actor–Critic and Git-Critic primitives, finite-JSON GEPA search, and
verified or latent Physics strategies for model-based scientific discovery.

## Safety and privacy

Egg can execute code and modify the current project. Treat it as a developer tool with shell access.

- Without a configured sandbox, tools may run on the host with your user permissions.
- Review tool requests when you do not trust the model, prompt, or input.
- Keep `.env`, `~/.eggllm/auth.json`, and project `.egg/` directories private.
- “Local workspace” does not mean local inference: configured model providers receive selected context, and configured web providers receive queries or URLs.
- Back up the entire `.egg/` directory. Stop Egg/EggW first or use SQLite backup facilities so the database and WAL are copied consistently.

EggW binds to loopback and protects its API by default. Exposing it beyond the local machine requires explicit public-mode, token, origin, and HTTPS configuration. Read the [EggW security documentation](eggw/README.md#security-and-network-configuration) before doing so.

## Configuration

The main configuration sources are:

- [`.env` template](dot.env.example) — credentials, local endpoints, and web backends;
- [`models.json`](eggconfig/eggconfig/data/models.json) — providers, model aliases, defaults, parameters, and costs;
- [`all-models.json`](eggconfig/eggconfig/data/all-models.json) — cached provider catalogs;
- [`image-generation-models.json`](eggconfig/eggconfig/data/image-generation-models.json) — image backends.

Use `EGG_STARTING_MODEL` to choose the initial model for new threads, or `/model` to change it interactively. EggW-specific network and deployment settings are documented in [`eggw/README.md`](eggw/README.md).

## Development

Create a development environment and run all Python component suites:

```bash
python3 -m venv venv
source venv/bin/activate
make test
```

Focused suites:

```bash
pytest eggthreads/tests -q
pytest egg/tests -q
pytest eggw/tests -q
pytest eggllm/tests -q
pytest eggflow/tests -q
pytest eggopt/tests -q
make lint
```

Cross-client integration tests:

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
npx playwright install chromium   # once
npm test
```

## Documentation

- [EggW usage, API, synchronization, and security](eggw/README.md)
- [eggthreads runtime guide](eggthreads/README.md)
- [eggthreads API reference](eggthreads/API.md)
- [eggthreads system design](eggthreads/system-design.md)
- [eggllm provider router](eggllm/README.md)
- [eggflow task framework](eggflow/README.md)
- [eggopt agentic optimization and Physics strategies](eggopt/README.md)
- [eggdisplay terminal primitives](eggdisplay/README.md)

## Status

Egg is under active development. The runtime and clients are functional and heavily tested, but setup is still source-checkout oriented, Linux/WSL receives the most testing, and tool-rich workflows expose more concepts than a conventional chat application.

## License

[MIT](LICENSE) © 2026 Albert Vučinović.
