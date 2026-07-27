from __future__ import annotations

import os
from typing import Any, List

from .base import (
    FetchAttempt,
    FetchResponse,
    SearchAttempt,
    SearchResponse,
    SearchResult,
    WebBackend,
    WebBackendError,
    bound_diagnostics,
    bound_text,
)
from .hosted_http import close_response, hosted_http_error


class ParallelBackend(WebBackend):
    """Parallel.ai Search and Extract provider."""

    name = "parallel"

    SEARCH_URL = "https://api.parallel.ai/v1/search"
    EXTRACT_URL = "https://api.parallel.ai/v1/extract"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("PARALLEL_API_KEY") or ""

    def _require_key(self) -> str:
        if not self._api_key:
            raise WebBackendError(
                "PARALLEL_API_KEY is required for the Parallel.ai backend.",
                provider=self.name,
            )
        return self._api_key

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return self.search_response(query, max_results=max_results).results

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
        import requests

        api_key = self._require_key()
        try:
            response = requests.post(
                self.SEARCH_URL,
                json={
                    "search_queries": [query],
                    "mode": "advanced",
                    "advanced_settings": {"max_results": max_results},
                },
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                },
                timeout=30,
                stream=True,
            )
        except requests.RequestException as error:
            raise WebBackendError(
                f"Parallel.ai search request failed: {error}",
                provider=self.name,
                retriable=True,
            ) from error

        if response.status_code != 200:
            raise hosted_http_error(
                response,
                provider=self.name,
                provider_label="Parallel.ai search",
                # Parallel documents 402 as insufficient account credit.
                reserved_quota_statuses=frozenset({402}),
                quota_details_enabled=False,
            )

        try:
            try:
                data = response.json() or {}
            except (ValueError, RecursionError, MemoryError) as error:
                raise WebBackendError(
                    "Parallel.ai search returned non-JSON.",
                    provider=self.name,
                    retriable=True,
                ) from error

            if not isinstance(data, dict):
                raise WebBackendError(
                    "Parallel.ai search returned an invalid response payload.",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )
            if "results" not in data:
                raise WebBackendError(
                    "Parallel.ai search returned no results field.",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )
            raw_results = data.get("results") or []
            if not isinstance(raw_results, list):
                raise WebBackendError(
                    "Parallel.ai search returned an invalid results payload.",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )

            results: List[SearchResult] = []
            for item in raw_results[:max_results]:
                if not isinstance(item, dict):
                    continue
                title = bound_text(_text(item.get("title")), limit=500)
                url = bound_text(_text(item.get("url")), limit=1000)
                snippet = _join_markdown(item.get("excerpts"))
                if title or url:
                    results.append(SearchResult(title=title, url=url, snippet=snippet))

            return SearchResponse(
                results=results,
                attempts=[
                    SearchAttempt(
                        provider=self.name,
                        success=True,
                        message=f"Parallel.ai returned {len(results)} result(s).",
                    )
                ],
            )
        finally:
            close_response(response)

    def fetch(self, url: str) -> str:
        return self.fetch_response(url).to_tool_output()

    def fetch_response(self, url: str) -> FetchResponse:
        import requests

        api_key = self._require_key()
        try:
            response = requests.post(
                self.EXTRACT_URL,
                json={
                    "urls": [url],
                    "advanced_settings": {"full_content": False},
                },
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                },
                timeout=30,
                stream=True,
            )
        except requests.RequestException as error:
            raise WebBackendError(
                f"Parallel.ai extract request failed: {error}",
                provider=self.name,
                retriable=True,
            ) from error

        if response.status_code != 200:
            raise hosted_http_error(
                response,
                provider=self.name,
                provider_label="Parallel.ai extract",
                reserved_quota_statuses=frozenset({402}),
                quota_details_enabled=False,
            )

        try:
            try:
                data = response.json() or {}
            except (ValueError, RecursionError, MemoryError) as error:
                raise WebBackendError(
                    "Parallel.ai extract returned non-JSON.",
                    provider=self.name,
                    retriable=True,
                ) from error

            if not isinstance(data, dict):
                raise WebBackendError(
                    "Parallel.ai extract returned an invalid response payload.",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )
            raw_results = data.get("results") or []
            if not isinstance(raw_results, list):
                raise WebBackendError(
                    "Parallel.ai extract returned an invalid results payload.",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )
            if raw_results and isinstance(raw_results[0], dict):
                result = raw_results[0]
                final_url = _text(result.get("url")) or url
                content = _text(result.get("full_content")) or _join_markdown(
                    result.get("excerpts"), separator="\n\n"
                )
                if content:
                    return FetchResponse(
                        final_url=final_url,
                        content=content,
                        content_type="text/markdown",
                        attempts=[
                            FetchAttempt(
                                provider=self.name,
                                success=True,
                                message=f"Parallel.ai extracted {final_url}.",
                            )
                        ],
                    )
                raise WebBackendError(
                    f"Parallel.ai extract returned empty content for {final_url}",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )

            errors = data.get("errors") or []
            if isinstance(errors, list) and errors:
                error = errors[0]
                diagnostics = {"extract_error": bound_diagnostics(error)}
                if isinstance(error, dict):
                    failed_url = _text(error.get("url")) or url
                    reason = (
                        _text(error.get("content"))
                        or _text(error.get("error_type"))
                        or "fetch failed"
                    )
                else:
                    failed_url = url
                    reason = _text(error) or "fetch failed"
                raise WebBackendError(
                    f"Parallel.ai failed to fetch {failed_url}: {bound_text(reason, limit=400)}",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                    diagnostics=diagnostics,
                )

            if "results" not in data and "errors" not in data:
                message = "Parallel.ai extract returned an invalid response payload."
            else:
                message = "Parallel.ai extract returned no results."
            raise WebBackendError(
                message,
                provider=self.name,
                retriable=True,
                degraded=True,
            )
        finally:
            close_response(response)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _join_markdown(value: Any, *, separator: str = "\n\n") -> str:
    if not isinstance(value, list):
        return ""
    return bound_text(
        separator.join(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ),
        limit=200_000,
    )


__all__ = ["ParallelBackend"]
