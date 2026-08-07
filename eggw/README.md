# eggw

`eggw` is Egg's browser client: a FastAPI backend over the shared
[`eggthreads`](../eggthreads/README.md) runtime and a Next.js/React frontend. It
opens the same project-local thread database as the terminal client, so either
client can inspect and continue durable work.

## Features

- Root-thread sidebar and expandable parent/child navigation.
- Paginated transcripts with live SSE and WebSocket updates.
- Streaming text, reasoning, tool arguments/output, timing, and reconnect state.
- Tool approval and interruption controls.
- Shared slash commands, autocomplete, input history, and thread/model settings.
- Monaco-based draft/file editing with a plain-text fallback.
- Attachments, protected artifact previews/downloads, promotion to input, and
  image generation.
- Sandbox, persistent-session, tool-policy, auto-approval, verbosity, token,
  throughput, and cost controls.
- Responsive themes, keyboard shortcuts, syntax highlighting, Markdown, math,
  and code rendering.

## Quick start

Run the launcher **from the project EggW should operate on**:

```bash
cd /path/to/your/project
/path/to/egg-mono/eggw/eggw.sh
```

The first run creates the monorepo `venv/`, installs Python packages and frontend
dependencies, starts Hypercorn and the Next.js development server, warms the UI,
and opens the printed URL when possible. State is stored in
`./.egg/threads.sqlite` under the caller's project.

Requirements beyond the root project are Node.js 18.17+, npm, `nc`, `setsid`,
`curl`, and GNU-compatible `readlink -f`. Set `EGGW_NO_BROWSER=1` to suppress
automatic browser opening.

## Manual startup

Install packages first:

```bash
cd /path/to/egg-mono
python3 -m venv venv
source venv/bin/activate
make install
```

Backend, from the target project:

```bash
export EGG_DB_PATH="$PWD/.egg/threads.sqlite"
export EGG_CWD="$PWD"
export EGGW_API_TOKEN="replace-with-at-least-32-random-characters"
export EGGW_ALLOWED_ORIGINS="http://localhost:3000"
hypercorn eggw.main:app --bind 127.0.0.1:8000
```

Frontend, in another shell:

```bash
cd /path/to/egg-mono/eggw/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev -- -H 127.0.0.1 -p 3000
```

Then open `http://localhost:3000`. Hypercorn is preferred because HTTP/2 avoids
low per-origin connection limits when several thread streams are open.

## Security and network configuration

`eggw.sh` is loopback-only by default. It generates a fresh high-entropy API
token when none is supplied and passes it to the local frontend through a
same-origin, no-store runtime bootstrap. The token is not printed or compiled
into public JavaScript.

The backend centrally protects every REST, SSE, and WebSocket endpoint except
`/health`. Browser origins must match the configured exact allowlist; wildcards
are rejected. Non-browser clients may omit `Origin` but still need a bearer
token. WebSockets may carry the token through the authenticated subprotocol.

Important variables:

| Variable | Meaning |
| --- | --- |
| `EGGW_API_TOKEN` | API capability; at least 32 non-whitespace characters. Generated only for loopback launcher mode. |
| `EGGW_ALLOWED_ORIGINS` | Comma-separated exact `http://` or `https://` browser origins. |
| `EGGW_BIND_HOST` | Backend listener; defaults to `127.0.0.1`. |
| `EGGW_FRONTEND_BIND_HOST` | Frontend listener; defaults to `127.0.0.1`. |
| `EGGW_BACKEND_PORT` / `EGGW_FRONTEND_PORT` | Starting ports; the launcher finds available ports. |
| `NEXT_PUBLIC_API_URL` | Browser-facing backend origin. Required as explicit HTTPS in public launcher mode. |
| `EGGW_PUBLIC=1` | Explicit acknowledgement required for non-loopback listeners. |

