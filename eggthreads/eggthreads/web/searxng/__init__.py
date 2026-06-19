from __future__ import annotations

import os
from typing import Any, List

from ..base import SearchAttempt, SearchResponse, SearchResult, WebBackend, WebBackendError


DEFAULT_URL = "http://localhost:8888"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _bounded_text(value: Any, *, limit: int = 160) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _bounded_list(values: list[Any], *, limit: int = 5) -> list[Any]:
    return values[:limit]


def _extract_unresponsive_engines(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("unresponsive_engines") or []
    if not isinstance(raw, list):
        return []
    engines: list[dict[str, str]] = []
    for item in raw:
        name = ""
        reason = ""
        if isinstance(item, dict):
            name = _bounded_text(
                item.get("engine") or item.get("name") or item.get("id"),
                limit=80,
            )
            reason = _bounded_text(
                item.get("error") or item.get("reason") or item.get("exception"),
                limit=120,
            )
        elif isinstance(item, (list, tuple)):
            if item:
                name = _bounded_text(item[0], limit=80)
            if len(item) > 2:
                reason = _bounded_text(item[-1] or item[1], limit=120)
            elif len(item) > 1:
                reason = _bounded_text(item[1], limit=120)
        else:
            name = _bounded_text(item, limit=80)
        if name or reason:
            engines.append({"name": name or "unknown", "reason": reason})
    return engines


def _unresponsive_summary(engines: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for engine in engines[:5]:
        name = engine.get("name") or "unknown"
        reason = engine.get("reason") or "unresponsive"
        parts.append(f"{name} {reason}")
    suffix = "" if len(engines) <= 5 else f"; +{len(engines) - 5} more"
    return "; ".join(parts) + suffix


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
        return self.search_response(query, max_results=max_results).results

    def search_response(self, query: str, max_results: int = 5) -> SearchResponse:
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
                "Alternatively set EGG_WEB_BACKEND=tavily and TAVILY_API_KEY.",
                provider=self.name,
                retriable=True,
            ) from e
        except requests.RequestException as e:
            raise WebBackendError(
                f"SearXNG request failed: {e}",
                provider=self.name,
                retriable=True,
            ) from e
        if resp.status_code != 200:
            retriable = resp.status_code == 429 or resp.status_code >= 500
            raise WebBackendError(
                f"SearXNG status {resp.status_code}: {resp.text[:400]}",
                provider=self.name,
                retriable=retriable,
                status_code=resp.status_code,
            )
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise WebBackendError(
                "SearXNG returned non-JSON (is the json format enabled in settings.yml?)",
                provider=self.name,
                retriable=True,
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
        unresponsive = _extract_unresponsive_engines(data)
        diagnostics = {"unresponsive_engines": _bounded_list(unresponsive)} if unresponsive else {}
        degraded = bool(unresponsive)
        summary = _unresponsive_summary(unresponsive) if unresponsive else ""
        if degraded and not out:
            message = f"SearXNG degraded: {summary}" if summary else "SearXNG degraded."
            retriable = True
        elif degraded:
            message = f"SearXNG degraded: {summary}" if summary else "SearXNG partially degraded."
            retriable = False
        else:
            message = f"SearXNG returned {len(out)} result(s)."
            retriable = False
        attempt = SearchAttempt(
            provider=self.name,
            success=True,
            degraded=degraded,
            retriable=retriable,
            message=message,
            diagnostics=diagnostics,
        )
        return SearchResponse(results=out, attempts=[attempt])

    def fetch(self, url: str) -> str:
        from ..fetch import DirectHttpFetchProvider

        return DirectHttpFetchProvider(user_agent=self._ua, name=self.name).fetch(url)
