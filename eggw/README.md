# eggw

`eggw` is the web UI for Egg. It provides a FastAPI backend and a React/Next.js
frontend for browsing thread trees, chatting with threads, approving tools,
watching streaming output, and inspecting token/status information.

## Features

- Thread tree navigation with parent/child hierarchy.
- Real-time message streaming.
- Tool-call approval UI.
- Per-thread model selection.
- Message ids displayed for `/continue <msg_id>` and `/compact <msg_id>`
  workflows, with click-to-copy behavior in the chat header.
- Compaction boundary markers that preserve full scrollback.
- Token stats where `context_tokens` is current provider/API context and
  `full_thread_tokens` is full visible/effective history.
- Dark themed React UI.

## Quick start

From the monorepo root:

```bash
./eggw/eggw.sh
```

Then open the URL printed by the script, usually `http://localhost:3000`.

## Manual backend/frontend startup

Backend, from the directory whose `.egg/threads.sqlite` database you want to use:

```bash
export EGG_DB_PATH="$PWD/.egg/threads.sqlite"
export EGG_CWD="$PWD"
hypercorn eggw.main:app --bind 0.0.0.0:8000
```

Frontend:

```bash
cd /path/to/egg-mono/eggw/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev -- -p 3000
```

Then open `http://localhost:3000`.

Hypercorn is preferred because HTTP/2 support avoids the low per-origin
connection limits that make multiple active thread views awkward under HTTP/1.1.

## Configuration

The backend uses the same Egg database and model configuration as the terminal
UI:

- thread DB: usually `.egg/threads.sqlite` under the working directory;
- model config: `models.json` / `all-models.json` through `eggllm`;
- provider keys: environment variables named by each provider's `api_key_env`.

## API overview

Common REST endpoints:

- `GET /api/threads` — list threads
- `GET /api/threads/{id}` — thread details
- `POST /api/threads` — create thread
- `DELETE /api/threads/{id}` — delete thread
- `GET /api/threads/{id}/messages` — messages plus compaction marker items
- `POST /api/threads/{id}/messages` — send user message
- `GET /api/threads/{id}/stats` — token stats
- `GET /api/models` — configured models
- `POST /api/threads/{id}/model` — switch model
- `GET /api/threads/{id}/tools` — tool-call state
- `POST /api/threads/{id}/tools/approve` — approve/deny tool calls

Streaming endpoints:

- `GET /api/threads/{id}/events` — server-sent thread events
- `WS /ws/{id}` — bidirectional websocket channel where enabled

## Development checks

```bash
PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests
cd eggw/frontend && npx tsc --noEmit --pretty false
```
