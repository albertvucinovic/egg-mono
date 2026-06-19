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
        return _diagnostic_messages(self.attempts)


@dataclass
class FetchAttempt:
    provider: str
    success: bool
    degraded: bool = False
    retriable: bool = False
    message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResponse:
    final_url: str
    content: str
    content_type: str = ""
    attempts: List[FetchAttempt] = field(default_factory=list)

    def to_tool_output(self) -> str:
        content = (self.content or "").strip()
        if not content:
            content = "(no content)"
        return f"URL: {self.final_url}\n\n{content}"

    @property
    def degraded(self) -> bool:
        return any(
            attempt.degraded or attempt.retriable or not attempt.success
            for attempt in self.attempts
        )

    def diagnostic_messages(self) -> List[str]:
        return _diagnostic_messages(self.attempts)


def _diagnostic_messages(attempts: List[Any]) -> List[str]:
    messages: List[str] = []
    for attempt in attempts:
        if not (attempt.message and (attempt.degraded or not attempt.success)):
            continue
        if attempt.message in messages:
            continue
        messages.append(attempt.message)
    return messages


def bound_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def bound_diagnostics(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return bound_text(value, limit=200)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 20:
                out["…"] = "truncated"
                break
            out[bound_text(key, limit=80)] = bound_diagnostics(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [bound_diagnostics(item, depth=depth + 1) for item in list(value)[:20]]
    if isinstance(value, (str, bytes)):
        return bound_text(value, limit=500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return bound_text(value, limit=200)


def coerce_nonnegative_float(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def coerce_nonnegative_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


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


class FetchProvider(ABC):
    name: str = "fetch"

    @abstractmethod
    def fetch_response(self, url: str) -> FetchResponse:
        ...


class WebBackend(SearchProvider, FetchProvider, ABC):
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

    def fetch_response(self, url: str) -> FetchResponse:
        text = self.fetch(url)
        final_url = url
        content = text
        if text.startswith("URL: "):
            first, sep, rest = text.partition("\n\n")
            final_url = first.removeprefix("URL: ").strip() or url
            if sep:
                content = rest
        return FetchResponse(
            final_url=final_url,
            content=content,
            attempts=[
                FetchAttempt(
                    provider=self.name,
                    success=True,
                    message=f"{self.name} fetched {final_url}.",
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
