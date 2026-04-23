from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class WebBackendError(Exception):
    pass


class WebBackend(ABC):
    """Strategy interface for web search + fetch.

    Implementations translate between the shared tool surface
    (``web_search`` / ``fetch_url``) and a concrete provider: a hosted
    API like Tavily, a self-hosted metasearch like SearXNG, etc.
    """

    name: str = "web"

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        ...

    @abstractmethod
    def fetch(self, url: str) -> str:
        """Return readable markdown for ``url`` (never None)."""
        ...
