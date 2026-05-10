# egg-mono

Egg is a modular AI conversation platform from Entropy Gradient. It combines
SQLite-backed conversation threads, multi-provider LLM routing, tool execution,
persistent REPL sessions, subagents, and terminal/web frontends.

Use it as:

- an interactive terminal assistant (`egg`);
- a web UI (`eggw`);
- a headless thread/subagent engine (`eggthreads` + `eggllm`);
- a task runtime with caching/crash recovery (`eggflow`).

## Packages

| Package | Description |
| --- | --- |
| `eggthreads` | Core thread engine: append-only event log, snapshots, async runners, subtree scheduler, tools, sandbox/session integration, compaction, and subagents. |
| `eggllm` | Lightweight LLM router for OpenAI-compatible chat/responses providers, model aliases, provider catalogs, streaming, reasoning, and tool-call deltas. |
| `egg` | Terminal chat/TUI frontend. |
| `eggw` | FastAPI + React web frontend. |
| `eggdisplay` | Rich-based inline panels and text editor used by the terminal UI. |
| `eggflow` | Async task execution and caching framework, optionally integrated with `eggthreads`. |
| `eggconfig` | Shared default model configuration data. |

## Architecture

```text
              eggconfig/models.json
                      │
                    eggllm
                      │
                  eggthreads
          ┌───────────┼───────────┐
         egg          eggw      headless agents
          │
     eggdisplay

     eggflow can be used alongside eggthreads for cached tasks.
```

The core thread/event model lives in `eggthreads`. `egg` and `eggw` are clients
of that engine; they do not own the conversation state.

## Current core concepts

- **Stable threads**: one append-only event log per thread, with parent/child
  relationships for delegation and subtree scheduling.
- **Subagents**: model-visible tools can spawn child threads, wait on them, and
  send follow-up guidance.
- **Tools and commands**: model-visible tools, user commands, output approval,
  sandboxing, and long-output stashing share the same event model.
- **Persistent sessions**: Python/Bash REPL sessions can keep state across tool
  calls. Python REPLs are hydrated with `thread_context`, message aliases, and
  helper functions for exact transcript inspection.
- **Compaction**: `thread.compaction` events set the future provider/API context
  start without deleting UI/raw history. `/compact`, `/compactWithSummary`, and
  automatic summary compaction all use the same start-pointer primitive.
- **Token reporting**: `context_tokens` means the current provider/API context
  after compaction; `full_thread_tokens` means the full visible/effective thread
  history before compaction filtering.

## Install

```bash
git clone https://github.com/albertvucinovic/egg-mono.git
cd egg-mono
python3 -m venv .venv
source .venv/bin/activate
make install
```

For development dependencies:

```bash
make install-dev
```

Set API keys in your environment or `.env` file as expected by your
`models.json` provider entries, for example:

```bash
export OPENAI_API_KEY=...
export ANTHROPIC_API_KEY=...
export GOOGLE_API_KEY=...
```

## Run

Terminal UI:

```bash
./egg/egg.sh
```

Web UI:

```bash
./eggw/eggw.sh
```

Headless use starts from `eggthreads` APIs. See `eggthreads/README.md` for a
minimal subtree scheduler example.

## Useful terminal workflows

```text
/help                         # show commands
/model                        # inspect/switch model
/tools                        # inspect tool configuration
/compact                      # set provider context start
/compactWithSummary           # ask assistant to summarize, then compact
/sessionOn provider=docker    # enable persistent REPL sessions
/pythonRepl print(len(all_messages))
/skill rlm                    # load RLM/persistent-REPL workflow notes
```

Compaction does not delete history. Humans can still scroll the full transcript
and copy full `msg_id` values for `/continue <msg_id>` or `/compact <msg_id>`.

## Use as a dependency

Packages can be installed directly from subdirectories:

```text
eggthreads @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggthreads
eggllm @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggllm
eggflow[eggthreads] @ git+https://github.com/albertvucinovic/egg-mono.git#subdirectory=eggflow
```

## Sandboxing and sessions

Tool execution can run unsandboxed or in a sandbox. Supported providers include:

| Provider | Notes |
| --- | --- |
| Docker | Default/recommended when available. Build the local image with `./eggthreads/docker/create-image.sh`. |
| bubblewrap | Linux-only lightweight sandbox. |
| srt | Anthropic sandbox runtime. |
| off | Set `EGG_SANDBOX_MODE=off` to disable sandboxing. |

Docker is also used for optional persistent REPL session containers and the
self-hosted SearXNG search service. See `eggthreads/README.md` and
`eggthreads/eggthreads/web/searxng/README.md`.

## Development

```bash
make install-dev
make test
make lint
```

Focused test examples:

```bash
pytest -q eggthreads/tests/test_compaction.py
pytest -q egg/tests
cd eggw/frontend && npx tsc --noEmit --pretty false
```

## License

[MIT](LICENSE)
