from __future__ import annotations

import os

from .base import WebBackend, WebBackendError
from .fetch import DirectHttpFetchProvider, FetchOrchestrator
from .search import SearchOrchestrator


DEFAULT_BACKEND = "auto"


def _chosen_backend(name: str | None = None) -> str:
    return (name or os.environ.get("EGG_WEB_BACKEND") or DEFAULT_BACKEND).strip().lower()


def get_backend(name: str | None = None) -> WebBackend:
    """Resolve a single WebBackend implementation by name.

    New tool paths should prefer ``get_search_orchestrator()`` or
    ``get_fetch_orchestrator()``.  This remains for legacy imports/tests that
    still expect one combined backend object.
    """
    chosen = _chosen_backend(name)
    if chosen == "auto":
        chosen = "searxng"
    if chosen in ("searxng", "searx"):
        from .searxng import SearxngBackend
        return SearxngBackend()
    if chosen == "tavily":
        from .tavily import TavilyBackend
        return TavilyBackend()
    raise WebBackendError(
        f"Unknown EGG_WEB_BACKEND={chosen!r}. Valid values: auto, searxng, tavily."
    )


def get_search_orchestrator(name: str | None = None) -> SearchOrchestrator:
    """Resolve the search provider chain.

    ``auto`` tries configured hosted providers first, then SearXNG.  Explicit
    ``searxng`` or ``tavily`` remains pinned/deterministic.
    """
    chosen = _chosen_backend(name)
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
    raise WebBackendError(
        f"Unknown EGG_WEB_BACKEND={chosen!r}. Valid values: auto, searxng, tavily."
    )


def get_fetch_orchestrator(name: str | None = None) -> FetchOrchestrator:
    """Resolve the fetch provider chain.

    ``auto`` tries configured hosted extractors first, then direct HTTP.  The
    historical ``searxng`` backend name remains accepted for fetch by mapping to
    the direct HTTP behavior it already used internally.
    """
    chosen = _chosen_backend(name)
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
    raise WebBackendError(
        f"Unknown EGG_WEB_BACKEND={chosen!r}. Valid values: auto, searxng, tavily."
    )
