from __future__ import annotations

import os

from .base import WebBackend, WebBackendError


DEFAULT_BACKEND = "searxng"


def get_backend(name: str | None = None) -> WebBackend:
    """Resolve a WebBackend implementation by name.

    Name precedence:
    1. Explicit ``name`` argument.
    2. ``EGG_WEB_BACKEND`` env var.
    3. ``DEFAULT_BACKEND`` (searxng).
    """
    chosen = (name or os.environ.get("EGG_WEB_BACKEND") or DEFAULT_BACKEND).strip().lower()
    if chosen in ("searxng", "searx"):
        from .searxng import SearxngBackend
        return SearxngBackend()
    if chosen == "tavily":
        from .tavily import TavilyBackend
        return TavilyBackend()
    raise WebBackendError(
        f"Unknown EGG_WEB_BACKEND={chosen!r}. Valid values: searxng, tavily."
    )
