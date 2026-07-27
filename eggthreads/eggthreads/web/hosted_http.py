from __future__ import annotations

import json
import re
import zlib

from .base import WebBackendError, bound_text


_ERROR_WIRE_MAX_BYTES = 4096
_ERROR_DECODED_MAX_BYTES = 4096
_ERROR_DETAIL_MAX_CHARS = 400
_SUPPORTED_CONTENT_ENCODINGS = {"", "identity", "gzip", "x-gzip", "deflate"}
_NEGATED_OR_QUALIFIED_RE = re.compile(
    r"\b(?:nearly|almost|might|may|could|would|not|never|no\s+longer)\b",
    re.IGNORECASE,
)
# Tavily currently returns these phrases for account/plan exhaustion. Providers
# may opt into this semantic classifier only after their response semantics are
# verified; Parallel currently relies on documented HTTP retry statuses only.
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


def hosted_http_error(
    response: object,
    *,
    provider: str,
    provider_label: str,
    reserved_quota_statuses: set[int] | frozenset[int] = frozenset(),
    semantic_quota_statuses: set[int] | frozenset[int] = frozenset({402, 403}),
    quota_details_enabled: bool = True,
) -> WebBackendError:
    """Classify a hosted-provider HTTP failure with strictly bounded body work."""

    try:
        status_code = getattr(response, "status_code", None)
    except BaseException:
        status_code = None
    if not isinstance(status_code, int):
        status_code = 0

    quota_exhausted = status_code in reserved_quota_statuses
    try:
        wire, wire_complete = _read_error_wire(response)
        decoded, decoded_complete = _decode_error_wire(
            wire,
            wire_complete=wire_complete,
            content_encoding=_header_value(response, "Content-Encoding"),
        )
        semantic_details, diagnostic = _error_details(decoded, complete=decoded_complete)
    except BaseException:
        semantic_details, diagnostic = [], ""
    finally:
        close_response(response)

    if (
        quota_details_enabled
        and not quota_exhausted
        and status_code in semantic_quota_statuses
    ):
        quota_exhausted = any(_is_usage_limit_detail(detail) for detail in semantic_details)

    retriable = not quota_exhausted and (status_code == 429 or status_code >= 500)
    diagnostics = {
        "status_code": status_code,
        "response_detail": diagnostic,
    }
    if quota_exhausted:
        diagnostics["failure_kind"] = "quota_exhausted"
    suffix = f": {diagnostic}" if diagnostic else ""
    return WebBackendError(
        f"{provider_label} API status {status_code}{suffix}",
        provider=provider,
        retriable=retriable,
        fallback_eligible=quota_exhausted or retriable,
        status_code=status_code,
        diagnostics=diagnostics,
    )


def tavily_http_error(response: object, *, provider: str) -> WebBackendError:
    """Preserve Tavily's verified reserved and semantic quota handling."""

    return hosted_http_error(
        response,
        provider=provider,
        provider_label="Tavily",
        reserved_quota_statuses=frozenset({432, 433}),
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
    trailing_data = bool(decoder.unused_data)
    if not decoder.eof or overflow or trailing_data:
        return out[:_ERROR_DECODED_MAX_BYTES], False
    flushed = decoder.flush(_ERROR_DECODED_MAX_BYTES + 1 - len(out))
    out += flushed
    return out[:_ERROR_DECODED_MAX_BYTES], len(out) <= _ERROR_DECODED_MAX_BYTES


def _decode_error_wire(
    wire: bytes,
    *,
    wire_complete: bool,
    content_encoding: str,
) -> tuple[bytes, bool]:
    encoding = content_encoding.strip().lower()
    if encoding not in _SUPPORTED_CONTENT_ENCODINGS or "," in encoding:
        return b"", False
    if encoding in ("", "identity"):
        return wire[:_ERROR_DECODED_MAX_BYTES], wire_complete and len(wire) <= _ERROR_DECODED_MAX_BYTES
    if not wire_complete:
        return b"", False
    try:
        if encoding in ("gzip", "x-gzip"):
            return _bounded_decompress(wire, wbits=16 + zlib.MAX_WBITS)
        try:
            return _bounded_decompress(wire, wbits=zlib.MAX_WBITS)
        except zlib.error:
            return _bounded_decompress(wire, wbits=-zlib.MAX_WBITS)
    except (zlib.error, ValueError, MemoryError):
        return b"", False


def _decode_error_prefix(prefix: bytes, *, complete: bool) -> tuple[str, bool]:
    if not prefix:
        return "", not complete
    try:
        text = prefix.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = prefix.decode("utf-8", errors="replace")
        complete = False
    return text, not complete


def _collect_json_error_strings(value: object) -> list[str]:
    out: list[str] = []
    if not isinstance(value, dict):
        return out
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


def _is_usage_limit_detail(detail: str) -> bool:
    normalized = " ".join(detail.strip().split())
    if _NEGATED_OR_QUALIFIED_RE.search(normalized):
        return False
    return bool(
        _REQUEST_PLAN_LIMIT_RE.fullmatch(normalized)
        or _PROVIDER_QUOTA_RE.fullmatch(normalized)
    )


def close_response(response: object) -> None:
    try:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except BaseException:
        pass


__all__ = ["close_response", "hosted_http_error", "tavily_http_error"]
