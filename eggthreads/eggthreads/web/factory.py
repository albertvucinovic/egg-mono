from __future__ import annotations

import os

from .base import WebBackend, WebBackendError
from .fetch import DirectHttpFetchProvider, FetchOrchestrator
from .search import SearchOrchestrator


DEFAULT_BACKEND = "auto"
VALID_BACKENDS = "auto, searxng, tavily"
GLOBAL_BACKEND_ENV = "EGG_WEB_BACKEND"
SEARCH_BACKEND_ENV = "EGG_WEB_SEARCH_BACKEND"
FETCH_BACKEND_ENV = "EGG_WEB_FETCH_BACKEND"


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
    ``searxng`` or ``tavily`` remains pinned/deterministic.
    """
    chosen, source = _chosen_backend(name, split_env=SEARCH_BACKEND_ENV)
    if chosen in ("searxng", "searx"):
        from .searxng import SearxngBackend
        return SearchOrchestrator([SearxngBackend()])
    if chosen == "tavily":
        from .tavily import TavilyBackend
        return SearchOrchestrator([TavilyBackend()])
    if chosen == "auto":
        providers = []
        if os.environ.get("TAVILY_API_KEY"):
            from .tavily import TavilyBackend
            providers.append(TavilyBackend())
        from .searxng import SearxngBackend
        providers.append(SearxngBackend())
        return SearchOrchestrator(providers)
    raise _unknown_backend_error(chosen, source)


def get_fetch_orchestrator(name: str | None = None) -> FetchOrchestrator:
    """Resolve the fetch provider chain.

    ``auto`` tries configured hosted extractors first, then direct HTTP.  The
    historical ``searxng`` backend name remains accepted for fetch by mapping to
    the direct HTTP behavior it already used internally.
    """
    chosen, source = _chosen_backend(name, split_env=FETCH_BACKEND_ENV)
    if chosen in ("searxng", "searx"):
        return FetchOrchestrator([DirectHttpFetchProvider()])
    if chosen == "tavily":
        from .tavily import TavilyBackend
        return FetchOrchestrator([TavilyBackend()])
    if chosen == "auto":
        providers = []
        if os.environ.get("TAVILY_API_KEY"):
            from .tavily import TavilyBackend
            providers.append(TavilyBackend())
        providers.append(DirectHttpFetchProvider())
        return FetchOrchestrator(providers)
    raise _unknown_backend_error(chosen, source)
