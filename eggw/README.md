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
export EGGW_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export EGGW_ALLOWED_ORIGINS="http://localhost:3000"
hypercorn eggw.main:app --bind 127.0.0.1:8000
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

### Security and network configuration

`eggw.sh` is secure by default: it binds the backend to `127.0.0.1`, generates a
fresh high-entropy API token when `EGGW_API_TOKEN` is unset, provisions it at
runtime only to its loopback frontend without printing or bundling it, and
permits only the launched local frontend origin. Health checks at `/health`
remain public; every other REST endpoint plus SSE and WebSocket connections
require the token.

Configuration variables:

- `EGGW_API_TOKEN`: explicit API token (at least 32 non-whitespace characters).
  Loopback-only `eggw.sh` launches generate one when omitted. Public mode
  requires an operator-provided token and never exposes it through frontend
  build-time variables or private bootstrap.
- `EGGW_ALLOWED_ORIGINS`: comma-separated exact `http://` or `https://` browser
  origins. Wildcards are rejected. The launcher defaults this to the local
  frontend on `EGGW_FRONTEND_PORT`.
- `EGGW_BIND_HOST`: backend bind address. Loopback values work by default.
- `EGGW_FRONTEND_BIND_HOST`: frontend bind address. It defaults to loopback in
  every mode. A non-loopback value additionally requires `EGGW_PUBLIC=1`.
- `NEXT_PUBLIC_API_URL`: browser-facing API origin. Public launcher mode
  requires an explicit `https://` URL because a listener address cannot be
  inferred safely for a remote browser.
- `EGGW_PUBLIC=1`: mandatory explicit acknowledgement for any non-loopback bind.
  For example, use `EGGW_PUBLIC=1 EGGW_BIND_HOST=0.0.0.0` together with an
  explicit token, `NEXT_PUBLIC_API_URL`, and the real public frontend origin.
  Public mode fails closed if either browser-facing URL setting is omitted. Put
  TLS and normal network access controls in front of EggW; the bearer token is
  an API capability, not a replacement for encrypted transport.

### Browser credential threat model

A public frontend is available to untrusted visitors, so any `NEXT_PUBLIC_*`
value is public and cannot protect its API. EggW never embeds the bearer token
in browser-delivered JavaScript. The loopback launcher may serve its generated
token from a same-origin, no-store runtime bootstrap endpoint because both the
frontend and backend are bound to the local machine. This assumes a trusted
local host: loopback is host-local, not isolated from other local OS users or
processes, and private bootstrap must not be published through a reverse proxy.
Public and manual frontend deployments disable that bootstrap; each authorized
user enters the operator-
provided token into the connection screen. The browser keeps this token only in
memory and tab-scoped `sessionStorage` (not `localStorage`, cookies, or a URL).
Closing the tab ends that browser session.

The runtime token is sent in the `Authorization` header for REST/fetch-SSE and
in a WebSocket subprotocol for browser WebSockets. It is never placed in query
strings or logged by EggW. Manual non-browser callers use
`Authorization: Bearer <token>`.

Operational requirements for a public deployment:

- terminate TLS at a trusted reverse proxy and forward only to EggW's private
  frontend and backend listeners; never send the bearer capability over
  plaintext networks. Both listeners remain loopback by default, including in
  public mode; set their bind-host variables explicitly only when the network
  topology requires non-loopback upstream sockets;
- set `EGGW_PUBLIC=1`, an explicit `EGGW_API_TOKEN`, browser-facing HTTPS API
  origin in `NEXT_PUBLIC_API_URL`, and the exact HTTPS frontend origin in
  `EGGW_ALLOWED_ORIGINS`;
- keep frontend and API origins stable. `eggw.sh` passes the explicit public
  `NEXT_PUBLIC_API_URL` to Next, but never put credentials in any
  `NEXT_PUBLIC_*` value;
- rotate a compromised token by restarting the backend with a new
  `EGGW_API_TOKEN`; browser tabs holding the old token will receive `401`, clear
  their tab-scoped credential, and return to the connection screen; and
- do not expose the private `/api/eggw-bootstrap` route in public/manual
  deployments. It is enabled only when the launcher supplies a private
  loopback bootstrap token.

`EGGW_ALLOWED_ORIGINS` is an exact browser-origin allowlist, not an
authentication mechanism. Non-browser clients still need the bearer token, and
operators should also apply ordinary firewall, proxy, and access controls.

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
- `GET /api/threads/{id}/messages` — legacy message/compaction-marker array plus
  `X-Egg-Event-Cursor`; pass `envelope=true` for
  `{items, snapshot_cursor, next_before}`. The cursor is the exact event
  watermark represented by the returned page and is valid even with `limit` or
  `before_id` pagination.
- `POST /api/threads/{id}/messages` — send user message
- `GET /api/threads/{id}/stats` — token stats
- `GET /api/models` — configured models
- `POST /api/threads/{id}/model` — switch model
- `GET /api/threads/{id}/tools` — tool-call state
- `POST /api/threads/{id}/tools/approve` — approve/deny tool calls

Streaming endpoints:

- `GET /api/threads/{id}/events` — cursor-resumable server-sent thread events.
  Pass the message `snapshot_cursor` as `after_seq`; on reconnect the browser
  client sends `Last-Event-ID`. Explicit `after_seq` takes precedence. Each
  frame emits `id: <event_seq>` and JSON
  `{event_id,event_seq,type,ts,msg_id,invoke_id,chunk_seq,payload}`. Events are
  strictly after the cursor, so reconnect is duplicate-safe. A cursorless
  connection replays only an exact unexpired lease invocation; unmatched stale
  `stream.open` events do not imply active work. Missing threads return `404`.
- `WS /ws/{id}` — bidirectional websocket channel where enabled

### Synchronization and frontend ownership

The browser treats persisted transcript pages as an infinite React Query value
keyed by thread ID. Drafts, staged attachments, live invocation/tool metadata,
stream buffers, and connection status are also thread-scoped, so an async
completion or pagination request for thread A cannot mutate thread B after
navigation. Optimistic sends are identified by a client operation ID: success
replaces that exact temporary message with the backend `message_id`; failure
removes only that operation and restores its original draft and attachments.

For a gap-free live view, first request the message envelope, then connect SSE
with its `snapshot_cursor`. The frontend resumes transport with `Last-Event-ID`,
rejects duplicate/out-of-order canonical event sequences before UI mutation,
and on reconnect refreshes the authoritative transcript watermark plus
`/api/threads/{id}/state`. Connection state (`connecting`, `connected`, or
`reconnecting`) is separate from lease-backed run state: a dropped network
connection does not mean the thread stopped, and only an unexpired backend lease
identifies active work.

## Development checks

```bash
PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests
cd eggw/frontend && npm run test:unit
cd eggw/frontend && npx tsc --noEmit --pretty false
cd eggw/frontend && npm run build
cd eggw/frontend && npm test
```
