from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools
from eggthreads.web import WebBackendError
from eggthreads.web.tavily import TavilyBackend


class _MockResponse:
    def __init__(self, status_code: int, payload=None, text: str = ''):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        if self._payload is None:
            raise ValueError('no json payload')
        return self._payload


@pytest.fixture
def tools():
    return create_default_tools()


@pytest.fixture(autouse=True)
def _force_tavily_backend(monkeypatch):
    # The web_search / fetch_url tools dispatch through the currently
    # selected web provider/orchestrator; pin them to Tavily for this file.
    monkeypatch.setenv('EGG_WEB_BACKEND', 'tavily')


def test_fetch_url_requires_api_key(monkeypatch, tools):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)

    result = tools.execute('fetch_url', {'url': 'https://example.com'})

    assert 'TAVILY_API_KEY' in result


def test_fetch_url_requires_url(monkeypatch, tools):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    result = tools.execute('fetch_url', {})

    assert '"url" is required' in result


def test_tavily_backend_uses_simple_markdown_request(monkeypatch, tools):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append({
            'url': url,
            'json': json,
            'headers': headers,
            'timeout': timeout,
        })
        return _MockResponse(200, {
            'results': [
                {
                    'url': 'https://example.com',
                    'raw_content': '# Title\n\nBody text',
                }
            ],
            'failed_results': [],
        })

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    result = tools.execute('fetch_url', {'url': 'https://example.com'})

    assert len(calls) == 1
    call = calls[0]
    assert call['url'] == 'https://api.tavily.com/extract'
    assert call['json'] == {
        'urls': ['https://example.com'],
        'format': 'markdown',
    }
    assert call['headers']['Authorization'] == 'Bearer tvly-test'
    assert call['timeout'] == 30

    assert 'URL: https://example.com' in result
    assert '# Title' in result
    assert 'Body text' in result


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_tavily_success_requests_stream_and_close_response(monkeypatch, operation):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')
    calls = []
    response = _MockResponse(
        200,
        {'results': ([
            {'title': 'A', 'url': 'https://a.example', 'content': 'x'},
        ] if operation == "search" else [
            {'url': 'https://a.example', 'raw_content': 'body'},
        ])},
    )

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append((url, stream))
        return response

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    if operation == "search":
        TavilyBackend().search_response('x')
        expected_url = TavilyBackend.SEARCH_URL
    else:
        TavilyBackend().fetch_response('https://a.example')
        expected_url = TavilyBackend.EXTRACT_URL

    assert calls == [(expected_url, True)]
    assert response.closed is True


class _CountingRaw:
    def __init__(self, prefix: bytes, tail_size: int):
        self.prefix = prefix
        self.tail_size = tail_size
        self.bytes_read = 0
        self.calls = []

    def read(self, size=-1):
        self.calls.append(size)
        if self.bytes_read:
            return b""
        # Simulate a huge body without allocating its unread tail.
        chunk = (self.prefix + (b"x" * max(0, size - len(self.prefix))))[:size]
        self.bytes_read += len(chunk)
        return chunk


class _StreamingErrorResponse:
    def __init__(self, status_code: int, prefix: bytes, tail_size: int = 20_000_000):
        self.status_code = status_code
        self.raw = _CountingRaw(prefix, tail_size)
        self.closed = False

    @property
    def text(self):
        raise AssertionError("streamed error classification must not access response.text")

    def json(self):
        raise AssertionError("streamed error classification must not decode full JSON")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_tavily_error_reads_bounded_stream_prefix_and_closes(monkeypatch, operation):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')
    response = _StreamingErrorResponse(432, b'{"detail":"Plan usage limit exceeded"}')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        assert stream is True
        return response

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        if operation == "search":
            TavilyBackend().search_response('x')
        else:
            TavilyBackend().fetch_response('https://a.example')

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert response.raw.bytes_read == 4096
    assert response.raw.calls == [4096]
    assert response.closed is True
    assert len(error.diagnostics['response_detail']) <= 400


