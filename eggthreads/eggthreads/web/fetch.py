from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from .base import FetchAttempt, FetchProvider, FetchResponse, WebBackendError
from .extract import html_to_markdown


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 200_000


class FetchOrchestrator:
    """Run an ordered fetch provider fallback chain."""

    def __init__(self, providers: Iterable[FetchProvider]):
        self.providers = list(providers)
        if not self.providers:
            raise WebBackendError("No fetch providers configured.", provider="fetch")

    def fetch_response(self, url: str) -> FetchResponse:
        attempts: list[FetchAttempt] = []
        last_error: WebBackendError | None = None
        for index, provider in enumerate(self.providers):
            provider_name = getattr(provider, "name", provider.__class__.__name__)
            try:
                response = provider.fetch_response(url)
            except WebBackendError as e:
                attempts.append(_attempt_from_error(provider_name, e))
                last_error = e
                has_fallback = index < len(self.providers) - 1
                if e.retriable and has_fallback:
                    continue
                if len(attempts) == 1:
                    raise
                raise _combined_fetch_error(attempts, e) from e
            response.attempts = attempts + response.attempts
            return response
        if last_error is not None:
            raise last_error
        raise WebBackendError("No fetch providers configured.", provider="fetch")

    def fetch(self, url: str) -> str:
        return self.fetch_response(url).to_tool_output()


class DirectHttpFetchProvider(FetchProvider):
    """Local/no-key URL fetch provider using direct HTTP + local extraction."""

    name = "direct_http"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout_sec: float | None = None,
        max_bytes: int | None = None,
        max_chars: int | None = None,
        name: str | None = None,
    ) -> None:
        if name:
            self.name = name
        self._ua = user_agent or os.environ.get("EGG_WEB_USER_AGENT") or DEFAULT_USER_AGENT
        self._timeout_sec = timeout_sec if timeout_sec is not None else _env_float(
            "EGG_WEB_FETCH_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC
        )
        self._max_bytes = max_bytes if max_bytes is not None else _env_int(
            "EGG_WEB_FETCH_MAX_BYTES", DEFAULT_MAX_BYTES
        )
        self._max_chars = max_chars if max_chars is not None else _env_int(
            "EGG_WEB_FETCH_MAX_CHARS", DEFAULT_MAX_CHARS
        )

    def fetch_response(self, url: str) -> FetchResponse:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise WebBackendError(
                f"Unsupported URL scheme for fetch: {parsed.scheme or '(missing)'}",
                provider=self.name,
                status_code=None,
            )

        import requests

        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": self._ua,
                    "Accept": (
                        "text/html,application/xhtml+xml,text/markdown,text/plain,"
                        "application/json,application/pdf;q=0.8,*/*;q=0.5"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self._timeout_sec,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise WebBackendError(
                f"fetch failed: {e}",
                provider=self.name,
                retriable=True,
            ) from e

        status_code = int(getattr(resp, "status_code", 0) or 0)
        if status_code >= 400:
            retriable = status_code == 429 or status_code >= 500
            raise WebBackendError(
                f"fetch status {status_code} for {url}",
                provider=self.name,
                retriable=retriable,
                status_code=status_code,
            )

        final_url = str(getattr(resp, "url", None) or url)
        raw_bytes = _response_bytes(resp)
        if len(raw_bytes) > self._max_bytes:
            raise WebBackendError(
                f"fetch response exceeded {self._max_bytes} bytes for {url}",
                provider=self.name,
                retriable=True,
                degraded=True,
                diagnostics={"max_bytes": self._max_bytes, "actual_bytes": len(raw_bytes)},
            )

        content_type = _content_type(resp)
        text = _response_text(resp, raw_bytes[: self._max_bytes])
        content, normalized_type = _extract_content(text, content_type, final_url)
        content = _bound_chars(content.strip(), self._max_chars)
        attempt = FetchAttempt(
            provider=self.name,
            success=True,
            message=f"Direct HTTP fetched {final_url}.",
            diagnostics={"content_type": content_type} if content_type else {},
        )
        return FetchResponse(
            final_url=final_url,
            content=content,
            content_type=normalized_type,
            attempts=[attempt],
        )

    def fetch(self, url: str) -> str:
        return self.fetch_response(url).to_tool_output()


def _attempt_from_error(provider_name: str, error: WebBackendError) -> FetchAttempt:
    return FetchAttempt(
        provider=error.provider or provider_name,
        success=False,
        degraded=True,
        retriable=error.retriable,
        message=str(error),
        diagnostics=error.diagnostics,
    )


def _combined_fetch_error(attempts: list[FetchAttempt], error: WebBackendError) -> WebBackendError:
    messages = [attempt.message for attempt in attempts if attempt.message]
    detail = "; ".join(messages[:3]) or str(error)
    return WebBackendError(
        f"fetch failed after provider fallback: {detail}",
        provider="fetch",
        retriable=any(attempt.retriable for attempt in attempts),
        degraded=True,
        diagnostics={
            "attempts": [
                {
                    "provider": attempt.provider,
                    "success": attempt.success,
                    "retriable": attempt.retriable,
                    "message": attempt.message[:200],
                }
                for attempt in attempts[:5]
            ]
        },
    )


def _content_type(resp: Any) -> str:
    headers = getattr(resp, "headers", None) or {}
    if hasattr(headers, "get"):
        return str(headers.get("Content-Type") or headers.get("content-type") or "").strip()
    return ""


def _response_bytes(resp: Any) -> bytes:
    content = getattr(resp, "content", None)
    if isinstance(content, bytes):
        return content
    text = getattr(resp, "text", "")
    if isinstance(text, str):
        return text.encode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
    return bytes(content or b"")


def _response_text(resp: Any, raw_bytes: bytes) -> str:
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    return raw_bytes.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")


def _extract_content(text: str, content_type: str, final_url: str) -> tuple[str, str]:
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "application/json" or kind.endswith("+json"):
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False), "application/json"
        except Exception:
            return text, content_type or "application/json"
    if kind in {"text/plain", "text/markdown", "text/x-markdown"}:
        return text, kind
    if kind in {"text/html", "application/xhtml+xml"} or not kind:
        return html_to_markdown(text, url=final_url), kind or "text/html"
    if kind.startswith("text/"):
        return text, kind
    return text, kind or content_type


def _bound_chars(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (truncated)"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
