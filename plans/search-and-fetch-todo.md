# Search and fetch todo

## Reanalyzed conclusions

1. **Default behavior should be provider fallback, not a single selected backend.**
   `web_search` should default to an `auto` provider chain. A configured hosted
   search provider should be tried first, with local SearXNG as the no-key/local
   fallback. Explicit backend pinning can remain available for debugging and
   deterministic tests, but normal Egg usage should not collapse onto one brittle
   provider.
2. **Search and fetch need separate orchestration.** Search reliability and page
   retrieval reliability fail in different ways. Keep the user-facing tools
   (`web_search`, `fetch_url`) stable, but split the internals into a
   `SearchOrchestrator` and a `FetchOrchestrator` instead of routing both through
   one combined `WebBackend`.
3. **SearXNG empty results are not always true empty results.** If SearXNG returns
   `results: []` plus `unresponsive_engines`, Egg should treat that attempt as
   degraded/retriable and continue the fallback chain. The final tool output must
   distinguish a real miss from backend degradation.
4. **Fetch should be a fallback chain too, with hosted extractors first when
   configured.** Fetching a known URL is not metasearch, and direct HTTP is often
   blocked or returns placeholder/challenge HTML. In `auto`, prefer configured
   non-browser hosted extractors such as Tavily Extract first, then fall back to
   direct HTTP as the local/no-key option.
5. **Blocked/challenge/placeholder HTML is a fetch failure for fallback purposes.**
   Direct HTTP responses that extract to login/challenge/enable-JS/blocked
   placeholders should be marked degraded/retriable rather than returned as if
   they were useful page content.
6. **No browser-based links in the active chain for now.** Do not wire Playwright,
   browser-rendered fetch, or browser-scraped search into the default or optional
   active fallback chain in this todo. Browser support can be reconsidered later
   as a separate, explicitly enabled project.

## Hierarchical todo

### 0. Product decisions and invariants

- [ ] Adopt `auto` provider fallback as the default behavior when no explicit
      backend is configured. *(Milestone 1 completed this for search; fetch fallback remains Milestone 2.)*
  - [x] Change the effective default from `searxng` to `auto` for search.
  - [x] Preserve explicit search pinning for tests/operators, e.g.
        `EGG_WEB_BACKEND=searxng` or `EGG_WEB_BACKEND=tavily`.
  - [x] Define whether explicit pinning disables fallback; pinned search backends
        are deterministic unless a separate chain variable is added later.
- [ ] Keep the public tool names stable.
  - [ ] `web_search` remains the search tool.
  - [ ] `fetch_url` remains the URL retrieval/extraction tool.
- [ ] Exclude browser-based providers from this implementation.
  - [ ] Do not add Playwright/browser fetch to the chain.
  - [ ] Do not add browser-scraped Google/DDG/Bing search to the chain.
  - [ ] If future browser work is documented, mark it explicitly deferred and
        not part of `auto`.

### 1. Shared provider result model

- [ ] Add structured attempt/result types under `eggthreads/eggthreads/web/`.
      *(Search-side model done in Milestone 1; fetch-side model deferred.)*
  - [ ] `SearchAttempt` / `FetchAttempt` fields:
    - [x] `SearchAttempt`: provider name;
    - [x] `SearchAttempt`: success/failure state;
    - [x] `SearchAttempt`: `degraded` flag;
    - [x] `SearchAttempt`: `retriable` flag;
    - [x] `SearchAttempt`: diagnostic message;
    - [x] `SearchAttempt`: raw provider-specific diagnostics in a bounded/debug-safe shape;
    - [ ] `FetchAttempt`: same structured fields.
  - [x] `SearchResponse` fields:
    - [x] normalized `SearchResult` list;
    - [x] attempts list;
    - [x] final provider(s) used;
    - [x] helper for “true empty” vs “degraded empty”.
  - [ ] `FetchResponse` fields:
    - [ ] final URL;
    - [ ] extracted content;
    - [ ] content type;
    - [ ] attempts list;
    - [ ] helper for empty extraction / blocked page detection.
- [x] Extend errors without losing compatibility for search diagnostics.
  - [x] Keep `WebBackendError` as the base exception.
  - [x] Add optional structured attributes such as provider, retriable,
        degraded, status code, and diagnostics.
  - [x] Ensure stringification remains readable in tool output.

### 2. Search provider chain

- [x] Introduce `SearchProvider` and `SearchOrchestrator`.
  - [x] Move `web_search` onto search orchestration while keeping `WebBackend` as
        a compatibility/fetch shim.
  - [x] Let each provider return a structured `SearchResponse` or throw a
        structured `WebBackendError`.
  - [x] Deduplicate URLs across providers while preserving provider order.
  - [x] Stop when enough non-duplicate results have been collected; diagnostics-
        from-all remains a possible future option.
