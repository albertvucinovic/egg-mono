from __future__ import annotations

import os
import re
from typing import List

from .base import (
    FetchAttempt,
    FetchResponse,
    SearchAttempt,
    SearchResponse,
    SearchResult,
    WebBackend,
    WebBackendError,
    bound_text,
)


_ERROR_DETAIL_MAX_CHARS = 400
_SEMANTIC_QUOTA_STATUSES = {402, 403}
_PLAN_USAGE_LIMIT_RE = re.compile(
    r"\b(?:this\s+request\s+)?exceeds?\s+your\s+plan(?:[’']s|s)?"
    r"(?:\s+\w+){0,4}\s+usage\s+limit\b",
    re.IGNORECASE,
)
_PROVIDER_QUOTA_RE = re.compile(
    r"\b(?:your\s+)?plan(?:[’']s|s)?\s+(?:\w+\s+){0,3}"
    r"(?:usage|credit)\s+limit\s+(?:has\s+been\s+)?exceeded\b"
    r"|\b(?:usage|credit)\s+limit\s+(?:has\s+been\s+)?exceeded\b"
    r"|\binsufficient\s+(?:plan\s+)?credits?\b",
    re.IGNORECASE,
)
_JSON_DETAIL_RE = re.compile(
    r'''["'](?:detail|message|error)["']\s*:\s*'''
    r'''(?:\{[^{}]{0,200}?["'](?:message|detail)["']\s*:\s*)?'''
    r'''(?P<quote>["'])(?P<value>.{1,400}?)(?P=quote)''',
    re.IGNORECASE,
)


def _bounded_response_text(response: object) -> str:
    """Read at most the diagnostic prefix; never decode the full JSON body."""

    try:
        raw_text = getattr(response, "text", "")
    except BaseException:
        return ""
    if not isinstance(raw_text, (str, bytes)):
        return ""
    if isinstance(raw_text, bytes):
        raw_text = raw_text[:_ERROR_DETAIL_MAX_CHARS].decode("utf-8", errors="replace")
    prefix = raw_text[:_ERROR_DETAIL_MAX_CHARS]
    if len(raw_text) > _ERROR_DETAIL_MAX_CHARS:
        return prefix.rstrip() + "…"
    return bound_text(prefix, limit=_ERROR_DETAIL_MAX_CHARS)


def _response_detail(response: object) -> tuple[str, bool]:
    """Return bounded detail and whether it came from a known error field."""

    text = _bounded_response_text(response)
    match = _JSON_DETAIL_RE.search(text)
    if not match:
        return text, False
    return (
        bound_text(match.group("value"), limit=_ERROR_DETAIL_MAX_CHARS),
        True,
    )


def _is_usage_limit_detail(status_code: int, detail: str, *, structured: bool) -> bool:
    if status_code not in _SEMANTIC_QUOTA_STATUSES:
        return False
    if _PLAN_USAGE_LIMIT_RE.search(detail):
        return True
    return structured and bool(_PROVIDER_QUOTA_RE.search(detail))


def _http_error(response: object, *, provider: str) -> WebBackendError:
    """Classify a Tavily HTTP failure without unbounded body parsing."""

    try:
        status_code = getattr(response, "status_code", None)
    except BaseException:
        status_code = None
    if not isinstance(status_code, int):
        status_code = 0

    # Status 432 is provider-defined quota exhaustion. Establish recovery
    # semantics before touching the response body so malformed/deep bodies can
    # never suppress fallback. Detail extraction below is best-effort only.
    quota_exhausted = status_code == 432
    try:
        detail, structured = _response_detail(response)
    except BaseException:
        detail, structured = "", False
    if not quota_exhausted:
        quota_exhausted = _is_usage_limit_detail(
            status_code,
            detail,
            structured=structured,
        )

    retriable = not quota_exhausted and (status_code == 429 or status_code >= 500)
    diagnostics = {
        "status_code": status_code,
        "response_detail": detail,
    }
    if quota_exhausted:
        diagnostics["failure_kind"] = "quota_exhausted"
    suffix = f": {detail}" if detail else ""
    return WebBackendError(
        f"Tavily API status {status_code}{suffix}",
        provider=provider,
        retriable=retriable,
        fallback_eligible=quota_exhausted or retriable,
        status_code=status_code,
        diagnostics=diagnostics,
    )


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
            raise _http_error(resp, provider=self.name)
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
        return self.fetch_response(url).to_tool_output()

    def fetch_response(self, url: str) -> FetchResponse:
        import requests
        api_key = self._require_key()
        try:
            resp = requests.post(
                self.EXTRACT_URL,
                json={"urls": [url], "format": "markdown"},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise WebBackendError(
                f"Tavily extract request failed: {e}",
                provider=self.name,
                retriable=True,
            ) from e
        if resp.status_code != 200:
            raise _http_error(resp, provider=self.name)
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise WebBackendError(
                "Tavily extract returned non-JSON.",
                provider=self.name,
                retriable=True,
            ) from e
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
                return FetchResponse(
                    final_url=result_url,
                    content=content,
                    content_type="text/markdown",
                    attempts=[
                        FetchAttempt(
                            provider=self.name,
                            success=True,
                            message=f"Tavily extracted {result_url}.",
                        )
                    ],
                )
            raise WebBackendError(
                f"Tavily extract returned empty content for {result_url}",
                provider=self.name,
                retriable=True,
                degraded=True,
            )
        if failed:
            first = failed[0]
            if isinstance(first, dict):
                failed_url = str(first.get("url") or url).strip() or url
                reason = str(
                    first.get("error") or first.get("reason") or "fetch failed"
                ).strip()
                raise WebBackendError(
                    f"failed to fetch {failed_url}: {reason}",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                    diagnostics={"failed_result": {"url": failed_url, "reason": reason[:200]}},
                )
            s = str(first).strip()
            if s:
                raise WebBackendError(
                    f"failed to fetch {url}: {s}",
                    provider=self.name,
                    retriable=True,
                    degraded=True,
                )
        raise WebBackendError(
            "Tavily extract returned no results.",
            provider=self.name,
            retriable=True,
            degraded=True,
        )
