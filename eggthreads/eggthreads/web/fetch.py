from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .base import (
    FetchAttempt,
    FetchProvider,
    FetchResponse,
    WebBackendError,
    bound_diagnostics,
    bound_text,
    coerce_nonnegative_float,
    coerce_nonnegative_int,
)
from .extract import html_to_markdown


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 200_000
DEFAULT_FETCH_CACHE_TTL_SEC = 300.0
DEFAULT_FETCH_CACHE_MAX_ENTRIES = 128
DEFAULT_FETCH_CACHE_MAX_CHARS = 500_000
FETCH_CACHE_VERSION = "fetch-cache-v1"
QUALITY_SCORE_THRESHOLD = 4
NEAR_EMPTY_HTML_CHARS = 20
NEAR_EMPTY_HTML_WORDS = 3


@dataclass(frozen=True)
class FetchQuality:
    """Small, explainable quality score for direct HTTP fetch output."""

    score: int
    signals: tuple[str, ...]
    details: tuple[str, ...] = ()
    threshold: int = QUALITY_SCORE_THRESHOLD

    @property
    def ok(self) -> bool:
        return self.score < self.threshold

    @property
    def summary(self) -> str:
        return ", ".join(self.signals) if self.signals else "ok"

    def diagnostics(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "threshold": self.threshold,
            "signals": list(self.signals),
            "details": list(self.details),
        }


@dataclass
class _FetchCacheEntry:
    expires_at: float
    response: FetchResponse


_FETCH_CACHE: OrderedDict[tuple[Any, ...], _FetchCacheEntry] = OrderedDict()


class FetchOrchestrator:
    """Run an ordered fetch provider fallback chain."""

    def __init__(
        self,
        providers: Iterable[FetchProvider],
        *,
        cache_ttl_sec: float | None = None,
        cache_max_entries: int | None = None,
        cache_max_chars: int | None = None,
        cache_enabled: bool = True,
    ):
        self.providers = list(providers)
        if not self.providers:
            raise WebBackendError("No fetch providers configured.", provider="fetch")
        self.cache_ttl_sec = coerce_nonnegative_float(cache_ttl_sec, DEFAULT_FETCH_CACHE_TTL_SEC)
        self.cache_max_entries = coerce_nonnegative_int(
            cache_max_entries, DEFAULT_FETCH_CACHE_MAX_ENTRIES
        )
        self.cache_max_chars = coerce_nonnegative_int(cache_max_chars, DEFAULT_FETCH_CACHE_MAX_CHARS)
        self.cache_enabled = cache_enabled

    def fetch_response(self, url: str) -> FetchResponse:
        cache_key = self._cache_key(url)
        if self.cache_enabled:
            cached = _fetch_cache_get(cache_key)
            if cached is not None:
                return cached

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
            if self.cache_enabled and self._is_cacheable(response):
                keys = [cache_key]
                final_key = self._cache_key(response.final_url)
                if final_key != cache_key and self.cache_max_entries >= 2:
                    keys.append(final_key)
                _fetch_cache_put(keys, response, self.cache_ttl_sec, self.cache_max_entries)
            return response
        if last_error is not None:
            raise last_error
        raise WebBackendError("No fetch providers configured.", provider="fetch")

    def fetch(self, url: str) -> str:
        return self.fetch_response(url).to_tool_output()

    def _cache_key(self, url: str) -> tuple[Any, ...]:
        return (
            FETCH_CACHE_VERSION,
            tuple(_provider_cache_identity(provider) for provider in self.providers),
            _normalize_url(url),
        )

    def _is_cacheable(self, response: FetchResponse) -> bool:
        if self.cache_ttl_sec <= 0 or self.cache_max_entries <= 0:
            return False
        if response.degraded or not response.content.strip():
            return False
        if len(response.content) > self.cache_max_chars:
            return False
        return True


def clear_fetch_cache() -> None:
    """Clear process-local fetch cache state for tests/operator reset."""

    _FETCH_CACHE.clear()


def _fetch_cache_get(key: tuple[Any, ...]) -> FetchResponse | None:
    now = time.monotonic()
    entry = _FETCH_CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        _FETCH_CACHE.pop(key, None)
        return None
    _FETCH_CACHE.move_to_end(key)
    return copy.deepcopy(entry.response)


def _fetch_cache_put(
    keys: list[tuple[Any, ...]],
    response: FetchResponse,
    ttl_sec: float,
    max_entries: int,
) -> None:
    cached = _response_for_cache(response)
    expires_at = time.monotonic() + ttl_sec
    for key in keys:
        _FETCH_CACHE[key] = _FetchCacheEntry(
            expires_at=expires_at,
            response=copy.deepcopy(cached),
        )
        _FETCH_CACHE.move_to_end(key)
    while len(_FETCH_CACHE) > max_entries:
        _FETCH_CACHE.popitem(last=False)


