from __future__ import annotations

import copy
import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, List

from .base import SearchAttempt, SearchProvider, SearchResponse, SearchResult, WebBackendError


SEARCH_CACHE_VERSION = "search-cache-v1"
DEFAULT_SEARCH_CACHE_TTL_SEC = 300.0
DEFAULT_DEGRADED_EMPTY_SEARCH_CACHE_TTL_SEC = 10.0
DEFAULT_SEARCH_CACHE_MAX_ENTRIES = 128


@dataclass
class _SearchCacheEntry:
    expires_at: float
    response: SearchResponse


_SEARCH_CACHE: OrderedDict[tuple[Any, ...], _SearchCacheEntry] = OrderedDict()


class SearchOrchestrator:
    """Run an ordered search provider fallback chain."""

    def __init__(
        self,
        providers: Iterable[SearchProvider],
        *,
        cache_ttl_sec: float | None = None,
        degraded_empty_cache_ttl_sec: float | None = None,
        cache_max_entries: int | None = None,
        cache_enabled: bool = True,
    ):
        self.providers = list(providers)
        if not self.providers:
            raise WebBackendError("No search providers configured.", provider="search")
        self.cache_ttl_sec = _coerce_float(cache_ttl_sec, DEFAULT_SEARCH_CACHE_TTL_SEC)
        self.degraded_empty_cache_ttl_sec = _coerce_float(
            degraded_empty_cache_ttl_sec,
            DEFAULT_DEGRADED_EMPTY_SEARCH_CACHE_TTL_SEC,
        )
        self.cache_max_entries = _coerce_int(cache_max_entries, DEFAULT_SEARCH_CACHE_MAX_ENTRIES)
        self.cache_enabled = cache_enabled

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
        cache_key = self._cache_key(query, max_results)
        if self.cache_enabled:
            cached = _search_cache_get(cache_key)
            if cached is not None:
                return cached

        response = self._search_response_uncached(query, max_results=max_results)
        if self.cache_enabled:
            ttl = self._cache_ttl_for(response)
            if ttl > 0 and self.cache_max_entries > 0:
                _search_cache_put(cache_key, response, ttl, self.cache_max_entries)
        return response

    def _search_response_uncached(self, query: str, max_results: int = 5) -> SearchResponse:
        collected: List[SearchResult] = []
        attempts: List[SearchAttempt] = []
        seen_urls: set[str] = set()

        for provider in self.providers:
            provider_name = getattr(provider, "name", provider.__class__.__name__)
            try:
                response = provider.search_response(query, max_results=max_results)
            except WebBackendError as e:
                attempts.append(_attempt_from_error(provider_name, e))
                if e.retriable:
                    continue
                if not collected and len(self.providers) == 1:
                    raise
                break

            attempts.extend(response.attempts)
            for result in response.results:
                url = (result.url or "").strip()
                dedupe_key = url.lower()
                if dedupe_key and dedupe_key in seen_urls:
                    continue
                if dedupe_key:
                    seen_urls.add(dedupe_key)
                collected.append(result)
                if len(collected) >= max_results:
                    break

            if len(collected) >= max_results:
                break

        return SearchResponse(results=collected, attempts=attempts)

    def _cache_key(self, query: str, max_results: int) -> tuple[Any, ...]:
        return (
            SEARCH_CACHE_VERSION,
            tuple(_provider_cache_identity(provider) for provider in self.providers),
            _normalize_query(query),
            int(max_results),
        )

    def _cache_ttl_for(self, response: SearchResponse) -> float:
        if response.degraded_empty:
            return self.degraded_empty_cache_ttl_sec
        return self.cache_ttl_sec

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return self.search_response(query, max_results=max_results).results


def clear_search_cache() -> None:
    """Clear process-local search cache state for tests/operator reset."""

    _SEARCH_CACHE.clear()


def _search_cache_get(key: tuple[Any, ...]) -> SearchResponse | None:
    now = time.monotonic()
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _SEARCH_CACHE.pop(key, None)
        return None
    _SEARCH_CACHE.move_to_end(key)
    return copy.deepcopy(entry.response)


def _search_cache_put(
    key: tuple[Any, ...],
    response: SearchResponse,
    ttl_sec: float,
    max_entries: int,
) -> None:
    _SEARCH_CACHE[key] = _SearchCacheEntry(
        expires_at=time.monotonic() + ttl_sec,
        response=_response_for_cache(response),
    )
    _SEARCH_CACHE.move_to_end(key)
    while len(_SEARCH_CACHE) > max_entries:
        _SEARCH_CACHE.popitem(last=False)


def _provider_cache_identity(provider: SearchProvider) -> tuple[str, ...]:
    cls = provider.__class__
    parts = [
        str(getattr(provider, "name", cls.__name__)),
        f"{cls.__module__}.{cls.__qualname__}",
    ]
    base_url = getattr(provider, "_base_url", None)
    if base_url:
        parts.append(f"base_url={base_url}")
    search_url = getattr(provider, "SEARCH_URL", None)
    if search_url:
        parts.append(f"search_url={search_url}")
    if hasattr(provider, "_api_key"):
        api_key = str(getattr(provider, "_api_key") or "")
        if api_key:
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
            parts.append(f"api_key_hash={key_hash}")
        else:
            parts.append("api_key=missing")
    return tuple(parts)


def _response_for_cache(response: SearchResponse) -> SearchResponse:
    return SearchResponse(
        results=[
            SearchResult(
                title=_bound_text(result.title, limit=500),
                url=_bound_text(result.url, limit=1000),
                snippet=_bound_text(result.snippet, limit=1000),
            )
            for result in response.results
        ],
        attempts=[
            SearchAttempt(
                provider=_bound_text(attempt.provider, limit=80),
                success=attempt.success,
                degraded=attempt.degraded,
                retriable=attempt.retriable,
                message=_bound_text(attempt.message, limit=500),
                diagnostics=_bound_diagnostics(attempt.diagnostics),
            )
            for attempt in response.attempts[:8]
        ],
    )


def _bound_diagnostics(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return _bound_text(value, limit=200)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 20:
                out["…"] = "truncated"
                break
            out[_bound_text(key, limit=80)] = _bound_diagnostics(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_bound_diagnostics(item, depth=depth + 1) for item in list(value)[:20]]
    if isinstance(value, (str, bytes)):
        return _bound_text(value, limit=500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bound_text(value, limit=200)


def _bound_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").strip().lower().split())


def _coerce_float(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _coerce_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _attempt_from_error(provider_name: str, error: WebBackendError) -> SearchAttempt:
    return SearchAttempt(
        provider=error.provider or provider_name,
        success=False,
        degraded=True,
        retriable=error.retriable,
        message=str(error),
        diagnostics=error.diagnostics,
    )
