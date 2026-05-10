# SearXNG for Egg

This directory contains the local SearXNG compose setup used by Egg's web-search
tools. It provides no-key, local metasearch for `web_search` and readable page
fetching workflows.

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

Defaults normally point at this service:

```bash
export EGG_WEB_BACKEND=searxng
export SEARXNG_URL=http://localhost:8888
```

To use Tavily instead for a session:

```bash
export EGG_WEB_BACKEND=tavily
export TAVILY_API_KEY=tvly-...
```

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
