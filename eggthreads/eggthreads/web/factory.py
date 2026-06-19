from __future__ import annotations

import os

from .base import WebBackend, WebBackendError
from .search import SearchOrchestrator


DEFAULT_BACKEND = "auto"


def _chosen_backend(name: str | None = None) -> str:
    return (name or os.environ.get("EGG_WEB_BACKEND") or DEFAULT_BACKEND).strip().lower()


def get_backend(name: str | None = None) -> WebBackend:
    """Resolve a single WebBackend implementation by name.

    ``auto`` is intentionally handled by search orchestration for
    ``web_search``.  This compatibility shim keeps fetch_url pinned to the
    existing SearXNG/direct HTTP behavior until FetchOrchestrator work lands.
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
