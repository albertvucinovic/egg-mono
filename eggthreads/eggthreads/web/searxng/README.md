# SearXNG for Egg

This directory contains the local SearXNG compose setup used by Egg's
`web_search` tool. It provides no-key, local metasearch and is the search
fallback in the default `auto` provider chain.

`fetch_url` does not fetch through SearXNG. In default `auto` mode it tries
Tavily Extract when `TAVILY_API_KEY` is configured, then falls back to direct
HTTP with local extraction and content-quality checks. No browser-based
search/fetch provider is active in the default chain.

## Start

From the Egg terminal UI:

```text
/startSearxng
```

Or from a shell:

```bash
cd eggthreads/eggthreads/web/searxng
docker compose up -d      # docker-compose also works on older installs
```

The service binds to loopback at `http://localhost:8888`.

## Verify

```bash
curl -s 'http://localhost:8888/search?q=ping&format=json' | head
```

If you get HTML rather than JSON, check that `settings.yml` includes `json` in
`search.formats`.

## Use from Egg

By default, search runs in `auto` mode:

- with `TAVILY_API_KEY`, `web_search` tries Tavily Search first and falls back
  to SearXNG;
- without `TAVILY_API_KEY`, `web_search` uses SearXNG as the only search
  provider.

To pin search to local/no-key SearXNG for a session:

```bash
export EGG_WEB_SEARCH_BACKEND=searxng
export SEARXNG_URL=http://localhost:8888
```

To pin all web tools to a single backend for compatibility/debugging, use the
global selector:

```bash
export EGG_WEB_BACKEND=searxng   # search = SearXNG; fetch = direct HTTP compatibility
```

To use Tavily for search/fetch when configured:

```bash
export TAVILY_API_KEY=tvly-...
# Optional pins; auto mode also uses Tavily first when the key is set.
export EGG_WEB_SEARCH_BACKEND=tavily
export EGG_WEB_FETCH_BACKEND=tavily
# Optional explicit ordered chains; no browser providers are active.
export EGG_WEB_SEARCH_CHAIN=tavily,searxng
export EGG_WEB_FETCH_CHAIN=tavily,direct_http
```

Valid backend selector values are `auto`, `searxng`/`searx`, and `tavily`.
Chain variables (`EGG_WEB_SEARCH_CHAIN`, `EGG_WEB_FETCH_CHAIN`) override split
selectors for their own tool only. Split selectors (`EGG_WEB_SEARCH_BACKEND`,
`EGG_WEB_FETCH_BACKEND`) override `EGG_WEB_BACKEND`. Chain values are
comma-separated current provider names; fetch chains also accept `direct_http`
(`searxng` maps to direct HTTP compatibility for fetch).

## Stop

From Egg:

```text
/stopSearxng
```

From a shell:

```bash
cd eggthreads/eggthreads/web/searxng
docker compose down
```

## Engine policy

`settings.yml` disables Google, Bing, and Yahoo by default. Those engines often
serve CAPTCHAs to shared/public IPs, which can also affect your personal browser
on the same network. The enabled default set favors engines that are
privacy-positioned, open, or less hostile to automated local use.

If you need a disabled engine for a dedicated environment, edit `settings.yml`
and set `disabled: false` for that engine.

## Security notes

- The committed `secret_key` is for local development only. Rotate it before any
  internet-exposed deployment.
- The compose file binds to loopback; do not expose it publicly without reviewing
  SearXNG security/rate-limit settings.
- The limiter uses the Valkey sidecar (`egg-searxng-valkey`). `/startSearxng`
  starts both containers.