def test_tavily_backend_formats_failed_result(monkeypatch, tools):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(200, {
            'results': [],
            'failed_results': [
                {
                    'url': 'https://bad.example.com',
                    'error': 'timeout',
                }
            ],
        })

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    result = tools.execute('fetch_url', {'url': 'https://bad.example.com'})

    assert 'failed to fetch https://bad.example.com: timeout' in result


def test_fetch_url_is_exposed_in_tool_spec(tools):
    specs = tools.tools_spec()
    by_name = {spec['function']['name']: spec for spec in specs}

    assert 'fetch_url' in by_name
    props = by_name['fetch_url']['function']['parameters']['properties']
    assert props['url'] == {'type': 'string', 'description': 'URL to fetch.'}
    assert props['timeout']['type'] == 'number'
    assert 'timeout_sec' not in props
    assert by_name['fetch_url']['function']['parameters']['required'] == ['url']


def test_tavily_tool_aliases_are_gone(tools):
    """The historical search_tavily / fetch_tavily aliases were removed
    once the pluggable backend landed."""
    names = {spec['function']['name'] for spec in tools.tools_spec()}
    assert 'search_tavily' not in names
    assert 'fetch_tavily' not in names


def test_tavily_backend_search_parses_results(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        assert url == 'https://api.tavily.com/search'
        return _MockResponse(200, {
            'results': [
                {'title': 'A', 'url': 'https://a.example', 'content': 'x'},
                {'title': 'B', 'url': 'https://b.example', 'content': 'y'},
            ]
        })

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    results = TavilyBackend().search('hello', max_results=2)
    assert [r.url for r in results] == ['https://a.example', 'https://b.example']


@pytest.mark.parametrize(
    "status_code, body, expected_detail",
    [
        (432, "Plan usage limit exceeded", "Plan usage limit exceeded"),
        (
            403,
            '{"detail": "Your plan usage limit has been exceeded"}',
            "Your plan usage limit has been exceeded",
        ),
    ],
)
def test_tavily_search_quota_failure_advances_chain_but_is_not_retryable(
    monkeypatch, status_code, body, expected_detail
):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(status_code, text=body)

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        TavilyBackend().search('x')

    error = exc_info.value
    assert error.status_code == status_code
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics['failure_kind'] == 'quota_exhausted'
    assert error.diagnostics['response_detail'] == expected_detail


def test_tavily_search_reads_quota_detail_from_json_error(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(
            403,
            text='{"detail": {"message": "Plan usage limit exceeded for your account"}}',
        )

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        TavilyBackend().search('x')

    error = exc_info.value
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics['response_detail'] == 'Plan usage limit exceeded for your account'


def test_tavily_extract_quota_failure_advances_only_configured_fetch_chain(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        assert url == 'https://api.tavily.com/extract'
        return _MockResponse(432, text='Plan usage limit exceeded')

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        TavilyBackend().fetch_response('https://example.com')

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics['failure_kind'] == 'quota_exhausted'


class _ExplodingBodyResponse:
    status_code = 432

    @property
    def text(self):
        raise RecursionError("deep body")

    def json(self):
        raise AssertionError("HTTP error diagnostics must not decode JSON")


@pytest.mark.parametrize("operation", ["search", "extract"])
@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: _MockResponse(432, text='{malformed'),
        lambda: _MockResponse(432, text='[[[[[[' + ('x' * 5000)),
        _ExplodingBodyResponse,
    ],
    ids=["malformed", "deep-oversized", "throwing-accessor"],
)
def test_tavily_432_body_failures_are_bounded_and_cannot_suppress_quota(
    monkeypatch, operation, response_factory
):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    response = response_factory()

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return response

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        if operation == "search":
            TavilyBackend().search_response('x')
        else:
            TavilyBackend().fetch_response('https://example.com')

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics['failure_kind'] == 'quota_exhausted'
    assert len(error.diagnostics['response_detail']) <= 401
    assert len(str(error)) < 450


def test_tavily_backend_search_missing_key_raises():
    import os
    os.environ.pop('TAVILY_API_KEY', None)
    with pytest.raises(WebBackendError):
        TavilyBackend(api_key='').search('x')
