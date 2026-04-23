# SearXNG for egg-mono

Self-hosted metasearch engine that backs the `web_search` and `fetch_url`
tools by default (no API keys, no per-call cost).

## Start

From inside the egg TUI, just run:

```
/startSearxng
```

Or from a shell, in this directory:

```bash
cd eggthreads/eggthreads/web/searxng
docker-compose up -d        # or: docker compose up -d
```

SearXNG listens on `http://localhost:8888` (bound to loopback).

## Verify

```bash
curl -s 'http://localhost:8888/search?q=ping&format=json' | head
```

Should return JSON. If you get HTML instead, the `json` format is not
enabled in `settings.yml` — check that `search.formats` contains `json`.

## Stop

From inside the egg TUI: `/stopSearxng`. From a shell:

```bash
docker-compose down
```

## Use from egg-mono

The eggthreads default backend is already `searxng`, so:

```bash
# Optional overrides:
export EGG_WEB_BACKEND=searxng              # default
export SEARXNG_URL=http://localhost:8888    # default
```

To swap back to Tavily for a session:

```bash
export EGG_WEB_BACKEND=tavily
export TAVILY_API_KEY=tvly-...
```

## Engine policy

`settings.yml` ships with **Google, Bing, and Yahoo disabled**. These
engines aggressively fight scraping and will serve CAPTCHAs to your
shared public IP — which would then also hit your personal browser
when you search there. The enabled set (DuckDuckGo, Brave, Qwant,
Startpage, Mojeek + specialty engines like Wikipedia, GitHub,
Stack Overflow, arXiv, Hacker News, Reddit) either has official APIs,
is privacy-positioned, or is open by charter.

If you want Google back (e.g. for a one-off, on a dedicated VPS where
your browser is on a different IP), edit `settings.yml` and set
`disabled: false` on the relevant engine entries.

## Notes

- The `secret_key` in `settings.yml` is a throwaway value committed for
  local development. Rotate it for any internet-exposed deployment.
- The `limiter` is enabled as a circuit breaker: if an agent loop fires
  a burst of queries, SearXNG returns HTTP 429 before the burst reaches
  upstream engines and trips their CAPTCHAs on your shared public IP.
  Per-request overhead is ~1–3 ms. Requires the Valkey sidecar
  container (`egg-searxng-valkey`); `/startSearxng` starts both.