def _provider_cache_identity(provider: FetchProvider) -> tuple[str, ...]:
    cls = provider.__class__
    parts = [
        str(getattr(provider, "name", cls.__name__)),
        f"{cls.__module__}.{cls.__qualname__}",
    ]
    extract_url = getattr(provider, "EXTRACT_URL", None)
    if extract_url:
        parts.append(f"extract_url={extract_url}")
    if hasattr(provider, "_api_key"):
        api_key = str(getattr(provider, "_api_key") or "")
        if api_key:
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
            parts.append(f"api_key_hash={key_hash}")
        else:
            parts.append("api_key=missing")
    direct_attrs = (
        ("user_agent", "_ua"),
        ("timeout_sec", "_timeout_sec"),
        ("max_bytes", "_max_bytes"),
        ("max_chars", "_max_chars"),
    )
    for label, attr in direct_attrs:
        if hasattr(provider, attr):
            parts.append(f"{label}={getattr(provider, attr)}")
    return tuple(parts)


def _response_for_cache(response: FetchResponse) -> FetchResponse:
    return FetchResponse(
        final_url=str(response.final_url or "").strip(),
        content=str(response.content or ""),
        content_type=str(response.content_type or "").strip(),
        attempts=[
            FetchAttempt(
                provider=bound_text(attempt.provider, limit=80),
                success=attempt.success,
                degraded=attempt.degraded,
                retriable=attempt.retriable,
                message=bound_text(attempt.message, limit=500),
                diagnostics=bound_diagnostics(attempt.diagnostics),
            )
            for attempt in response.attempts[:8]
        ],
    )


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    return parsed._replace(scheme=scheme, netloc=netloc, fragment="").geturl()


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
        quality = classify_fetch_quality(
            raw_text=text,
            extracted_content=content,
            content_type=content_type,
            final_url=final_url,
            header_hints=_quality_header_hints(resp),
        )
        if not quality.ok:
            raise WebBackendError(
                f"Direct HTTP content degraded for {final_url}: {quality.summary}",
                provider=self.name,
                retriable=True,
                degraded=True,
                diagnostics={"fetch_quality": quality.diagnostics()},
            )
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


def classify_fetch_quality(
    *,
    raw_text: str,
    extracted_content: str,
    content_type: str,
    final_url: str,
    header_hints: tuple[str, ...] = (),
) -> FetchQuality:
    """Return a small confidence score for known low-quality fetch output.

    The score intentionally combines multiple concise signals instead of trying
    to be a comprehensive allow/deny list.  New block-page families can be added
    by appending one weighted signal and one focused test.
    """

    kind = content_type.split(";", 1)[0].strip().lower()
    raw_lower = _normalize_for_quality(raw_text)
    content_lower = _normalize_for_quality(extracted_content)
    combined = f"{raw_lower}\n{content_lower}"
    htmlish = _looks_like_html(kind, raw_lower)
    signals: list[str] = []
    details: list[str] = []
    score = 0

    def add(signal: str, weight: int, detail: str = "") -> None:
        nonlocal score
        if signal in signals:
            return
        signals.append(signal)
        score += weight
        if detail:
            details.append(detail[:200])

    content = extracted_content.strip()
    word_count = len(re.findall(r"\w+", content))
    if htmlish and not content:
        add("empty_html_extraction", QUALITY_SCORE_THRESHOLD, "no extracted text")
    elif htmlish and (len(content) < NEAR_EMPTY_HTML_CHARS or word_count < NEAR_EMPTY_HTML_WORDS):
        add(
            "near_empty_html_extraction",
            QUALITY_SCORE_THRESHOLD,
            f"{len(content)} chars/{word_count} words",
        )

    if _contains_any(
        combined,
        (
            "just a moment",
            "attention required",
            "/cdn-cgi/",
            "cf-browser-verification",
            "cloudflare ray id",
        ),
    ):
        add("cloudflare_challenge", QUALITY_SCORE_THRESHOLD)

    if _contains_any(
        combined,
        (
            "captcha",
            "recaptcha",
            "g-recaptcha",
            "h-captcha",
            "hcaptcha",
            "turnstile",
            "cf-turnstile",
        ),
    ):
        add("captcha_challenge", QUALITY_SCORE_THRESHOLD)

    if _contains_any(
        combined,
        (
            "enable javascript",
            "please enable js",
            "checking your browser",
            "browser check",
            "javascript and cookies",
        ),
    ):
        add("javascript_required_placeholder", QUALITY_SCORE_THRESHOLD)

    if _contains_any(
        combined,
        (
            "access denied",
            "unusual traffic",
            "automated queries",
            "traffic from your computer network",
            "temporarily blocked",
            "request blocked",
        ),
    ):
        add("bot_block_placeholder", QUALITY_SCORE_THRESHOLD)

    header_score = _header_hint_score(header_hints)
    if header_score >= QUALITY_SCORE_THRESHOLD:
        add("provider_challenge_headers", QUALITY_SCORE_THRESHOLD, ", ".join(header_hints[:5]))

    if htmlish and re.search(r"<meta\b[^>]*http-equiv=[\"']?refresh", raw_text, re.IGNORECASE):
        add("meta_refresh_placeholder", QUALITY_SCORE_THRESHOLD, "meta refresh")

    script_count = len(re.findall(r"<script\b", raw_text, re.IGNORECASE))
    form_count = len(re.findall(r"<form\b", raw_text, re.IGNORECASE))
    if htmlish and (script_count >= 6 or form_count >= 2) and _low_or_generic_content(content_lower):
        add(
            "script_form_heavy_placeholder",
            QUALITY_SCORE_THRESHOLD,
            f"{script_count} scripts/{form_count} forms",
        )

    if htmlish and _low_or_generic_content(content_lower) and _contains_any(
        combined,
        (
            "unsupported browser",
            "browser is not supported",
            "browser not supported",
            "outdated browser",
            "upgrade your browser",
            "update your browser",
            "requires a modern browser",
        ),
    ):
        add("unsupported_browser_placeholder", QUALITY_SCORE_THRESHOLD)

    if htmlish and _low_or_generic_content(content_lower) and _contains_any(
        combined,
        (
            "sign in to continue",
            "log in to continue",
            "login to continue",
            "login required",
            "please log in",
            "please sign in",
            "create an account to continue",
            "accept cookies to continue",
            "allow cookies to continue",
            "cookie consent",
            "consent required",
            "manage consent",
            "we value your privacy",
        ),
    ):
        add("login_or_consent_wall", QUALITY_SCORE_THRESHOLD)

    path = urlparse(final_url).path.lower()
    if _has_suspicious_final_path(path) and _low_or_generic_content(content_lower):
        add("suspicious_final_url_path", QUALITY_SCORE_THRESHOLD, path)

    return FetchQuality(score=score, signals=tuple(signals), details=tuple(details))


