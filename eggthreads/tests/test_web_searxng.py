from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools
from eggthreads.web import WebBackendError
from eggthreads.web.searxng import SearxngBackend


class _MockResponse:
    def __init__(self, status_code=200, payload=None, text='', url=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError('no json payload')
        return self._payload


@pytest.fixture
def tools():
    return create_default_tools()


@pytest.fixture(autouse=True)
def _force_searxng(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'searxng')
    monkeypatch.setenv('SEARXNG_URL', 'http://localhost:8888')


def test_web_tools_exposed(tools):
    names = {s['function']['name'] for s in tools.tools_spec()}
    assert {'web_search', 'fetch_url'} <= names
    # Tavily-named aliases were removed when the pluggable backend landed.
    assert 'search_tavily' not in names
    assert 'fetch_tavily' not in names


def test_searxng_search_parses_json(monkeypatch):
    calls = []

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append({'url': url, 'params': params, 'headers': headers})
        return _MockResponse(200, {
            'results': [
                {'title': 'Example', 'url': 'https://example.com', 'content': 'hello world'},
                {'title': 'Other', 'url': 'https://other.example', 'content': ''},
            ]
        })

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    results = backend.search('test query', max_results=5)

    assert calls[0]['url'] == 'http://localhost:8888/search'
    assert calls[0]['params'] == {'q': 'test query', 'format': 'json'}
    assert calls[0]['headers']['Accept'] == 'application/json'
    assert [r.url for r in results] == ['https://example.com', 'https://other.example']
    assert results[0].title == 'Example'


def test_searxng_search_non_json_raises(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, payload=None, text='<html>not json</html>')

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError):
        backend.search('x')


def test_searxng_fetch_runs_through_trafilatura(monkeypatch):
    html = "<html><body><h1>Title</h1><p>Paragraph body.</p></body></html>"

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(200, text=html, url=url)

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    out = backend.fetch('https://example.com/page')

    assert out.startswith('URL: https://example.com/page')
    # Either trafilatura-extracted markdown or the stripped-tags fallback
    # should include the headline / body.
    assert 'Title' in out
    assert 'Paragraph body' in out


def test_searxng_fetch_http_error_raises(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(503, text='service unavailable', url=url)

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError):
        backend.fetch('https://example.com/page')


def test_web_search_tool_goes_through_searxng(monkeypatch, tools):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {
            'results': [{'title': 'T', 'url': 'https://a.example', 'content': 'x'}]
        })

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    out = tools.execute('web_search', {'query': 'hello'})
    assert 'https://a.example' in out
    assert 'T' in out


def test_searxng_connection_refused_hints_at_startSearxng(monkeypatch):
    """When SearXNG isn't running, the error message should tell the
    user to run /startSearxng."""
    import requests

    def mock_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("Max retries exceeded")

    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError) as exc_info:
        backend.search('anything')
    msg = str(exc_info.value)
    assert '/startSearxng' in msg
    assert 'http://localhost:8888' in msg


def test_web_search_error_reaches_tool_layer(monkeypatch, tools):
    """The startSearxng hint bubbles up through the tool dispatcher."""
    import requests

    def mock_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, 'get', mock_get)

    out = tools.execute('web_search', {'query': 'x'})
    assert 'Error:' in out
    assert '/startSearxng' in out
