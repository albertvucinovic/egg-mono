from __future__ import annotations

from datetime import datetime, timezone

from eggthreads.runner_recovery import (
    RecoveryDecision,
    classify_failure_payload,
    classify_failure_text,
    format_recovery_decision_notice,
    parse_retry_delay_seconds,
)


def assert_retriable(decision: RecoveryDecision, category: str) -> None:
    assert decision.retriable is True
    assert decision.recoverable is True
    assert decision.category == category
    assert decision.delay_sec is not None
    assert decision.stop_reason is None


def assert_non_retriable(decision: RecoveryDecision, category: str) -> None:
    assert decision.retriable is False
    assert decision.recoverable is False
    assert decision.category == category
    assert decision.delay_sec is None
    assert decision.stop_reason


def test_classifies_transfer_encoding_400_as_retriable_transport() -> None:
    text = (
        "Response payload is not completed "
        "<TransferEncodingError: 400, message='Not enough data to satisfy transfer length header.'>"
    )

    decision = classify_failure_text(text)

    assert_retriable(decision, "transport")
    assert decision.delay_sec == 2.0
    assert "transport" in decision.reason.lower()


def test_classifies_timeout_as_retriable() -> None:
    decision = classify_failure_text("asyncio.TimeoutError: provider read timeout")

    assert_retriable(decision, "timeout")
    assert decision.delay_sec == 2.0


def test_classifies_5xx_statuses_as_retriable() -> None:
    for status in (500, 502, 503, 504, 520, 524, 529):
        decision = classify_failure_text(f"HTTP {status}: transient provider failure")
        assert_retriable(decision, "server_error")
        assert decision.delay_sec == 5.0


def test_classifies_rate_limit_with_acceptable_delay_as_retriable() -> None:
    decision = classify_failure_text("HTTP 429 rate limit exceeded, retry after 30 seconds")

    assert_retriable(decision, "rate_limit")
    assert decision.delay_sec == 30.0


def test_classifies_rate_limit_with_excessive_delay_as_non_retry() -> None:
    decision = classify_failure_text("HTTP 429 too many requests; retry after 10 minutes", max_delay_sec=300)

    assert_non_retriable(decision, "rate_limit")
    assert "exceeds maximum" in decision.stop_reason


def test_excludes_generic_400_and_permanent_errors() -> None:
    cases = [
        ("HTTP 400 Bad Request", "bad_request"),
        ("HTTP 401 unauthorized invalid api key", "auth"),
        ("HTTP 403 forbidden model", "auth"),
        ("context_length_exceeded: maximum context length exceeded", "context_length"),
        ("invalid request: unsupported parameter tool_choice", "invalid_request"),
        ("invalid schema for tool call", "invalid_request"),
        ("content filter safety policy blocked this request", "safety"),
        ("quota exceeded: billing hard limit reached", "quota"),
        ("finish_reason=length because max_output_tokens was reached", "max_output"),
    ]

    for text, category in cases:
        decision = classify_failure_text(text)
        assert_non_retriable(decision, category)


def test_empty_assistant_system_payload_is_retriable() -> None:
    decision = classify_failure_payload(
        {"role": "system", "content": "LLM error: empty assistant message returned by provider"}
    )

    assert_retriable(decision, "empty_response")
    assert decision.delay_sec == 2.0


def test_incomplete_payload_with_transport_reason_is_retriable() -> None:
    decision = classify_failure_payload(
        {
            "role": "assistant",
            "content": "partial",
            "incomplete": True,
            "incomplete_reason": "provider stream ended early: ServerDisconnectedError",
        }
    )

    assert_retriable(decision, "transport")


def test_incomplete_payload_with_max_output_reason_is_not_retriable() -> None:
    decision = classify_failure_payload(
        {
            "role": "assistant",
            "content": "partial",
            "incomplete": True,
            "incomplete_reason": "finish_reason=length max_output_tokens reached",
        }
    )

    assert_non_retriable(decision, "max_output")


def test_incomplete_payload_without_transient_reason_is_not_retriable() -> None:
    decision = classify_failure_payload({"role": "assistant", "content": "partial", "incomplete": True})

    assert_non_retriable(decision, "incomplete")


def test_recovery_notice_payload_is_ignored() -> None:
    decision = classify_failure_payload(
        {"role": "system", "content": "LLM/runner error: old failure", "recovery_notice": True}
    )

    assert_non_retriable(decision, "local_notice")


def test_parse_retry_delay_phrases_without_treating_status_codes_as_delays() -> None:
    assert parse_retry_delay_seconds("HTTP 503 service unavailable") is None
    assert parse_retry_delay_seconds("retry after 30 seconds") == 30.0
    assert parse_retry_delay_seconds("try again in 2m") == 120.0
    assert parse_retry_delay_seconds("available in 60s") == 60.0
    assert parse_retry_delay_seconds("resets in 45 seconds") == 45.0
    assert parse_retry_delay_seconds("Retry-After: 90") == 90.0


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    assert parse_retry_delay_seconds(
        "Retry-After: Thu, 01 Jan 2026 00:02:00 GMT",
        now=now,
    ) == 120.0


def test_format_recovery_decision_notice_for_retry_and_stop() -> None:
    retry = classify_failure_text("HTTP 503 Service Unavailable")
    retry_notice = format_recovery_decision_notice(retry, source="auto-continue")
    assert "Decision: retry (server_error) after 5s." in retry_notice
    assert "Source: HTTP 503 Service Unavailable" in retry_notice

    stop = classify_failure_text("HTTP 400 Bad Request")
    stop_notice = format_recovery_decision_notice(stop, source="auto-continue")
    assert "Decision: stop (bad_request)." in stop_notice
    assert "Stop reason:" in stop_notice
