from __future__ import annotations

import pytest

from eggthreads.web import WebBackendError
from eggthreads.web.parallel_ai import ParallelBackend


class _BytesRaw:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0
        self.calls = []
        self.decode_content = True

    def read(self, size=-1):
        self.calls.append(size)
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class _MockResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        import json

        self.status_code = status_code
        self._payload = payload
        self.text = text
        body = json.dumps(payload).encode() if payload is not None else text.encode()
        self.raw = _BytesRaw(body)
        self.headers = {}
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload

    def close(self):
        self.closed = True


def test_parallel_search_request_and_response(monkeypatch):
    calls = []
    response = _MockResponse(200, {
        "results": [
            {
                "title": "Dogs",
                "url": "https://example.com/dogs",
                "excerpts": ["First excerpt", "Second excerpt"],
            },
            {"url": "https://example.com/empty", "excerpts": []},
        ]
    })

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append((url, json, headers, timeout, stream))
        return response

    import requests
    monkeypatch.setattr(requests, "post", mock_post)

    result = ParallelBackend(api_key="parallel-test").search_response("dogs", max_results=10)

    assert calls == [(
        "https://api.parallel.ai/v1/search",
        {
            "search_queries": ["dogs"],
            "mode": "advanced",
            "advanced_settings": {"max_results": 10},
        },
        {"Content-Type": "application/json", "x-api-key": "parallel-test"},
        30,
        True,
    )]
    assert [(item.title, item.url, item.snippet) for item in result.results] == [
        ("Dogs", "https://example.com/dogs", "First excerpt\n\nSecond excerpt"),
        ("", "https://example.com/empty", ""),
    ]
    assert [attempt.provider for attempt in result.attempts] == ["parallel"]
    assert response.closed is True


def test_parallel_extract_request_and_response(monkeypatch):
    calls = []
    response = _MockResponse(200, {
        "results": [{
            "url": "https://example.com/final",
            "excerpts": ["Focused excerpt"],
            "full_content": None,
        }],
        "errors": [],
    })

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append((url, json, headers, timeout, stream))
        return response

    import requests
    monkeypatch.setattr(requests, "post", mock_post)

    result = ParallelBackend(api_key="parallel-test").fetch_response("https://example.com")

    assert calls == [(
        "https://api.parallel.ai/v1/extract",
        {
            "urls": ["https://example.com"],
            "advanced_settings": {"full_content": False},
        },
        {"Content-Type": "application/json", "x-api-key": "parallel-test"},
        30,
        True,
    )]
    assert result.final_url == "https://example.com/final"
    assert result.content == "Focused excerpt"
    assert result.content_type == "text/markdown"
    assert response.closed is True


def test_parallel_extract_joins_default_excerpts(monkeypatch):
    response = _MockResponse(200, {
        "results": [{
            "url": "https://example.com",
            "excerpts": ["One", "Two"],
            "full_content": None,
        }],
        "errors": [],
    })

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    result = ParallelBackend(api_key="parallel-test").fetch_response("https://example.com")

    assert result.content == "One\n\nTwo"
    assert response.closed is True


def test_parallel_extract_accepts_full_content_when_returned(monkeypatch):
    response = _MockResponse(200, {
        "results": [{
            "url": "https://example.com",
            "excerpts": ["Focused excerpt"],
            "full_content": "# Full content",
        }],
        "errors": [],
    })

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    result = ParallelBackend(api_key="parallel-test").fetch_response(
        "https://example.com"
    )

    assert result.content == "# Full content"
    assert response.closed is True


def test_parallel_extract_error_is_structured_and_degraded(monkeypatch):
    response = _MockResponse(200, {
        "results": [],
        "errors": [{
            "url": "https://example.com",
            "error_type": "fetch_error",
            "http_status_code": 403,
            "content": "blocked",
        }],
    })

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(WebBackendError) as exc_info:
        ParallelBackend(api_key="parallel-test").fetch_response("https://example.com")

    error = exc_info.value
    assert error.provider == "parallel"
    assert error.retriable is True
    assert error.fallback_eligible is True
    assert error.degraded is True
    assert error.diagnostics["extract_error"]["error_type"] == "fetch_error"
    assert response.closed is True


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_parallel_requires_api_key(operation):
    backend = ParallelBackend(api_key="")
    with pytest.raises(WebBackendError, match="PARALLEL_API_KEY"):
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")


@pytest.mark.parametrize("operation", ["search", "extract"])
@pytest.mark.parametrize(
    "status_code, expected_retriable",
    [(401, False), (422, False), (429, True), (503, True)],
)
def test_parallel_http_error_classification_and_closure(
    monkeypatch, operation, status_code, expected_retriable
):
    response = _MockResponse(
        status_code,
        {"type": "error", "error": {"message": "provider failure"}},
    )

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(WebBackendError) as exc_info:
        backend = ParallelBackend(api_key="parallel-test")
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")

    error = exc_info.value
    assert error.status_code == status_code
    assert error.retriable is expected_retriable
    assert error.fallback_eligible is expected_retriable
    assert error.diagnostics == {
        "status_code": status_code,
        "response_detail": "provider failure",
    }
    assert response.closed is True


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_parallel_insufficient_credit_is_fallback_eligible_not_retriable(
    monkeypatch, operation
):
    response = _MockResponse(
        402,
        {"error": {"message": "Payment required: insufficient credit in account"}},
    )

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(WebBackendError) as exc_info:
        backend = ParallelBackend(api_key="parallel-test")
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")

    error = exc_info.value
    assert error.status_code == 402
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics["failure_kind"] == "quota_exhausted"
    assert response.closed is True


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_parallel_non_json_success_is_retriable_and_closed(monkeypatch, operation):
    response = _MockResponse(200, text="not json")

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(WebBackendError) as exc_info:
        backend = ParallelBackend(api_key="parallel-test")
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")

    assert exc_info.value.retriable is True
    assert response.closed is True


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_parallel_error_body_is_bounded_and_streamed(monkeypatch, operation):
    body = b'{"error":{"message":"permission denied"}}' + (b"x" * 100_000)

    class Response:
        status_code = 403
        headers = {}
        raw = _BytesRaw(body)
        closed = False

        @property
        def text(self):
            raise AssertionError("streamed error handling must not access response.text")

        def close(self):
            self.closed = True

    response = Response()
    calls = []

    def mock_post(*args, **kwargs):
        calls.append(kwargs.get("stream"))
        return response

    import requests
    monkeypatch.setattr(requests, "post", mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        backend = ParallelBackend(api_key="parallel-test")
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")

    error = exc_info.value
    assert error.retriable is False
    assert error.fallback_eligible is False
    assert len(error.diagnostics["response_detail"]) <= 400
    assert response.raw.offset <= 4097
    assert response.closed is True
    assert calls == [True]


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_parallel_missing_result_fields_are_degraded(monkeypatch, operation):
    response = _MockResponse(200, {"session_id": "session"})

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(WebBackendError) as exc_info:
        backend = ParallelBackend(api_key="parallel-test")
        if operation == "search":
            backend.search_response("dogs")
        else:
            backend.fetch_response("https://example.com")

    assert exc_info.value.retriable is True
    assert exc_info.value.degraded is True
    assert response.closed is True


