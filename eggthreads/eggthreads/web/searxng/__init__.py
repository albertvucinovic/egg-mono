from __future__ import annotations

import os
from typing import List

from ..base import SearchResult, WebBackend, WebBackendError
from ..extract import html_to_markdown


DEFAULT_URL = "http://localhost:8888"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SearxngBackend(WebBackend):
    name = "searxng"

    def __init__(
        self,
        base_url: str | None = None,
        user_agent: str | None = None,
    ):
        self._base_url = (
            base_url
            or os.environ.get("SEARXNG_URL")
            or os.environ.get("EGG_SEARXNG_URL")
            or DEFAULT_URL
        ).rstrip("/")
        self._ua = user_agent or os.environ.get("EGG_WEB_USER_AGENT") or DEFAULT_USER_AGENT

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        import requests
        try:
            resp = requests.get(
                f"{self._base_url}/search",
                params={"q": query, "format": "json"},
                headers={"User-Agent": self._ua, "Accept": "application/json"},
                timeout=20,
            )
        except requests.ConnectionError as e:
            raise WebBackendError(
                f"SearXNG not reachable at {self._base_url}. "
                "Run /startSearxng in egg to start the local container "
                "(or `docker-compose up -d` in "
                "eggthreads/eggthreads/web/searxng/). "
                "Alternatively set EGG_WEB_BACKEND=tavily and TAVILY_API_KEY."
            ) from e
        except requests.RequestException as e:
            raise WebBackendError(f"SearXNG request failed: {e}") from e
        if resp.status_code != 200:
            raise WebBackendError(
                f"SearXNG status {resp.status_code}: {resp.text[:400]}"
            )
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise WebBackendError(
                "SearXNG returned non-JSON (is the json format enabled in settings.yml?)"
            ) from e
        raw = data.get("results") or []
        out: List[SearchResult] = []
        for r in raw[:max_results]:
            if not isinstance(r, dict):
                continue
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            snippet = (r.get("content") or "").strip()
            if title or url:
                out.append(SearchResult(title=title, url=url, snippet=snippet))
        return out

    def fetch(self, url: str) -> str:
        import requests
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": self._ua,
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=20,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise WebBackendError(f"fetch failed: {e}") from e
        if resp.status_code >= 400:
            raise WebBackendError(
                f"fetch status {resp.status_code} for {url}"
            )
        final_url = resp.url or url
        markdown = html_to_markdown(resp.text or "", url=final_url)
        if not markdown.strip():
            return f"URL: {final_url}\n\n(no content)"
        return f"URL: {final_url}\n\n{markdown}"