- [x] Make fallback the default search mode.
  - [x] Default `auto` chain:
    - [x] hosted provider(s) with configured credentials first;
    - [x] SearXNG last as the local/no-key fallback.
  - [x] Initial hosted provider: Tavily when `TAVILY_API_KEY` is present.
  - [x] Leave clear extension points for Exa, Parallel, Brave Search API, Kagi,
        etc., without blocking the first implementation on them.
  - [x] If no hosted provider credentials exist, use SearXNG as the only search
        provider and surface actionable diagnostics if it is unavailable.
- [x] Improve the SearXNG provider.
  - [x] Parse `unresponsive_engines` from the SearXNG JSON response.
  - [x] Treat `results: []` with non-empty `unresponsive_engines` as degraded and
        retriable, not as a true empty result.
  - [x] Include a concise diagnostic summary such as
        `SearXNG degraded: duckduckgo CAPTCHA; brave too many requests`.
  - [x] Keep the existing `/startSearxng` hint for connection failures.
  - [x] Add tests for empty+degraded, empty+not-degraded, partial results with
        degraded engines, non-JSON, HTTP errors, and connection refused.
- [x] Normalize hosted search providers.
  - [x] Keep the existing Tavily search implementation but adapt it to the new
        `SearchProvider` interface.
  - [x] Mark HTTP 429/5xx/network failures as retriable.
  - [x] Treat a clean successful empty result from a provider as a true empty only
        after diagnostics do not indicate provider degradation.
  - [x] Bound snippets and provider diagnostic text.
- [x] Improve search tool output.
  - [x] Continue returning compact markdown bullet results.
  - [x] If all providers cleanly return no results, output `No matching results
        found.`
  - [x] If any provider was degraded and final results are empty, output
        `Search backend degraded; no reliable results returned.` plus concise
        provider diagnostics.
  - [ ] If fallback succeeds after a degraded provider, optionally append a short
        diagnostic note only when useful and not too noisy. *(Deferred/not added
        in Milestone 1 to keep normal successful output quiet.)*

### 3. Fetch provider chain

- [ ] Introduce `FetchProvider` and `FetchOrchestrator`.
  - [ ] Move fetch-only behavior out of the combined `WebBackend` abstraction.
  - [ ] `fetch_url` should call the fetch orchestrator, not the search backend
        factory.
  - [ ] Keep provider attempts inspectable for debugging and tests.
- [ ] Make fetch fallback the default in `auto` mode.
  - [ ] Default configured chain: Tavily Extract first when `TAVILY_API_KEY` is
        present, then direct HTTP as the local/no-key fallback.
  - [ ] Do not include SearXNG as a fetch provider unless a real URL extraction
        API/proxy is added; the current SearXNG fetch code is effectively direct
        HTTP plus local extraction and should be renamed/migrated accordingly.
  - [ ] Leave extension points for other non-browser hosted extractors such as
        Jina Reader or Firecrawl API.
- [ ] Implement direct HTTP as the fallback/no-key fetch provider.
  - [ ] Allow only `http://` and `https://` URLs.
  - [ ] Use browser-like but honest headers:
    - [ ] `User-Agent` from `EGG_WEB_USER_AGENT` or a sensible default;
    - [ ] `Accept` covering HTML, text, markdown, JSON, and PDF where supported;
    - [ ] `Accept-Language` default such as `en-US,en;q=0.9`.
  - [ ] Enforce bounded timeouts.
  - [ ] Enforce a maximum response size before extraction.
  - [ ] Follow redirects and report the final URL.
  - [ ] Preserve useful HTTP diagnostics for 403/429/5xx and network errors.
- [ ] Add content-type aware extraction.
  - [ ] HTML/XHTML:
    - [ ] reuse `html_to_markdown`;
    - [ ] preserve links/tables when available;
    - [ ] detect empty extraction.
  - [ ] Plain text / markdown:
    - [ ] return bounded content directly.
  - [ ] JSON:
    - [ ] return a bounded pretty representation.
  - [ ] PDF:
    - [ ] decide whether first implementation reports unsupported or adds an
          optional text extractor; do not block direct HTTP for HTML/text on PDF.
