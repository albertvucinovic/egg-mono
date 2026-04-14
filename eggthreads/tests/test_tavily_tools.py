from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools


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


def test_fetch_tavily_requires_api_key(monkeypatch, tools):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)

    result = tools.execute('fetch_tavily', {'url': 'https://example.com'})

    assert 'TAVILY_API_KEY' in result


def test_fetch_tavily_requires_url(monkeypatch, tools):
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-test')

    result = tools.execute('fetch_tavily', {})

    assert '"url" is required' in result


def test_fetch_tavily_uses_simple_markdown_request(monkeypatch, tools):
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

    result = tools.execute('fetch_tavily', {'url': 'https://example.com'})

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


def test_fetch_tavily_formats_failed_result(monkeypatch, tools):
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

    result = tools.execute('fetch_tavily', {'url': 'https://bad.example.com'})

    assert 'failed to fetch https://bad.example.com: timeout' in result


def test_fetch_tavily_is_exposed_in_tool_spec(tools):
    specs = tools.tools_spec()
    by_name = {spec['function']['name']: spec for spec in specs}

    assert 'fetch_tavily' in by_name
    props = by_name['fetch_tavily']['function']['parameters']['properties']
    assert props == {
        'url': {'type': 'string', 'description': 'URL to fetch.'},
    }
    assert by_name['fetch_tavily']['function']['parameters']['required'] == ['url']
