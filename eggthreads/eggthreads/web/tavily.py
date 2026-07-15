from __future__ import annotations

import json
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


_ERROR_DETAIL_MAX_BYTES = 4096
_ERROR_DETAIL_MAX_CHARS = 400
_SEMANTIC_QUOTA_STATUSES = {402, 403}
_NEGATED_OR_QUALIFIED_RE = re.compile(
    r"\b(?:nearly|almost|might|may|could|would|not|never|no\s+longer)\b",
    re.IGNORECASE,
)
_REQUEST_PLAN_LIMIT_RE = re.compile(
    r"^(?:this\s+)?request\s+exceeds?\s+your\s+plan(?:[’']s|s)?"
    r"(?:\s+[a-z]+){0,4}\s+usage\s+limit"
    r"(?:\.\s*please\s+upgrade\s+your\s+plan\.?)?$",
    re.IGNORECASE,
)
_PROVIDER_QUOTA_RE = re.compile(
    r"^(?:your\s+)?plan(?:[’']s|s)?\s+(?:[a-z]+\s+){0,3}"
    r"(?:usage|credit)\s+limit\s+(?:has\s+been\s+)?exceeded(?:\s+for\s+your\s+account)?\.?$"
    r"|^(?:usage|credit)\s+limit\s+(?:has\s+been\s+)?exceeded(?:\s+for\s+your\s+account)?\.?$"
    r"|^insufficient\s+(?:plan\s+)?credits?\.?$",
    re.IGNORECASE,
)
def _read_error_prefix(response: object) -> bytes:
    """Read and close at most one bounded prefix from a streamed response."""

    prefix = bytearray()
    raw = getattr(response, "raw", None)
    read = getattr(raw, "read", None)
    if callable(read):
        chunk = read(_ERROR_DETAIL_MAX_BYTES)
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        if not isinstance(chunk, (bytes, bytearray)):
            return b""
        return bytes(chunk[:_ERROR_DETAIL_MAX_BYTES])

    # Compatibility for small test doubles. Real requests use ``raw`` because
    # Tavily calls set stream=True; never access requests.Response.text here.
    content = getattr(response, "content", None)
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content[:_ERROR_DETAIL_MAX_BYTES])

    text = getattr(response, "text", "")
    if isinstance(text, bytes):
        return text[:_ERROR_DETAIL_MAX_BYTES]
    if isinstance(text, str):
        return text.encode("utf-8", errors="replace")[:_ERROR_DETAIL_MAX_BYTES]
    return b""


def _close_response(response: object) -> None:
    try:
        close = getattr(response, "close", None)
    except BaseException:
        return
    if callable(close):
        try:
            close()
        except BaseException:
            pass


def _decode_error_prefix(prefix: bytes) -> tuple[str, bool]:
    text = prefix.decode("utf-8", errors="replace")
    truncated = len(prefix) >= _ERROR_DETAIL_MAX_BYTES
    return text, truncated


def _collect_json_error_strings(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    out: list[str] = []
    for key in ("detail", "message", "error"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            for nested_key in ("detail", "message", "error"):
                nested = item.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    out.append(nested.strip())
    return out


def _error_details(prefix: bytes) -> tuple[list[str], str]:
    """Return all recognized JSON error values or one whole plain message."""

    text, truncated = _decode_error_prefix(prefix)
    diagnostic = bound_text(text, limit=_ERROR_DETAIL_MAX_CHARS - 1)
    stripped = text.strip()
    if not stripped:
        return [], ""

    if stripped.startswith(("{", "[")):
        if truncated:
            return [], diagnostic
        try:
            payload = json.loads(stripped)
        except (ValueError, RecursionError, MemoryError):
            return [], diagnostic
        details = [
            bound_text(item, limit=_ERROR_DETAIL_MAX_CHARS - 1)
            for item in _collect_json_error_strings(payload)
        ]
        return details, details[0] if details else diagnostic

    if truncated or stripped.startswith("<") or "\x00" in stripped:
        return [], diagnostic
    return [bound_text(stripped, limit=_ERROR_DETAIL_MAX_CHARS - 1)], diagnostic


def _is_usage_limit_detail(status_code: int, detail: str) -> bool:
    if status_code not in _SEMANTIC_QUOTA_STATUSES:
        return False
    normalized = " ".join(detail.strip().split())
    if _NEGATED_OR_QUALIFIED_RE.search(normalized):
        return False
    return bool(
        _REQUEST_PLAN_LIMIT_RE.fullmatch(normalized)
        or _PROVIDER_QUOTA_RE.fullmatch(normalized)
    )


def _http_error(response: object, *, provider: str) -> WebBackendError:
    """Classify one streamed Tavily HTTP failure with bounded body work."""

    try:
        status_code = getattr(response, "status_code", None)
    except BaseException:
        status_code = None
    if not isinstance(status_code, int):
        status_code = 0

    # Status 432 is provider-defined quota exhaustion before any body work.
    quota_exhausted = status_code == 432
    try:
        prefix = _read_error_prefix(response)
        semantic_details, diagnostic = _error_details(prefix)
    except BaseException:
        semantic_details, diagnostic = [], ""
    finally:
        _close_response(response)

    if not quota_exhausted:
        quota_exhausted = any(
            _is_usage_limit_detail(status_code, detail)
            for detail in semantic_details
        )

    retriable = not quota_exhausted and (status_code == 429 or status_code >= 500)
    diagnostics = {
        "status_code": status_code,
        "response_detail": diagnostic,
    }
    if quota_exhausted:
        diagnostics["failure_kind"] = "quota_exhausted"
    suffix = f": {diagnostic}" if diagnostic else ""
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
                stream=True,
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
        finally:
            _close_response(resp)

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
                stream=True,
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
        finally:
            _close_response(resp)
