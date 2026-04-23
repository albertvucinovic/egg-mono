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
    # selected WebBackend; pin it to Tavily for this file.
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
    assert props == {
        'url': {'type': 'string', 'description': 'URL to fetch.'},
    }
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


def test_tavily_backend_search_missing_key_raises():
    import os
    os.environ.pop('TAVILY_API_KEY', None)
    with pytest.raises(WebBackendError):
        TavilyBackend(api_key='').search('x')
