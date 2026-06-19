from __future__ import annotations

import os

from .base import WebBackend, WebBackendError
from .fetch import (
    DEFAULT_FETCH_CACHE_MAX_CHARS,
    DEFAULT_FETCH_CACHE_MAX_ENTRIES,
    DEFAULT_FETCH_CACHE_TTL_SEC,
    DirectHttpFetchProvider,
    FetchOrchestrator,
)
from .search import (
    DEFAULT_DEGRADED_EMPTY_SEARCH_CACHE_TTL_SEC,
    DEFAULT_SEARCH_CACHE_MAX_ENTRIES,
    DEFAULT_SEARCH_CACHE_TTL_SEC,
    SearchOrchestrator,
)


DEFAULT_BACKEND = "auto"
VALID_BACKENDS = "auto, searxng, tavily"
VALID_SEARCH_CHAIN_PROVIDERS = "searxng, searx, tavily"
VALID_FETCH_CHAIN_PROVIDERS = "searxng, searx, tavily, direct_http"
GLOBAL_BACKEND_ENV = "EGG_WEB_BACKEND"
SEARCH_BACKEND_ENV = "EGG_WEB_SEARCH_BACKEND"
FETCH_BACKEND_ENV = "EGG_WEB_FETCH_BACKEND"
SEARCH_CHAIN_ENV = "EGG_WEB_SEARCH_CHAIN"
FETCH_CHAIN_ENV = "EGG_WEB_FETCH_CHAIN"
SEARCH_CACHE_TTL_ENV = "EGG_WEB_SEARCH_CACHE_TTL_SEC"
SEARCH_CACHE_DEGRADED_EMPTY_TTL_ENV = "EGG_WEB_SEARCH_CACHE_DEGRADED_EMPTY_TTL_SEC"
SEARCH_CACHE_MAX_ENTRIES_ENV = "EGG_WEB_SEARCH_CACHE_MAX_ENTRIES"
FETCH_CACHE_TTL_ENV = "EGG_WEB_FETCH_CACHE_TTL_SEC"
FETCH_CACHE_MAX_ENTRIES_ENV = "EGG_WEB_FETCH_CACHE_MAX_ENTRIES"
FETCH_CACHE_MAX_CHARS_ENV = "EGG_WEB_FETCH_CACHE_MAX_CHARS"


def _chosen_backend(
    name: str | None = None,
    *,
    split_env: str | None = None,
) -> tuple[str, str]:
    if name is not None:
        return name.strip().lower(), "backend"
    if split_env:
        raw = os.environ.get(split_env)
        if raw is not None and raw.strip():
            return raw.strip().lower(), split_env
    raw = os.environ.get(GLOBAL_BACKEND_ENV)
    if raw is not None and raw.strip():
        return raw.strip().lower(), GLOBAL_BACKEND_ENV
    return DEFAULT_BACKEND, split_env or GLOBAL_BACKEND_ENV


def _unknown_backend_error(chosen: str, source: str) -> WebBackendError:
    return WebBackendError(
        f"Unknown {source}={chosen!r}. Valid values: {VALID_BACKENDS}."
    )


def _unknown_chain_provider_error(
    chosen: str,
    source: str,
    valid_values: str,
) -> WebBackendError:
    return WebBackendError(
        f"Unknown {source} provider {chosen!r}. Valid values: {valid_values}."
    )


def get_backend(name: str | None = None) -> WebBackend:
    """Resolve a single WebBackend implementation by name.

    New tool paths should prefer ``get_search_orchestrator()`` or
    ``get_fetch_orchestrator()``.  This remains for legacy imports/tests that
    still expect one combined backend object.
    """
    chosen, source = _chosen_backend(name)
    if chosen == "auto":
        chosen = "searxng"
    if chosen in ("searxng", "searx"):
        from .searxng import SearxngBackend
        return SearxngBackend()
    if chosen == "tavily":
        from .tavily import TavilyBackend
        return TavilyBackend()
    raise _unknown_backend_error(chosen, source)


def get_search_orchestrator(name: str | None = None) -> SearchOrchestrator:
    """Resolve the search provider chain.

    ``auto`` tries configured hosted providers first, then SearXNG.  Explicit
    ``searxng`` or ``tavily`` remains pinned/deterministic.  If no explicit
    name is passed, ``EGG_WEB_SEARCH_CHAIN`` can provide a comma-separated
    ordered chain and takes precedence over backend selector env vars.
    """
    chain = _chosen_chain(name, SEARCH_CHAIN_ENV)
    if chain is not None:
        return _search_orchestrator(_search_providers_from_chain(chain, SEARCH_CHAIN_ENV))

    chosen, source = _chosen_backend(name, split_env=SEARCH_BACKEND_ENV)
    return _search_orchestrator(_search_providers_for_backend(chosen, source))


