from __future__ import annotations

import json
import os
import re
import zlib
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


_ERROR_WIRE_MAX_BYTES = 4096
_ERROR_DECODED_MAX_BYTES = 4096
_ERROR_DETAIL_MAX_CHARS = 400
_SEMANTIC_QUOTA_STATUSES = {402, 403}
_RESERVED_QUOTA_STATUSES = {432, 433}
_SUPPORTED_CONTENT_ENCODINGS = {"", "identity", "gzip", "x-gzip", "deflate"}
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
def _header_value(response: object, name: str) -> str:
    try:
        headers = getattr(response, "headers", None)
        get = getattr(headers, "get", None)
        if not callable(get):
            return ""
        return str(get(name) or get(name.lower()) or "").strip()
    except BaseException:
        return ""


def _read_error_wire(response: object) -> tuple[bytes, bool]:
    """Read at most wire-cap + 1 bytes and report whether EOF was observed."""

    try:
        raw = getattr(response, "raw", None)
        read = getattr(raw, "read", None)
    except BaseException:
        raw = None
        read = None
    if callable(read):
        try:
            raw.decode_content = False
        except (AttributeError, TypeError):
            pass
        target = _ERROR_WIRE_MAX_BYTES + 1
        chunks: list[bytes] = []
        total = 0
        eof = False
        while total < target:
            remaining = target - total
            chunk = read(remaining)
            if not chunk:
                eof = True
                break
            if isinstance(chunk, str):
                chunk = chunk[:remaining].encode("utf-8", errors="replace")
            elif isinstance(chunk, (bytes, bytearray)):
                chunk = bytes(chunk[:remaining])
            else:
                return b"", False
            if not chunk:
                eof = True
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return data[:_ERROR_WIRE_MAX_BYTES], eof and len(data) <= _ERROR_WIRE_MAX_BYTES

    # Compatibility for small test doubles only. Real streamed requests use raw.
    content = getattr(response, "content", None)
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    if isinstance(content, (bytes, bytearray)):
        data = bytes(content[:_ERROR_WIRE_MAX_BYTES + 1])
        return data[:_ERROR_WIRE_MAX_BYTES], len(data) <= _ERROR_WIRE_MAX_BYTES

    text = getattr(response, "text", "")
    if isinstance(text, bytes):
        data = text[:_ERROR_WIRE_MAX_BYTES + 1]
    elif isinstance(text, str):
        data = text.encode("utf-8", errors="replace")[:_ERROR_WIRE_MAX_BYTES + 1]
    else:
        return b"", False
    return data[:_ERROR_WIRE_MAX_BYTES], len(data) <= _ERROR_WIRE_MAX_BYTES


def _bounded_decompress(data: bytes, *, wbits: int) -> tuple[bytes, bool]:
    decoder = zlib.decompressobj(wbits)
    out = decoder.decompress(data, _ERROR_DECODED_MAX_BYTES + 1)
    overflow = len(out) > _ERROR_DECODED_MAX_BYTES or bool(decoder.unconsumed_tail)
    return out[:_ERROR_DECODED_MAX_BYTES], decoder.eof and not overflow


def _decode_error_wire(
    wire: bytes,
    *,
    wire_complete: bool,
    content_encoding: str,
) -> tuple[bytes, bool]:
    encoding = content_encoding.lower().strip()
    if "," in encoding or encoding not in _SUPPORTED_CONTENT_ENCODINGS:
        return b"", False
    if encoding in ("", "identity"):
        return wire[:_ERROR_DECODED_MAX_BYTES], wire_complete
    try:
        if encoding in ("gzip", "x-gzip"):
            decoded, stream_complete = _bounded_decompress(wire, wbits=16 + zlib.MAX_WBITS)
        else:
            try:
                decoded, stream_complete = _bounded_decompress(wire, wbits=zlib.MAX_WBITS)
            except zlib.error:
                decoded, stream_complete = _bounded_decompress(wire, wbits=-zlib.MAX_WBITS)
    except (zlib.error, MemoryError):
        return b"", False
    return decoded, wire_complete and stream_complete


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


def _decode_error_prefix(prefix: bytes, *, complete: bool) -> tuple[str, bool]:
    text = prefix.decode("utf-8", errors="replace")
    return text, not complete


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


def _error_details(prefix: bytes, *, complete: bool) -> tuple[list[str], str]:
    """Return all recognized JSON error values or one whole plain message."""

    text, truncated = _decode_error_prefix(prefix, complete=complete)
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

    # Tavily's official Python SDK 0.7.11 reserves both 432 and 433 beside
    # forbidden/paygo failures for search and extract. Establish this before
    # body work so malformed/encoded bodies cannot suppress fallback.
    quota_exhausted = status_code in _RESERVED_QUOTA_STATUSES
    try:
        wire, wire_complete = _read_error_wire(response)
        decoded, decoded_complete = _decode_error_wire(
            wire,
            wire_complete=wire_complete,
            content_encoding=_header_value(response, "Content-Encoding"),
        )
        semantic_details, diagnostic = _error_details(
            decoded,
            complete=decoded_complete,
        )
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