- [ ] Detect retriable fetch failures.
  - [ ] Mark network errors, timeout, 429, most 5xx, empty extraction, obvious
        bot-block pages, and JS-required placeholders as fallback candidates.
  - [ ] Add a small `FetchQuality` / blocked-page classifier rather than relying
        on HTTP status alone.
    - [ ] Pattern signals: CAPTCHA, hCaptcha/reCAPTCHA/Turnstile, Cloudflare
          `Just a moment` / `Attention Required`, Akamai/PerimeterX/DataDome/
          Incapsula/DDoS-Guard challenges, `enable JavaScript`, `checking your
          browser`, `access denied`, `forbidden`, `unusual traffic`, login walls,
          cookie/consent placeholders, and unsupported-browser placeholders.
    - [ ] Structural signals: very low extracted text, generic title, high
          script/form density, meta refresh, no article/main text, extraction
          much smaller than raw HTML, or final URL paths such as `/login`,
          `/captcha`, `/cdn-cgi/`, `/challenge`, `/verify`.
    - [ ] Header/provider signals: Cloudflare/Akamai/etc. challenge headers,
          suspicious cookies, and content-type mismatches.
    - [ ] Treat the classifier as confidence/scoring, not a perfect allow/deny
          list; uncertain low-quality pages should fall back when another
          provider remains, and return with a warning only if no fallback works.
  - [ ] Treat clean 404/410 as terminal unless a provider-specific reason says
        otherwise.
  - [ ] Include concise diagnostics in the final output when all fetch providers
        fail.
- [ ] Add hosted extractor providers without browser.
  - [ ] Use Tavily Extract as the preferred fetch provider in `auto` when
        `TAVILY_API_KEY` is configured.
  - [ ] Keep the existing Tavily extract code but adapt it to `FetchProvider`.
  - [ ] If Tavily Extract fails, returns empty content, or reports a retriable
        provider error, continue the fetch chain.
  - [ ] Leave extension points for non-browser hosted extractors such as Jina
        Reader or Firecrawl API, but do not require them for the first pass.
- [ ] Keep browser fetch out of scope.
  - [ ] Do not add Playwright or headless browser dependencies.
  - [ ] Do not mention browser fetch as part of `auto` or normal fallback docs.

### 4. Configuration and factory migration

- [ ] Replace the single backend factory with search/fetch resolution helpers.
      *(Search helper done; fetch helper deferred.)*
  - [x] Add `get_search_orchestrator()`.
  - [ ] Add `get_fetch_orchestrator()`.
  - [x] Keep `get_backend()` only as a compatibility shim if needed by existing
        imports/tests during migration.
- [ ] Define environment variables.
  - [x] `EGG_WEB_BACKEND=auto` default for backward-compatible global selection.
  - [x] Existing explicit values still work for search: `searxng`, `tavily`.
  - [ ] Optional future split variables:
    - [ ] `EGG_WEB_SEARCH_BACKEND` or `EGG_WEB_SEARCH_CHAIN`;
    - [ ] `EGG_WEB_FETCH_BACKEND` or `EGG_WEB_FETCH_CHAIN`.
  - [x] `EGG_WEB_MAX_RESULTS` continues to set default search result count.
  - [ ] Add fetch-specific bounds:
    - [ ] `EGG_WEB_FETCH_TIMEOUT_SEC`;
    - [ ] `EGG_WEB_FETCH_MAX_BYTES`;
    - [ ] optional `EGG_WEB_FETCH_MAX_CHARS`.
- [ ] Define chain semantics.
  - [x] `auto`: provider fallback enabled by default for search.
  - [x] explicit single search backend: deterministic provider, no fallback unless
        a chain variable is provided.
  - [ ] explicit chain: ordered comma-separated providers with fallback.
  - [x] unknown provider names produce a clear valid-values error.
- [x] Keep Milestone 1 tests deterministic.
  - [x] Unit tests should pin provider chains explicitly rather than depending on
        developer machine credentials.
  - [x] Tests must not make real network calls.

### 5. Tool schemas and user-facing behavior

- [x] Keep `web_search` schema compatible.
  - [x] Preserve `query` and `max_results`.
  - [x] Keep the cap at `WEB_RESULTS_CAP` unless product requirements change.
- [ ] Consider small `fetch_url` schema extensions.
  - [ ] Keep `url` required.
  - [ ] Preserve existing `timeout` behavior if already exposed by the tool
        layer.
  - [ ] Add optional `format` only if it is implemented end-to-end, e.g.
        `markdown`, `text`, or `raw`.
  - [ ] Add optional `max_chars` only if bounded consistently after extraction.
- [ ] Make diagnostics helpful but not noisy.
  - [x] Normal successful search stays concise.
  - [ ] Normal successful fetch should stay concise.
  - [x] Degraded/no-result cases should explain provider degradation.
  - [x] Error messages should suggest configuration fixes, e.g. set
        `TAVILY_API_KEY` or run `/startSearxng`.

