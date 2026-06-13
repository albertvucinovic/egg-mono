# Search and fetch observations

## Current Egg behavior

- `web_search` defaults to SearXNG when `EGG_WEB_BACKEND` is unset.
- `fetch_url` is routed through the same selected web backend, even though search and page retrieval have different reliability concerns.
- SearXNG can return HTTP 200 JSON with an empty `results` array while also reporting upstream engine failures in `unresponsive_engines`.
- Egg currently collapses that case to plain `No results.`, losing the important distinction between a true empty result set and backend degradation.

## Observed SearXNG degradation

Local SearXNG was reachable and the local limiter passlisted Egg requests, but upstream engines frequently failed:

- DuckDuckGo: CAPTCHA
- Startpage: CAPTCHA
- Brave: too many requests
- Qwant: access denied
- Mojeek / Karmasearch: HTTP 403 / access denied

This means many `No results.` responses are likely not true search misses; they are degraded SearXNG runs with no usable upstream engines.

## OpenCode comparison

OpenCode separates discovery from retrieval:

- `websearch` is hosted/provider-backed, not browser-backed or SearXNG-backed.
  - It uses Exa MCP and/or Parallel MCP.
  - It exposes search options like result count, live crawl mode, fast/deep search type, and context character limit.
- `webfetch` is direct HTTP fetch, not headless browser by default.
  - It uses browser-like headers, bounded timeout, max response size, content-type handling, HTML-to-markdown conversion, image attachments, and an honest-UA retry for some Cloudflare challenge cases.

The main lesson is that reliable search comes from a search/crawl provider, while fetch can start with direct HTTP and only escalate when needed.

## Recommended Egg architecture

### Search

Introduce a `SearchOrchestrator` instead of binding `web_search` to one backend:

1. Provider chain:
   - API-backed provider first when configured, e.g. Tavily, Exa, Parallel, Brave Search API, Kagi, etc.
   - SearXNG as local/no-key fallback.
2. Structured provider response:
   - results
   - provider name
   - diagnostics
   - degraded/retriable flags
3. Fallback behavior:
   - fall back when a provider fails or returns empty results with clear backend degradation.
   - dedupe URLs across providers.
4. User/LLM-facing diagnostics:
   - distinguish true `No matching results found` from `Search backend degraded`.
5. Cache normalized query + provider + options.

### Fetch

Introduce a separate `FetchOrchestrator`:

1. Direct HTTP fetch first.
2. Local extraction by content type:
   - HTML: trafilatura/readability/turndown-style markdown conversion.
   - plain text/markdown: return directly.
   - JSON: bounded pretty representation.
   - PDF: optional text extraction.
3. Detect empty extraction, bot-block pages, and JS-heavy pages.
4. Optional fallbacks:
   - hosted extractor such as Tavily Extract / Firecrawl / Jina Reader.
   - browser-rendered fetch via Playwright when enabled.
5. Cache fetched/extracted content by URL/final URL/validators where available.

## Browser-backed tools

Browser-backed fetch is useful as a fallback for pages that require JavaScript rendering or block direct HTTP, but it should not be the default for every fetch because it is slower, heavier, harder to secure, and still not immune to bot detection.

Browser-backed general search should not be the primary design. Scraping Google/DDG/Bing result pages with Playwright is fragile, slower, CAPTCHA-prone, and less structured than using a search API/provider. Browser automation is better reserved for specific page retrieval, authenticated/internal pages, or site-specific search UIs.

## Practical implementation order

1. Surface SearXNG `unresponsive_engines` diagnostics instead of plain `No results.`.
2. Add `EGG_WEB_BACKEND=auto` for provider fallback, using Tavily first when `TAVILY_API_KEY` exists and SearXNG as fallback.
3. Split search and fetch backend selection.
4. Upgrade `fetch_url` to OpenCode-like direct HTTP behavior: format option, better Accept headers, max response size, and honest-UA retry.
5. Add optional browser-rendered fetch with strict timeout, size, and permission boundaries.
