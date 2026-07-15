from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools
from eggthreads.web import WebBackendError, get_search_orchestrator
from eggthreads.web.search import SearchOrchestrator
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
    assert not any('browser' in name.lower() or 'playwright' in name.lower() for name in names)


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


def test_searxng_search_empty_without_unresponsive_is_true_empty(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {'results': [], 'unresponsive_engines': []})

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    response = backend.search_response('no hits')

    assert response.results == []
    assert response.true_empty
    assert not response.degraded_empty
    assert response.attempts[0].success is True
    assert response.attempts[0].degraded is False


def test_searxng_search_empty_with_unresponsive_is_degraded_retriable(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {
            'results': [],
            'unresponsive_engines': [
                ['duckduckgo', 'general', 'CAPTCHA'],
                {'engine': 'brave', 'error': 'too many requests'},
            ],
        })

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    response = backend.search_response('degraded')

    assert response.results == []
    assert not response.true_empty
    assert response.degraded_empty
    attempt = response.attempts[0]
    assert attempt.success is True
    assert attempt.degraded is True
    assert attempt.retriable is True
    assert 'SearXNG degraded' in attempt.message
    assert 'duckduckgo CAPTCHA' in attempt.message
    assert attempt.diagnostics['unresponsive_engines'][0] == {
        'name': 'duckduckgo',
        'reason': 'CAPTCHA',
    }


def test_searxng_search_partial_results_with_unresponsive_is_degraded_not_retriable(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {
            'results': [
                {'title': 'Example', 'url': 'https://example.com', 'content': 'ok'},
            ],
            'unresponsive_engines': [['brave', 'timeout']],
        })

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    response = backend.search_response('partial')

    assert [r.url for r in response.results] == ['https://example.com']
    attempt = response.attempts[0]
    assert attempt.degraded is True
    assert attempt.retriable is False
    assert 'brave timeout' in attempt.message


def test_searxng_search_non_json_raises(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, payload=None, text='<html>not json</html>')

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError):
        backend.search('x')


def test_searxng_search_http_error_has_structured_diagnostics(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(503, {'error': 'down'}, text='service unavailable')

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError) as exc_info:
        backend.search('x')

    exc = exc_info.value
    assert exc.provider == 'searxng'
    assert exc.status_code == 503
    assert exc.retriable is True
    assert 'SearXNG status 503' in str(exc)


def test_searxng_search_http_404_is_not_retriable(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(404, {'error': 'not found'}, text='not found')

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    backend = SearxngBackend(base_url='http://localhost:8888')
    with pytest.raises(WebBackendError) as exc_info:
        backend.search('x')

    assert exc_info.value.status_code == 404
    assert exc_info.value.retriable is False


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
    """The startSearxng hint bubbles up through the degraded search output."""
    import requests

    def mock_get(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, 'get', mock_get)

    out = tools.execute('web_search', {'query': 'x'})
    assert 'Search backend degraded; no reliable results returned.' in out
    assert '/startSearxng' in out


def test_web_search_true_empty_message(monkeypatch, tools):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {'results': []})

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    out = tools.execute('web_search', {'query': 'x'})
    assert out == 'No matching results found.'


def test_web_search_degraded_empty_message(monkeypatch, tools):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {
            'results': [],
            'unresponsive_engines': [['duckduckgo', 'CAPTCHA']],
        })

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    out = tools.execute('web_search', {'query': 'x'})
    assert out.startswith('Search backend degraded; no reliable results returned.')
    assert 'SearXNG degraded: duckduckgo CAPTCHA' in out


def test_search_orchestrator_falls_back_after_degraded_empty():
    class DegradedProvider:
        name = 'degraded'

        def search_response(self, query, max_results=5):
            from eggthreads.web import SearchAttempt, SearchResponse
            return SearchResponse(
                results=[],
                attempts=[SearchAttempt(
                    provider=self.name,
                    success=True,
                    degraded=True,
                    retriable=True,
                    message='degraded provider unavailable',
                )],
            )

    class SuccessProvider:
        name = 'success'

        def search_response(self, query, max_results=5):
            from eggthreads.web import SearchAttempt, SearchResponse, SearchResult
            return SearchResponse(
                results=[SearchResult('T', 'https://ok.example', 'snippet')],
                attempts=[SearchAttempt(provider=self.name, success=True)],
            )

    response = SearchOrchestrator([DegradedProvider(), SuccessProvider()]).search_response('x')

    assert [attempt.provider for attempt in response.attempts] == ['degraded', 'success']
    assert [result.url for result in response.results] == ['https://ok.example']


def test_search_orchestrator_deduplicates_urls():
    class Provider:
        name = 'p'

        def __init__(self, suffix):
            self.suffix = suffix

        def search_response(self, query, max_results=5):
            from eggthreads.web import SearchAttempt, SearchResponse, SearchResult
            return SearchResponse(
                results=[
                    SearchResult(f'A{self.suffix}', 'https://same.example', ''),
                    SearchResult(f'B{self.suffix}', f'https://{self.suffix}.example', ''),
                ],
                attempts=[SearchAttempt(provider=f'p{self.suffix}', success=True)],
            )

    response = SearchOrchestrator([Provider('one'), Provider('two')]).search_response('x', 3)

    assert [result.url for result in response.results] == [
        'https://same.example',
        'https://one.example',
        'https://two.example',
    ]


