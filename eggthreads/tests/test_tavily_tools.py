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

    def mock_post(url, json=None, headers=None, timeout=None):
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


def test_tavily_backend_formats_failed_result(monkeypatch, tools):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None):
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

    def mock_post(url, json=None, headers=None, timeout=None):
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
    "status_code, detail",
    [
        (432, "Plan usage limit exceeded"),
        (403, "Your plan usage limit has been exceeded"),
    ],
)
def test_tavily_search_quota_failure_advances_chain_but_is_not_retryable(
    monkeypatch, status_code, detail
):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResponse(status_code, text=detail)

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        TavilyBackend().search('x')

    error = exc_info.value
    assert error.status_code == status_code
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics['failure_kind'] == 'quota_exhausted'
    assert error.diagnostics['response_detail'] == detail


def test_tavily_search_reads_quota_detail_from_json_error(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResponse(403, payload={
            'detail': {'message': 'Plan usage limit exceeded for your account'},
        }, text='Forbidden')

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

    def mock_post(url, json=None, headers=None, timeout=None):
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


def test_tavily_backend_search_missing_key_raises():
    import os
    os.environ.pop('TAVILY_API_KEY', None)
    with pytest.raises(WebBackendError):
        TavilyBackend(api_key='').search('x')
