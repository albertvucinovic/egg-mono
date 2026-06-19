from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchAttempt:
    provider: str
    success: bool
    degraded: bool = False
    retriable: bool = False
    message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    results: List[SearchResult] = field(default_factory=list)
    attempts: List[SearchAttempt] = field(default_factory=list)

    @property
    def providers(self) -> List[str]:
        return [attempt.provider for attempt in self.attempts]

    @property
    def degraded(self) -> bool:
        return any(
            attempt.degraded or attempt.retriable or not attempt.success
            for attempt in self.attempts
        )

    @property
    def true_empty(self) -> bool:
        return not self.results and bool(self.attempts) and not self.degraded

    @property
    def degraded_empty(self) -> bool:
        return not self.results and self.degraded

    @property
    def all_attempts_failed(self) -> bool:
        return bool(self.attempts) and all(not attempt.success for attempt in self.attempts)

    def diagnostic_messages(self) -> List[str]:
        messages: List[str] = []
        for attempt in self.attempts:
            if not (attempt.message and (attempt.degraded or not attempt.success)):
                continue
            if attempt.message in messages:
                continue
            messages.append(attempt.message)
        return messages


class WebBackendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retriable: bool = False,
        degraded: bool = False,
        status_code: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retriable = retriable
        self.degraded = degraded
        self.status_code = status_code
        self.diagnostics = diagnostics or {}


class SearchProvider(ABC):
    name: str = "search"

    @abstractmethod
    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
        ...


class WebBackend(SearchProvider, ABC):
    """Strategy interface for web search + fetch.

    Implementations translate between the shared tool surface
    (``web_search`` / ``fetch_url``) and a concrete provider: a hosted
    API like Tavily, a self-hosted metasearch like SearXNG, etc.
    """

    name: str = "web"

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
        results = self.search(query, max_results=max_results)
        return SearchResponse(
            results=results,
            attempts=[
                SearchAttempt(
                    provider=self.name,
                    success=True,
                    message=f"{self.name} returned {len(results)} result(s).",
                )
            ],
        )

    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        ...

    @abstractmethod
    def fetch(self, url: str) -> str:
        """Return readable markdown for ``url`` (never None)."""
        ...