def test_auto_search_uses_tavily_then_searxng_when_key_is_configured(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'auto')
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(('post', url))
        return _MockResponse(503, {'error': 'down'}, text='down')

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append(('get', url))
        return _MockResponse(200, {
            'results': [{'title': 'S', 'url': 'https://searx.example', 'content': 'ok'}]
        })

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)
    monkeypatch.setattr(requests, 'get', mock_get)

    response = get_search_orchestrator().search_response('x')

    assert calls == [
        ('post', 'https://api.tavily.com/search'),
        ('get', 'http://localhost:8888/search'),
    ]
    assert [attempt.provider for attempt in response.attempts] == ['tavily', 'searxng']
    assert [result.url for result in response.results] == ['https://searx.example']


def test_auto_search_skips_tavily_without_key(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'auto')
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(('post', url))
        raise AssertionError('Tavily should not be called without a key')

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append(('get', url))
        return _MockResponse(200, {'results': []})

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)
    monkeypatch.setattr(requests, 'get', mock_get)

    response = get_search_orchestrator().search_response('x')

    assert calls == [('get', 'http://localhost:8888/search')]
    assert [attempt.provider for attempt in response.attempts] == ['searxng']
    assert response.true_empty


def test_default_auto_search_without_key_uses_searxng(monkeypatch):
    monkeypatch.delenv('EGG_WEB_BACKEND', raising=False)
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    calls = []

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return _MockResponse(200, {'results': []})

    import requests
    monkeypatch.setattr(requests, 'get', mock_get)

    response = get_search_orchestrator().search_response('x')

    assert calls == ['http://localhost:8888/search']
    assert [attempt.provider for attempt in response.attempts] == ['searxng']


def test_explicit_searxng_search_is_pinned_without_tavily(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'searxng')
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        raise AssertionError('Tavily should not be called when SearXNG is pinned')

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append(('get', url))
        return _MockResponse(200, {'results': []})

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)
    monkeypatch.setattr(requests, 'get', mock_get)

    response = get_search_orchestrator().search_response('x')

    assert calls == [('get', 'http://localhost:8888/search')]
    assert [attempt.provider for attempt in response.attempts] == ['searxng']


def test_explicit_tavily_search_is_pinned_without_searxng_fallback(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'tavily')
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(200, {'results': []})

    def mock_get(url, params=None, headers=None, timeout=None):
        raise AssertionError('SearXNG should not be called when Tavily is pinned')

    import requests
    monkeypatch.setattr(requests, 'post', mock_post)
    monkeypatch.setattr(requests, 'get', mock_get)

    response = get_search_orchestrator().search_response('x')

    assert [attempt.provider for attempt in response.attempts] == ['tavily']
    assert response.true_empty


def test_unknown_search_backend_has_clear_valid_values(monkeypatch):
    monkeypatch.setenv('EGG_WEB_BACKEND', 'bogus')

    with pytest.raises(WebBackendError) as exc_info:
        get_search_orchestrator()

    msg = str(exc_info.value)
    assert "Unknown EGG_WEB_BACKEND='bogus'" in msg
    assert 'auto, searxng, tavily' in msg


def _mock_n_results(monkeypatch, n: int):
    def mock_get(url, params=None, headers=None, timeout=None):
        return _MockResponse(200, {
            'results': [
                {
                    'title': f'Title {i}',
                    'url': f'https://a{i}.example',
                    'content': f'Snippet {i}',
                }
                for i in range(n)
            ]
        })
    import requests
    monkeypatch.setattr(requests, 'get', mock_get)


def test_web_search_defaults_to_ten(monkeypatch, tools):
    """Default should be 10 results when neither arg nor env var is set."""
    monkeypatch.delenv('EGG_WEB_MAX_RESULTS', raising=False)
    _mock_n_results(monkeypatch, 25)
    out = tools.execute('web_search', {'query': 'x'})
    assert out.count('\n- ') + 1 == 10  # 10 result lines


def test_web_search_honours_explicit_max_results(monkeypatch, tools):
    _mock_n_results(monkeypatch, 20)
    out = tools.execute('web_search', {'query': 'x', 'max_results': 3})
    assert out.count('\n- ') + 1 == 3


def test_web_search_honours_env_default(monkeypatch, tools):
    monkeypatch.setenv('EGG_WEB_MAX_RESULTS', '7')
    _mock_n_results(monkeypatch, 20)
    out = tools.execute('web_search', {'query': 'x'})
    assert out.count('\n- ') + 1 == 7


def test_web_search_caps_absurd_values(monkeypatch, tools):
    """Values above the cap are clamped; garbage values fall back to 10."""
    _mock_n_results(monkeypatch, 100)
    out = tools.execute('web_search', {'query': 'x', 'max_results': 9999})
    # Count must be at most _WEB_RESULTS_CAP (25) but exactly 25 here
    # because the mock supplies 100 raw results.
    assert out.count('\n- ') + 1 == 25

    monkeypatch.setenv('EGG_WEB_MAX_RESULTS', 'not-a-number')
    out = tools.execute('web_search', {'query': 'x'})
    assert out.count('\n- ') + 1 == 10  # fell back to default


def test_web_search_includes_snippet(monkeypatch, tools):
    _mock_n_results(monkeypatch, 3)
    out = tools.execute('web_search', {'query': 'x', 'max_results': 3})
    # Snippet text should appear indented under each result.
    assert 'Snippet 0' in out and 'Snippet 2' in out
    assert 'https://a1.example' in out


def test_web_search_schema_advertises_max_results(tools):
    spec = next(s for s in tools.tools_spec() if s['function']['name'] == 'web_search')
    props = spec['function']['parameters']['properties']
    assert 'max_results' in props
    assert props['max_results']['type'] == 'integer'
    assert props['max_results']['minimum'] == 1
    assert props['max_results']['maximum'] >= 20