def _chosen_chain(name: str | None, chain_env: str) -> str | None:
    if name is not None:
        return None
    raw = os.environ.get(chain_env)
    if raw is not None and raw.strip():
        return raw.strip()
    return None


def _chain_parts(value: str, source: str, valid_values: str) -> list[str]:
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not parts:
        raise WebBackendError(f"Empty {source}. Valid values: {valid_values}.")
    return parts


def _search_providers_from_chain(value: str, source: str) -> list:
    return [
        _search_provider_from_name(provider, source)
        for provider in _chain_parts(value, source, VALID_SEARCH_CHAIN_PROVIDERS)
    ]


def _search_providers_for_backend(chosen: str, source: str) -> list:
    if chosen == "auto":
        providers = []
        if os.environ.get("TAVILY_API_KEY"):
            providers.append(_search_provider_from_name("tavily", source))
        providers.append(_search_provider_from_name("searxng", source))
        return providers
    if chosen in ("searxng", "searx", "tavily"):
        return [_search_provider_from_name(chosen, source)]
    raise _unknown_backend_error(chosen, source)


def _search_provider_from_name(chosen: str, source: str):
    if chosen in ("searxng", "searx"):
        from .searxng import SearxngBackend

        return SearxngBackend()
    if chosen == "tavily":
        from .tavily import TavilyBackend

        return TavilyBackend()
    raise _unknown_chain_provider_error(chosen, source, VALID_SEARCH_CHAIN_PROVIDERS)


def _search_orchestrator(providers: list) -> SearchOrchestrator:
    return SearchOrchestrator(
        providers,
        cache_ttl_sec=_env_float(SEARCH_CACHE_TTL_ENV, DEFAULT_SEARCH_CACHE_TTL_SEC),
        degraded_empty_cache_ttl_sec=_env_float(
            SEARCH_CACHE_DEGRADED_EMPTY_TTL_ENV,
            DEFAULT_DEGRADED_EMPTY_SEARCH_CACHE_TTL_SEC,
        ),
        cache_max_entries=_env_int(SEARCH_CACHE_MAX_ENTRIES_ENV, DEFAULT_SEARCH_CACHE_MAX_ENTRIES),
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def get_fetch_orchestrator(name: str | None = None) -> FetchOrchestrator:
    """Resolve the fetch provider chain.

    ``auto`` tries configured hosted extractors first, then direct HTTP.  The
    historical ``searxng`` backend name remains accepted for fetch by mapping to
    the direct HTTP behavior it already used internally.  If no explicit name is
    passed, ``EGG_WEB_FETCH_CHAIN`` can provide a comma-separated ordered chain
    and takes precedence over backend selector env vars.
    """
    chain = _chosen_chain(name, FETCH_CHAIN_ENV)
    if chain is not None:
        return _fetch_orchestrator(_fetch_providers_from_chain(chain, FETCH_CHAIN_ENV))

    chosen, source = _chosen_backend(name, split_env=FETCH_BACKEND_ENV)
    return _fetch_orchestrator(_fetch_providers_for_backend(chosen, source))


def _fetch_providers_from_chain(value: str, source: str) -> list:
    return [
        _fetch_provider_from_name(provider, source)
        for provider in _chain_parts(value, source, VALID_FETCH_CHAIN_PROVIDERS)
    ]


def _fetch_providers_for_backend(chosen: str, source: str) -> list:
    if chosen == "auto":
        providers = []
        if os.environ.get("TAVILY_API_KEY"):
            providers.append(_fetch_provider_from_name("tavily", source))
        providers.append(_fetch_provider_from_name("direct_http", source))
        return providers
    if chosen in ("searxng", "searx", "tavily"):
        return [_fetch_provider_from_name(chosen, source)]
    raise _unknown_backend_error(chosen, source)


def _fetch_provider_from_name(chosen: str, source: str):
    if chosen in ("searxng", "searx", "direct_http"):
        return DirectHttpFetchProvider()
    if chosen == "tavily":
        from .tavily import TavilyBackend

        return TavilyBackend()
    raise _unknown_chain_provider_error(chosen, source, VALID_FETCH_CHAIN_PROVIDERS)


def _fetch_orchestrator(providers: list) -> FetchOrchestrator:
    return FetchOrchestrator(
        providers,
        cache_ttl_sec=_env_float(FETCH_CACHE_TTL_ENV, DEFAULT_FETCH_CACHE_TTL_SEC),
        cache_max_entries=_env_int(FETCH_CACHE_MAX_ENTRIES_ENV, DEFAULT_FETCH_CACHE_MAX_ENTRIES),
        cache_max_chars=_env_int(FETCH_CACHE_MAX_CHARS_ENV, DEFAULT_FETCH_CACHE_MAX_CHARS),
    )