def _normalize_for_quality(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _looks_like_html(kind: str, raw_lower: str) -> bool:
    if kind in {"text/html", "application/xhtml+xml"}:
        return True
    if kind:
        return False
    return any(marker in raw_lower for marker in ("<html", "<!doctype", "<body"))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _quality_header_hints(resp: Any) -> tuple[str, ...]:
    headers = getattr(resp, "headers", None) or {}
    if not hasattr(headers, "items"):
        return ()
    hints: set[str] = set()
    for raw_name, raw_value in list(headers.items())[:50]:
        name = str(raw_name or "").strip().lower()
        value = str(raw_value or "").strip().lower()[:500]
        combined = f"{name}: {value}"
        if name == "server" and "cloudflare" in value:
            hints.add("cloudflare_cdn_header")
        if (
            name in {"cf-mitigated", "cf-chl-bypass"}
            or "cf-chl" in combined
            or "__cf_bm" in combined
            or "cf_clearance" in combined
        ):
            hints.add("cloudflare_challenge_header")
        if (
            "x-akamai" in name
            or "akamai-bot" in combined
            or "_abck" in combined
            or "ak_bmsc" in combined
            or "bm_sz" in combined
        ):
            hints.add("akamai_challenge_header")
        if "perimeterx" in combined or name.startswith("x-px") or "_px" in combined:
            hints.add("perimeterx_challenge_header")
        if "datadome" in combined or name.startswith("x-datadome"):
            hints.add("datadome_challenge_header")
        if (
            "incapsula" in combined
            or "incap_ses" in combined
            or "visid_incap" in combined
            or "nlbi_" in combined
            or name == "x-iinfo"
        ):
            hints.add("incapsula_challenge_header")
        if "ddos-guard" in combined or "__ddg" in combined or name.startswith("x-ddg"):
            hints.add("ddos_guard_challenge_header")
    return tuple(sorted(hints))


def _header_hint_score(header_hints: tuple[str, ...]) -> int:
    score = 0
    for hint in header_hints:
        score += 1 if hint == "cloudflare_cdn_header" else QUALITY_SCORE_THRESHOLD
    return score


def _has_suspicious_final_path(path: str) -> bool:
    return any(
        marker in path
        for marker in ("/login", "/captcha", "/challenge", "/verify", "/cdn-cgi/")
    )


def _low_or_generic_content(content_lower: str) -> bool:
    if len(content_lower) < 300:
        return True
    return _contains_any(
        content_lower,
        (
            "access denied",
            "attention required",
            "just a moment",
            "security check",
            "please verify",
            "checking your browser",
            "login required",
        ),
    )


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
