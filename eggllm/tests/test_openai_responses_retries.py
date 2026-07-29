"""Retry behavior for the OpenAI Responses API adapter."""

import asyncio
import json
import sys

import pytest
import requests

from eggllm.providers import openai_responses as responses_provider
from eggllm.providers.openai_responses import OpenAIResponsesAdapter


URL = "https://example.test/v1/responses"
PAYLOAD = {
    "model": "gpt-test",
    "messages": [{"role": "user", "content": "Hi"}],
}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(responses_provider, "_sleep_sync", lambda _delay: None)

    async def _no_async_wait(_delay):
        return None

    monkeypatch.setattr(responses_provider, "_sleep_async", _no_async_wait)


def _completed_events(text="ok"):
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed"},
    ]


def _overloaded_event():
    return {
        "type": "response.failed",
        "response": {
            "error": {
                "code": "server_error",
                "message": "Our servers are currently overloaded",
            }
        },
    }


def _processing_error_event():
    return {
        "type": "error",
        "error": {
            "code": "unknown",
            "message": "An error occurred while processing your request",
        },
    }


class _SyncResponse:
    def __init__(self, status=200, events=(), body=""):
        self.status_code = status
        self.text = body
        self.events = list(events)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_lines(self):
        for event in self.events:
            yield ("data: " + json.dumps(event)).encode("utf-8")

    def close(self):
        self.closed = True


class _SyncSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def post(self, *_args, **_kwargs):
        self.calls += 1
        if not self.results:
            raise AssertionError("unexpected HTTP attempt")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _AsyncClientError(Exception):
    pass


class _AsyncContent:
    def __init__(self, events):
        self.lines = [
            ("data: " + json.dumps(event)).encode("utf-8")
            for event in events
        ]

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""


class _AsyncResponse:
    def __init__(self, status=200, events=(), body=""):
        self.status = status
        self.content = _AsyncContent(events)
        self.body = body
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def text(self):
        return self.body


class _AsyncSession:
    def __init__(self, aiohttp):
        self.aiohttp = aiohttp

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    def post(self, *_args, **_kwargs):
        self.aiohttp.calls += 1
        if not self.aiohttp.results:
            raise AssertionError("unexpected HTTP attempt")
        result = self.aiohttp.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _AsyncAiohttp:
    ClientError = _AsyncClientError

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    @staticmethod
    def ClientTimeout(*_args, **_kwargs):
        return None

    def ClientSession(self, *_args, **_kwargs):
        return _AsyncSession(self)


def _collect_async(adapter):
    async def collect():
        return [
            event
            async for event in adapter.stream_async(URL, {}, PAYLOAD)
        ]

    return asyncio.run(collect())


def test_codex_retry_defaults_and_backoff(monkeypatch):
    assert responses_provider.DEFAULT_REQUEST_MAX_RETRIES == 4
    assert responses_provider.DEFAULT_STREAM_MAX_RETRIES == 5

    monkeypatch.setattr(responses_provider.random, "uniform", lambda _low, _high: 1.0)
    assert [
        responses_provider._retry_delay_seconds(retry)
        for retry in range(1, 6)
    ] == [0.2, 0.4, 0.8, 1.6, 3.2]


def test_sync_http_retries_four_times_before_success():
    session = _SyncSession(
        [_SyncResponse(503, body="busy") for _ in range(4)]
        + [_SyncResponse(events=_completed_events())]
    )

    output = list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 5
    assert output[-1]["message"]["content"] == "ok"


def test_sync_http_stops_after_four_retries():
    session = _SyncSession(
        [_SyncResponse(503, body="busy") for _ in range(5)]
    )

    with pytest.raises(RuntimeError, match="HTTP 503"):
        list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 5


def test_sync_transport_retries_four_times_before_success():
    session = _SyncSession(
        [requests.ConnectionError("disconnected") for _ in range(4)]
        + [_SyncResponse(events=_completed_events())]
    )

    output = list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 5
    assert output[-1]["message"]["content"] == "ok"


def test_sync_http_does_not_retry_429():
    session = _SyncSession([_SyncResponse(429, body="slow down")])

    with pytest.raises(RuntimeError, match="HTTP 429"):
        list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 1


def test_sync_stream_retries_five_times_before_success():
    session = _SyncSession(
        [_SyncResponse(events=[_overloaded_event()]) for _ in range(5)]
        + [_SyncResponse(events=_completed_events("recovered"))]
    )

    output = list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 6
    assert output == [
        {"type": "content_delta", "text": "recovered"},
        {"type": "done", "message": {"role": "assistant", "content": "recovered"}},
    ]


def test_sync_stream_retries_processing_error_phrase():
    session = _SyncSession(
        [_SyncResponse(events=[_processing_error_event()]) for _ in range(5)]
        + [_SyncResponse(events=_completed_events("recovered"))]
    )

    output = list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 6
    assert output[-1]["message"]["content"] == "recovered"


def test_sync_stream_stops_after_five_retries():
    session = _SyncSession(
        [_SyncResponse(events=[_overloaded_event()]) for _ in range(6)]
    )

    with pytest.raises(RuntimeError, match="currently overloaded"):
        list(OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session))

    assert session.calls == 6


def test_sync_stream_is_not_replayed_after_partial_output():
    session = _SyncSession(
        [
            _SyncResponse(
                events=[
                    {"type": "response.output_text.delta", "delta": "partial"},
                    _overloaded_event(),
                ]
            )
        ]
    )
    stream = OpenAIResponsesAdapter().stream(URL, {}, PAYLOAD, session=session)

    assert next(stream) == {"type": "content_delta", "text": "partial"}
    with pytest.raises(RuntimeError, match="currently overloaded"):
        next(stream)

    assert session.calls == 1


def test_async_http_retries_four_times_before_success(monkeypatch):
    aiohttp = _AsyncAiohttp(
        [_AsyncResponse(503, body="busy") for _ in range(4)]
        + [_AsyncResponse(events=_completed_events())]
    )
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)

    output = _collect_async(OpenAIResponsesAdapter())

    assert aiohttp.calls == 5
    assert output[-1]["message"]["content"] == "ok"


def test_async_transport_retries_four_times_before_success(monkeypatch):
    aiohttp = _AsyncAiohttp(
        [_AsyncClientError("disconnected") for _ in range(4)]
        + [_AsyncResponse(events=_completed_events())]
    )
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)

    output = _collect_async(OpenAIResponsesAdapter())

    assert aiohttp.calls == 5
    assert output[-1]["message"]["content"] == "ok"


def test_async_stream_retries_five_times_before_success(monkeypatch):
    aiohttp = _AsyncAiohttp(
        [_AsyncResponse(events=[_overloaded_event()]) for _ in range(5)]
        + [_AsyncResponse(events=_completed_events("recovered"))]
    )
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)

    output = _collect_async(OpenAIResponsesAdapter())

    assert aiohttp.calls == 6
    assert output[-1]["message"]["content"] == "recovered"
