from __future__ import annotations

"""Conservative provider/runner recovery classification helpers.

This module is intentionally pure: it classifies persisted failure text/payloads
and formats local recovery notices, but it does not mutate thread state or call
``continue_thread``. Runner integration happens in a later phase.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from typing import Any, Dict, Optional

DEFAULT_TRANSPORT_DELAY_SEC = 2.0
DEFAULT_TIMEOUT_DELAY_SEC = 2.0
DEFAULT_SERVER_DELAY_SEC = 5.0
DEFAULT_RATE_LIMIT_DELAY_SEC = 5.0
DEFAULT_EMPTY_OR_INCOMPLETE_DELAY_SEC = 2.0
DEFAULT_MAX_RETRY_DELAY_SEC = 300.0

_RETRIABLE_SERVER_STATUS_CODES = {500, 502, 503, 504, 520, 524, 529}

_STATUS_PHRASES = {
    400: r"bad request",
    401: r"unauthorized|unauthorised",
    402: r"payment required",
    403: r"forbidden",
    429: r"too many requests",
    500: r"internal server error|server error",
    502: r"bad gateway",
    503: r"service unavailable|temporarily unavailable|overloaded",
    504: r"gateway timeout",
    520: r"unknown error",
    524: r"timeout occurred|a timeout occurred",
    529: r"overloaded|too many requests",
}


@dataclass(frozen=True)
class RecoveryDecision:
    """Decision returned by provider/runner recovery classification."""

    retriable: bool
    category: str
    reason: str
    source_summary: str
    delay_sec: Optional[float] = None
    stop_reason: Optional[str] = None

    @property
    def recoverable(self) -> bool:
        """Alias for callers that prefer recovery terminology."""

        return self.retriable


@dataclass(frozen=True)
class RecoverySource:
    """Persisted message that represents a recoverable/non-recoverable failure."""

    msg_id: str
    event_seq: int
    payload: Dict[str, Any]
    decision: RecoveryDecision


@dataclass(frozen=True)
class RecoveryFenceResult:
    ok: bool
    reason: str = ""


def _short_text(value: Any, *, limit: int = 240) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _payload_from_json(value: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(value) if isinstance(value, str) else (value or {})
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _retry(category: str, reason: str, source: str, delay_sec: float) -> RecoveryDecision:
    return RecoveryDecision(
        retriable=True,
        category=category,
        reason=reason,
        source_summary=_short_text(source),
        delay_sec=float(delay_sec),
        stop_reason=None,
    )


def _stop(category: str, reason: str, source: str, stop_reason: Optional[str] = None) -> RecoveryDecision:
    return RecoveryDecision(
        retriable=False,
        category=category,
        reason=reason,
        source_summary=_short_text(source),
        delay_sec=None,
        stop_reason=stop_reason or reason,
    )


def _unit_seconds(unit: Optional[str]) -> float:
    if not unit:
        return 1.0
    low = unit.lower()
    if low in {"m", "min", "mins", "minute", "minutes"}:
        return 60.0
    return 1.0


def _delay_from_number(value: str, unit: Optional[str]) -> Optional[float]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return amount * _unit_seconds(unit)


def _retry_after_header_delay(text: str, *, now: Optional[datetime]) -> Optional[float]:
    for line in text.splitlines():
        if not re.match(r"^\s*retry[- ]after\s*:", line, flags=re.IGNORECASE):
            continue
        value = line.split(":", 1)[1].strip()
        numeric = re.match(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s*$", value)
        if numeric:
            return _delay_from_number(numeric.group(1), numeric.group(2))
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        base = now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - base).total_seconds())
    return None


def parse_retry_delay_seconds(text: str, *, now: Optional[datetime] = None) -> Optional[float]:
    """Parse a provider-requested retry delay from text.

    Only parses numbers that are attached to retry-delay phrases, so HTTP
    status codes such as ``HTTP 503`` are not accidentally treated as seconds.
    """

    if not text:
        return None

    header_delay = _retry_after_header_delay(str(text), now=now)
    if header_delay is not None:
        return header_delay

    patterns = [
        r"\bretry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)?\b",
        r"\btry again in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)?\b",
        r"\bavailable in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)?\b",
        r"\bresets? in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, str(text), flags=re.IGNORECASE)
        if match:
            return _delay_from_number(match.group(1), match.group(2))
    return None


def _contains_any(low: str, needles: tuple[str, ...]) -> bool:
    return any(needle in low for needle in needles)


def _has_http_status(text: str, code: int) -> bool:
    phrase = _STATUS_PHRASES.get(code)
    patterns = [
        rf"\bhttp(?:\s+status|\s+code|\s+error)?\D{{0,20}}{code}\b",
        rf"\bstatus(?:\s+code)?\D{{0,20}}{code}\b",
        rf"\bresponse(?:\s+status)?\D{{0,20}}{code}\b",
        rf"\berror\D{{0,20}}{code}\b",
    ]
    if phrase:
        patterns.append(rf"\b{code}\s+(?:{phrase})\b")
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _has_any_http_status(text: str, codes: set[int]) -> bool:
    return any(_has_http_status(text, code) for code in codes)


def _excessive_delay_decision(category: str, reason: str, source: str, delay: float, max_delay_sec: float) -> RecoveryDecision:
    return _stop(
        category,
        reason,
        source,
        f"Provider requested retry delay {delay:g}s exceeds maximum {max_delay_sec:g}s",
    )


def _delay_or_default(text: str, fallback: float, max_delay_sec: float, category: str, reason: str) -> RecoveryDecision:
    parsed = parse_retry_delay_seconds(text)
    if parsed is not None:
        if parsed > max_delay_sec:
            return _excessive_delay_decision(category, reason, text, parsed, max_delay_sec)
        return _retry(category, reason, text, parsed)
    return _retry(category, reason, text, fallback)


def _is_transport_error(low: str) -> bool:
    return _contains_any(
        low,
        (
            "response payload is not completed",
            "transferencodingerror",
            "not enough data to satisfy transfer length header",
            "clientpayloaderror",
            "chunkedencodingerror",
            "incompleteread",
            "serverdisconnectederror",
            "connection reset",
            "connection aborted",
            "connection closed",
            "broken pipe",
            "remote protocol error",
            "early eof",
            "eof occurred in violation of protocol",
            "provider stream ended early",
            "stream ended early",
            "transport disconnect",
            "provider disconnect",
            "disconnected",
        ),
    )


def _is_timeout_error(low: str) -> bool:
    return _contains_any(
        low,
        (
            "timeouterror",
            "asyncio.timeouterror",
            "timed out",
            "timeout",
            "read timeout",
            "connect timeout",
            "no response",
        ),
    )


def _is_rate_limit_or_overload(low: str, text: str) -> bool:
    return (
        _has_http_status(text, 429)
        or _contains_any(low, ("rate limit", "rate-limit", "too many requests", "overloaded", "temporarily unavailable"))
    )


def _is_empty_assistant_error(low: str) -> bool:
    return "empty assistant message returned by provider" in low


def _is_max_output_truncation(low: str) -> bool:
    return _contains_any(
        low,
        (
            "max_output_tokens",
            "max output tokens",
            "maximum output tokens",
            "finish_reason=length",
            "finish_reason: length",
            "finish reason length",
            "finish reason: length",
            "stop reason length",
            "output token limit",
        ),
    )


def _is_context_length_error(low: str) -> bool:
    return _contains_any(
        low,
        (
            "context_length_exceeded",
            "context length",
            "context window",
            "maximum context",
            "prompt is too long",
            "too many tokens",
            "input token count",
        ),
    ) and _contains_any(low, ("exceed", "too long", "maximum", "limit", "context"))


def _is_auth_or_permission_error(low: str, text: str) -> bool:
    return (
        _has_any_http_status(text, {401, 403})
        or _contains_any(
            low,
            (
                "unauthorized",
                "unauthorised",
                "invalid api key",
                "api key invalid",
                "expired oauth",
                "permission denied",
                "forbidden",
                "forbidden model",
            ),
        )
    )


def _is_safety_error(low: str) -> bool:
    return _contains_any(low, ("content filter", "safety", "policy violation", "blocked by policy", "safety policy"))


def _is_quota_error(low: str, text: str) -> bool:
    return _has_http_status(text, 402) or _contains_any(
        low,
        ("quota", "billing", "insufficient credits", "insufficient credit", "payment required", "credit balance"),
    )


def _is_permanent_invalid_request(low: str) -> bool:
    return _contains_any(
        low,
        (
            "invalid_request_error",
            "invalid request",
            "invalid parameter",
            "unsupported parameter",
            "unsupported value",
            "invalid schema",
            "tool schema",
            "schema validation",
            "malformed request",
        ),
    )


def classify_failure_text(
    text: str,
    *,
    max_delay_sec: float = DEFAULT_MAX_RETRY_DELAY_SEC,
) -> RecoveryDecision:
    """Classify provider/runner failure text for conservative recovery."""

    source = str(text or "").strip()
    if not source:
        return _stop("none", "No failure text provided", source)
    low = source.lower()

    if _is_empty_assistant_error(low):
        return _delay_or_default(source, DEFAULT_EMPTY_OR_INCOMPLETE_DELAY_SEC, max_delay_sec, "empty_response", "Provider returned an empty assistant response")
    if _is_max_output_truncation(low):
        return _stop("max_output", "Assistant response stopped because of max output length", source)
    if _is_context_length_error(low):
        return _stop("context_length", "Context length exceeded; use compaction recovery", source)
    if _is_auth_or_permission_error(low, source):
        return _stop("auth", "Authentication or permission error is not recoverable by continue", source)
    if _is_safety_error(low):
        return _stop("safety", "Provider safety/content policy block is not recoverable by continue", source)
    if _is_quota_error(low, source):
        return _stop("quota", "Quota or billing error is not recoverable by continue", source)
    if _is_permanent_invalid_request(low):
        return _stop("invalid_request", "Permanent invalid request/schema error", source)

    # Transport checks intentionally precede generic HTTP 400 handling. Some
    # transient transport exceptions include a 400 in their nested text.
    if _is_transport_error(low):
        return _delay_or_default(source, DEFAULT_TRANSPORT_DELAY_SEC, max_delay_sec, "transport", "Transient transport/provider stream failure")
    if _is_timeout_error(low):
        return _delay_or_default(source, DEFAULT_TIMEOUT_DELAY_SEC, max_delay_sec, "timeout", "Provider request timed out or returned no response")
    if _is_rate_limit_or_overload(low, source):
        return _delay_or_default(source, DEFAULT_RATE_LIMIT_DELAY_SEC, max_delay_sec, "rate_limit", "Provider is rate-limited or overloaded")
    if _has_any_http_status(source, _RETRIABLE_SERVER_STATUS_CODES):
        return _delay_or_default(source, DEFAULT_SERVER_DELAY_SEC, max_delay_sec, "server_error", "Transient provider/server error")
    if _has_http_status(source, 400) or "bad request" in low:
        return _stop("bad_request", "Generic HTTP 400/bad request is not retriable by default", source)

    return _stop("unknown", "Failure did not match conservative retry policy", source)


def classify_failure_payload(
    payload: Dict[str, Any],
    *,
    max_delay_sec: float = DEFAULT_MAX_RETRY_DELAY_SEC,
) -> RecoveryDecision:
    """Classify a persisted msg.create payload for recovery."""

    if not isinstance(payload, dict):
        return _stop("none", "Payload is not a message object", "")
    if payload.get("recovery_notice"):
        return _stop("local_notice", "Recovery notices are local status, not provider failures", payload.get("content", ""))

    role = payload.get("role")
    content = payload.get("content")
    if role == "system" and isinstance(content, str) and content.strip():
        return classify_failure_text(content, max_delay_sec=max_delay_sec)

    incomplete_reason = payload.get("incomplete_reason")
    if payload.get("incomplete") or incomplete_reason:
        reason_text = str(incomplete_reason or "assistant message marked incomplete")
        low = reason_text.lower()
        if _is_max_output_truncation(low):
            return _stop("max_output", "Assistant response stopped because of max output length", reason_text)
        classified = classify_failure_text(reason_text, max_delay_sec=max_delay_sec)
        if classified.retriable:
            return classified
        if _contains_any(low, ("provider", "transport", "stream ended early", "ended early", "disconnect")):
            return _retry("incomplete", "Assistant response was incomplete for a provider/transport reason", reason_text, DEFAULT_EMPTY_OR_INCOMPLETE_DELAY_SEC)
        return _stop("incomplete", "Assistant message is incomplete but reason is not transient", reason_text)

    return _stop("none", "Payload does not describe a provider/runner failure", content or "")


def find_latest_recovery_source_after(
    db: Any,
    thread_id: str,
    after_event_seq: int,
    *,
    max_delay_sec: float = DEFAULT_MAX_RETRY_DELAY_SEC,
) -> Optional[RecoverySource]:
    """Return the latest provider/runner failure message after a RA1 trigger."""

    try:
        rows = db.conn.execute(
            "SELECT event_seq, msg_id, payload_json FROM events "
            "WHERE thread_id=? AND type='msg.create' AND event_seq>? ORDER BY event_seq ASC",
            (thread_id, int(after_event_seq)),
        ).fetchall()
    except Exception:
        return None

    latest: Optional[RecoverySource] = None
    for event_seq, msg_id, payload_json in rows:
        if not msg_id:
            continue
        if message_is_skipped(db, thread_id, str(msg_id)):
            continue
        payload = _payload_from_json(payload_json)
        decision = classify_failure_payload(payload, max_delay_sec=max_delay_sec)
        if decision.category in {"none", "local_notice"}:
            continue
        latest = RecoverySource(str(msg_id), int(event_seq), payload, decision)
    return latest


def recovery_attempt_count(db: Any, thread_id: str, trigger_msg_id: Optional[str]) -> int:
    if not trigger_msg_id:
        return 0
    count = 0
    try:
        rows = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
            (thread_id,),
        ).fetchall()
    except Exception:
        return 0
    for (payload_json,) in rows:
        payload = _payload_from_json(payload_json)
        if not payload.get("recovery_notice"):
            continue
        if payload.get("auto_continue") is not True:
            continue
        if payload.get("action") != "applied":
            continue
        if payload.get("trigger_msg_id") == trigger_msg_id:
            count += 1
    return count


def message_is_skipped(db: Any, thread_id: str, msg_id: str) -> bool:
    try:
        rows = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.edit' AND msg_id=? ORDER BY event_seq ASC",
            (thread_id, msg_id),
        ).fetchall()
    except Exception:
        return False
    for (payload_json,) in rows:
        payload = _payload_from_json(payload_json)
        if payload.get("skipped_on_continue"):
            return True
    return False


def check_recovery_fence(
    db: Any,
    thread_id: str,
    *,
    trigger_msg_id: Optional[str],
    source_msg_id: str,
    source_event_seq: int,
    max_attempts: int = 1,
) -> RecoveryFenceResult:
    """Validate that delayed auto-continue is still safe to apply."""

    try:
        if db.current_open(thread_id) is not None:
            return RecoveryFenceResult(False, "thread has an active runner lease")
    except Exception:
        return RecoveryFenceResult(False, "could not verify runner lease")

    try:
        row = db.conn.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND type='msg.create' AND msg_id=? LIMIT 1",
            (thread_id, source_msg_id),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return RecoveryFenceResult(False, "source failure message no longer exists")
    if message_is_skipped(db, thread_id, source_msg_id):
        return RecoveryFenceResult(False, "thread was continued manually before the retry fired")

    try:
        rows = db.conn.execute(
            "SELECT payload_json FROM events "
            "WHERE thread_id=? AND type='control.interrupt' AND event_seq>? ORDER BY event_seq ASC",
            (thread_id, int(source_event_seq)),
        ).fetchall()
    except Exception:
        rows = []
    for (payload_json,) in rows:
        payload = _payload_from_json(payload_json)
        if payload.get("purpose") == "continue":
            return RecoveryFenceResult(False, "thread was continued manually before the retry fired")

    if recovery_attempt_count(db, thread_id, trigger_msg_id) >= max_attempts:
        return RecoveryFenceResult(False, "automatic continue attempt cap reached")

    try:
        rows = db.conn.execute(
            "SELECT msg_id, payload_json FROM events "
            "WHERE thread_id=? AND type='msg.create' AND event_seq>? ORDER BY event_seq ASC",
            (thread_id, int(source_event_seq)),
        ).fetchall()
    except Exception:
        return RecoveryFenceResult(False, "could not inspect newer messages")
    for msg_id, payload_json in rows:
        payload = _payload_from_json(payload_json)
        if payload.get("recovery_notice") or payload.get("no_api"):
            continue
        role = payload.get("role")
        tool_calls = payload.get("tool_calls") or []
        keep_user_turn = bool(payload.get("keep_user_turn"))
        if role == "user" and not tool_calls and not keep_user_turn:
            return RecoveryFenceResult(False, "newer user trigger message appeared")
        if role == "tool" and not keep_user_turn:
            return RecoveryFenceResult(False, "newer tool trigger message appeared")
        if role == "assistant":
            return RecoveryFenceResult(False, "newer assistant/provider result appeared")
        if role == "system":
            decision = classify_failure_payload(payload)
            if decision.category not in {"none", "local_notice"}:
                return RecoveryFenceResult(False, "newer provider failure appeared")

    return RecoveryFenceResult(True)


def format_retry_delay(delay_sec: Optional[float]) -> str:
    if delay_sec is None:
        return "no retry delay"
    if float(delay_sec).is_integer():
        return f"{int(delay_sec)}s"
    return f"{delay_sec:.1f}s"


def format_recovery_decision_notice(decision: RecoveryDecision, *, source: str = "auto-continue") -> str:
    """Format a simple local recovery notice for a classification decision."""

    lines = [f"Recovery: {source}."]
    if decision.retriable:
        lines.append(f"Decision: retry ({decision.category}) after {format_retry_delay(decision.delay_sec)}.")
    else:
        lines.append(f"Decision: stop ({decision.category}).")
    lines.append(f"Reason: {decision.reason}")
    if decision.stop_reason and not decision.retriable:
        lines.append(f"Stop reason: {decision.stop_reason}")
    if decision.source_summary:
        lines.append(f"Source: {decision.source_summary}")
    return "\n".join(lines)


def format_auto_continue_notice(
    decision: RecoveryDecision,
    *,
    action: str,
    trigger_msg_id: Optional[str] = None,
    source_msg_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> str:
    """Format a simple auto-continue status notice."""

    lines = [f"Recovery: auto-continue {action}."]
    if trigger_msg_id:
        lines.append(f"Trigger: {trigger_msg_id[-8:]} ({trigger_msg_id}).")
    if source_msg_id:
        lines.append(f"Source failure: {source_msg_id[-8:]} ({source_msg_id}).")
    if decision.retriable:
        lines.append(f"Decision: retry ({decision.category}) after {format_retry_delay(decision.delay_sec)}.")
    else:
        lines.append(f"Decision: stop ({decision.category}).")
    lines.append(f"Reason: {decision.reason}")
    if decision.stop_reason and not decision.retriable:
        lines.append(f"Stop reason: {decision.stop_reason}")
    if detail:
        lines.append(f"Detail: {detail}")
    if decision.source_summary:
        lines.append(f"Source: {decision.source_summary}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_EMPTY_OR_INCOMPLETE_DELAY_SEC",
    "DEFAULT_MAX_RETRY_DELAY_SEC",
    "DEFAULT_RATE_LIMIT_DELAY_SEC",
    "DEFAULT_SERVER_DELAY_SEC",
    "DEFAULT_TIMEOUT_DELAY_SEC",
    "DEFAULT_TRANSPORT_DELAY_SEC",
    "RecoveryFenceResult",
    "RecoveryDecision",
    "RecoverySource",
    "check_recovery_fence",
    "classify_failure_payload",
    "classify_failure_text",
    "find_latest_recovery_source_after",
    "format_auto_continue_notice",
    "format_recovery_decision_notice",
    "format_retry_delay",
    "message_is_skipped",
    "parse_retry_delay_seconds",
    "recovery_attempt_count",
]
