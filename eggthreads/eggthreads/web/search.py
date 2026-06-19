from __future__ import annotations

from collections.abc import Iterable
from typing import List

from .base import SearchAttempt, SearchProvider, SearchResponse, SearchResult, WebBackendError


class SearchOrchestrator:
    """Run an ordered search provider fallback chain."""

    def __init__(self, providers: Iterable[SearchProvider]):
        self.providers = list(providers)
        if not self.providers:
            raise WebBackendError("No search providers configured.", provider="search")

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
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

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return self.search_response(query, max_results=max_results).results


def _attempt_from_error(provider_name: str, error: WebBackendError) -> SearchAttempt:
    return SearchAttempt(
        provider=error.provider or provider_name,
        success=False,
        degraded=True,
        retriable=error.retriable,
        message=str(error),
        diagnostics=error.diagnostics,
    )