Public mode requires an operator-provided token, explicit allowed origins, and
an explicit HTTPS browser-facing API URL. Put TLS and normal network access
controls in front of EggW. A bearer token is not a substitute for encrypted
transport.

The loopback bootstrap assumes a trusted local host. Other processes or OS users
on that host are outside browser-origin isolation. Keep `.env`, auth tokens, and
project `.egg/` data private.

## Thread and transcript behavior

The browser tree uses deterministic creation-time ordering; the terminal
`/threads` view separately orders roots by last modification with the newest at
the bottom. Selecting a thread loads a bounded newest transcript window;
scrolling up reveals already-loaded records and then requests older pages. New
records append without discarding mounted history. Auto-follow remains attached
only while the reader is at the latest content.

SSE is the canonical live feed and supports cursor-based reconnect. Connection
state (`connecting`, `connected`, `reconnecting`) is distinct from lease-backed
thread run state. Durable message envelopes are installed before dependent tool
lifecycle frames, and live tool cards remain keyed by `tool_call_id` until
canonical transcript records cover them.

Display verbosity is monotonic:

- `max`: full reasoning and tool detail;
- `medium`: conversation visible, internals collapsed but inspectable;
- `min`: conversation plus compact historical execution summaries.

Active tool arguments/output remain visible in every mode. Compaction markers
change provider context without hiding earlier browser history.

## Composer, editing, and files

The composer shares Egg's command catalog and autocomplete sources. It supports
multiline drafts, input-history traversal, staged attachments, image generation,
and `$`/`$$` command prefixes. Drafts and staged items are owned per thread so
navigation does not leak state between conversations.

`/editAnswer` and related flows open a Monaco editor modal. File-edit requests
use opaque backend handles rather than exposing arbitrary browser filesystem
paths. Attachment and artifact routes enforce thread ownership; provider-output
artifacts must be explicitly promoted before reuse as model input.

## Configuration

The backend resolves model files in this order:

1. explicit `EGG_MODELS_PATH`, `EGG_ALL_MODELS_PATH`, and
   `EGG_IMAGE_GENERATION_MODELS_PATH`;
2. matching files in the target project;
3. bundled [`eggconfig`](../eggconfig/README.md) data.

Provider keys, local endpoints, and web backends are documented in
[`dot.env.example`](../dot.env.example). The launcher loads `.env` from the
caller project first, then the monorepo root.

Other launcher controls include `EGGW_SKIP_DEPENDENCY_INSTALL`,
`EGGW_SKIP_FRONTEND_WARMUP`, startup/warmup timeouts, and executable overrides
for Hypercorn and npm. These are primarily useful for pre-provisioned or test
environments.

## API overview

The FastAPI backend provides:

- thread CRUD, roots, children, duplicate, state, and rename;
- paginated messages, input history, send/open/interrupt operations;
- command execution and answer-edit preparation;
- tool listing and approval;
- model and image-model listing/selection;
- sandbox, session, general settings, and auto-approval;
- token/cost/status statistics;
- attachment upload/read, provider artifacts, promotion, and image generation;
- SSE (`/api/threads/{id}/events`) and WebSocket (`/ws/{id}`) feeds;
- OAuth status/login/logout and `/health`.

Interactive API documentation is available from FastAPI when the backend is
running and authenticated.

## Development

Backend tests:

```bash
pip install -e ./eggw
PYTHONPATH=eggw:eggconfig:eggthreads:eggllm pytest -q eggw/tests
```

Frontend checks:

```bash
cd eggw/frontend
npm ci
npm run test:unit
npx tsc --noEmit --pretty false
npm run build
npx playwright install chromium   # once
npm test
```

Focused transcript profiling and regression scripts are declared in
[`frontend/package.json`](frontend/package.json). End-to-end tests start
isolated servers and must not reuse a development database.

Related documentation:

- [Root setup and architecture](../README.md)
- [eggthreads runtime](../eggthreads/README.md)
- [eggllm provider layer](../eggllm/README.md)