### 6. Caching

- [ ] Add search cache after structured responses exist.
  - [ ] Cache key: normalized query, max results/options, provider/chain version.
  - [ ] Cache value: normalized results plus bounded diagnostics.
  - [ ] Do not cache degraded empty responses for long.
- [ ] Add fetch cache after direct HTTP behavior is stable.
  - [ ] Cache key: URL/final URL plus validators where available.
  - [ ] Respect `ETag` / `Last-Modified` when available.
  - [ ] Cache extracted content, not just raw HTML.
  - [ ] Apply max size and TTL limits.

### 7. Tests

- [x] Search tests.
  - [x] Default mode builds an `auto` fallback chain.
  - [x] Tavily configured first, SearXNG fallback second.
  - [x] Missing Tavily key skips Tavily in `auto` instead of failing the whole
        chain.
  - [x] Tavily retriable failure falls back to SearXNG.
  - [x] SearXNG degraded empty result produces degraded diagnostics.
  - [x] True clean empty result produces `No matching results found.`
  - [x] URL deduplication across providers works.
- [ ] Fetch tests.
  - [ ] `fetch_url` uses Tavily Extract first in `auto` when configured.
  - [ ] `fetch_url` uses direct HTTP as fallback/no-key mode.
  - [ ] HTML goes through markdown extraction.
  - [ ] text/markdown returns directly.
  - [ ] JSON returns bounded pretty output.
  - [ ] 403/429/5xx/timeout/empty extraction/block-placeholder responses are
        treated as degraded/retriable when another fetch provider remains.
  - [ ] clean 404 is terminal.
  - [ ] browser providers are absent from default and explicit valid provider
        lists.
- [ ] Configuration tests.
  - [x] Explicit backend pinning remains deterministic for search.
  - [x] Unknown provider names produce a clear error.
  - [x] Environment defaults do not depend on real credentials in unit tests.
- [ ] Regression tests.
  - [x] Existing `web_search` and `fetch_url` tool specs remain compatible.
  - [ ] Existing `/startSearxng` and `/stopSearxng` workflows still work. *(Not
        retested in Milestone 1; command code left untouched.)*

### 8. Documentation and operator notes

- [ ] Update `eggthreads/API.md` tool descriptions.
  - [ ] `web_search`: provider-fallback search by default.
  - [ ] `fetch_url`: non-browser provider fallback by default, e.g. Tavily
        Extract first when configured, direct HTTP as no-key fallback.
- [ ] Update SearXNG docs.
  - [ ] Describe SearXNG as the local/no-key search fallback.
  - [ ] Explain that upstream engine CAPTCHA/rate-limit degradation can occur.
  - [ ] Keep `/startSearxng` instructions.
- [ ] Update `.env.example` / `dot.env.example`.
  - [ ] Document `EGG_WEB_BACKEND=auto` default.
  - [ ] Document `TAVILY_API_KEY` enabling hosted search and extract fallback.
  - [ ] Document fetch size/timeout bounds.
- [ ] Update command success messages if needed.
  - [ ] Avoid saying SearXNG is used for both `web_search / fetch_url` once fetch
        is split.

### 9. Suggested implementation milestones

- [x] Milestone 1: diagnostics and default search fallback.
  - [x] Add structured search attempts.
  - [x] Parse SearXNG `unresponsive_engines`.
  - [x] Add `SearchOrchestrator` with default `auto` chain.
  - [x] Update `web_search` output for degraded vs true empty.
  - Status note: search-only implementation is complete; `fetch_url` still uses
        the compatibility backend path until Milestone 2 adds `FetchOrchestrator`.
- [ ] Milestone 2: split fetch from search.
  - [ ] Add `FetchOrchestrator`.
  - [ ] Adapt Tavily Extract as the preferred configured non-browser fetch
        provider.
  - [ ] Add direct HTTP as the local/no-key fallback `FetchProvider`.
  - [ ] Detect blocked/challenge/placeholder HTML and continue fallback.
  - [ ] Make `fetch_url` stop depending on SearXNG/search backend selection.
- [ ] Milestone 3: configuration cleanup and compatibility shims.
  - [ ] Finalize env var semantics.
  - [ ] Keep explicit backend behavior stable.
  - [ ] Update docs and examples.
- [ ] Milestone 4: cache and optional additional providers.
  - [ ] Add bounded search/fetch caches.
  - [ ] Add more API-backed search/extract providers as separate PRs/tasks.
  - [ ] Keep browser-based providers deferred outside this todo.
