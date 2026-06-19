from __future__ import annotations

import os
from typing import List

from .base import SearchAttempt, SearchResponse, SearchResult, WebBackend, WebBackendError


class TavilyBackend(WebBackend):
    name = "tavily"

    SEARCH_URL = "https://api.tavily.com/search"
    EXTRACT_URL = "https://api.tavily.com/extract"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY") or ""

    def _require_key(self) -> str:
        if not self._api_key:
            raise WebBackendError("TAVILY_API_KEY not set in environment.", provider=self.name)
        return self._api_key

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return self.search_response(query, max_results=max_results).results

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
        import requests
        api_key = self._require_key()
        try:
            resp = requests.post(
                self.SEARCH_URL,
                json={
                    "query": query,
                    "max_results": max_results,
                    "include_answer": False,
                    "search_depth": "basic",
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise WebBackendError(
                f"Tavily request failed: {e}",
                provider=self.name,
                retriable=True,
            ) from e
        if resp.status_code != 200:
            retriable = resp.status_code == 429 or resp.status_code >= 500
            raise WebBackendError(
                f"Tavily API status {resp.status_code}: {resp.text[:400]}",
                provider=self.name,
                retriable=retriable,
                status_code=resp.status_code,
            )
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise WebBackendError(
                "Tavily returned non-JSON.",
                provider=self.name,
                retriable=True,
            ) from e
        raw = data.get("results") or data.get("data") or []
        out: List[SearchResult] = []
        for r in raw[:max_results]:
            if not isinstance(r, dict):
                continue
            title = (r.get("title") or "").strip()
            url = (r.get("url") or r.get("link") or "").strip()
            snippet = (r.get("content") or r.get("snippet") or "").strip()
            if title or url:
                out.append(SearchResult(title=title, url=url, snippet=snippet))
        return SearchResponse(
            results=out,
            attempts=[
                SearchAttempt(
                    provider=self.name,
                    success=True,
                    message=f"Tavily returned {len(out)} result(s).",
                )
            ],
        )

    def fetch(self, url: str) -> str:
        import requests
        api_key = self._require_key()
        resp = requests.post(
            self.EXTRACT_URL,
            json={"urls": [url], "format": "markdown"},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise WebBackendError(
                f"Tavily API status {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json() or {}
        results = data.get("results") or []
        failed = data.get("failed_results") or []
        if results and isinstance(results[0], dict):
            first = results[0]
            result_url = str(first.get("url") or url).strip() or url
            content = first.get("raw_content")
            if not isinstance(content, str):
                content = ""
            content = content.strip()
            if content:
                return f"URL: {result_url}\n\n{content}"
            return f"URL: {result_url}\n\n(no content)"
        if failed:
            first = failed[0]
            if isinstance(first, dict):
                failed_url = str(first.get("url") or url).strip() or url
                reason = str(
                    first.get("error") or first.get("reason") or "fetch failed"
                ).strip()
                raise WebBackendError(f"failed to fetch {failed_url}: {reason}")
            s = str(first).strip()
            if s:
                raise WebBackendError(f"failed to fetch {url}: {s}")
        raise WebBackendError("No results.")
