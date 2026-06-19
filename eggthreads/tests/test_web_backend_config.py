from __future__ import annotations

import pytest

from eggthreads.web import WebBackendError, get_fetch_orchestrator, get_search_orchestrator


class _MockResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "", url: str | None = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def _clear_web_env(monkeypatch):
    monkeypatch.delenv("EGG_WEB_BACKEND", raising=False)
    monkeypatch.delenv("EGG_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("EGG_WEB_FETCH_BACKEND", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def test_search_split_backend_overrides_global_backend(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    monkeypatch.setenv("EGG_WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url))
        return _MockResponse(200, {
            "results": [{"title": "T", "url": "https://tavily.example", "content": "ok"}],
        })

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("SearXNG should not be called when EGG_WEB_SEARCH_BACKEND=tavily")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x")

    assert calls == [("post", "https://api.tavily.com/search")]
    assert [attempt.provider for attempt in response.attempts] == ["tavily"]
    assert [result.url for result in response.results] == ["https://tavily.example"]


def test_fetch_split_backend_overrides_global_backend(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "tavily")
    monkeypatch.setenv("EGG_WEB_FETCH_BACKEND", "searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        raise AssertionError("Tavily should not be called when EGG_WEB_FETCH_BACKEND=searxng")

    def mock_get(url, headers=None, timeout=None, allow_redirects=None, params=None):
        calls.append(("get", url))
        return _MockResponse(
            200,
            text="direct fetch",
            url=url,
            headers={"Content-Type": "text/plain"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_fetch_orchestrator().fetch_response("https://example.com/page")

    assert calls == [("get", "https://example.com/page")]
    assert [attempt.provider for attempt in response.attempts] == ["direct_http"]
    assert response.content == "direct fetch"


def test_default_auto_backend_resolution_without_credentials(monkeypatch):
    _clear_web_env(monkeypatch)

    search = get_search_orchestrator()
    fetch = get_fetch_orchestrator()

    assert [provider.name for provider in search.providers] == ["searxng"]
    assert [provider.name for provider in fetch.providers] == ["direct_http"]


def test_global_backend_compatibility_when_split_vars_absent(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    search = get_search_orchestrator()
    fetch = get_fetch_orchestrator()

    assert [provider.name for provider in search.providers] == ["tavily"]
    assert [provider.name for provider in fetch.providers] == ["tavily"]


def test_global_searxng_fetch_compatibility_maps_to_direct_http(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")

    search = get_search_orchestrator()
    fetch = get_fetch_orchestrator()

    assert [provider.name for provider in search.providers] == ["searxng"]
    assert [provider.name for provider in fetch.providers] == ["direct_http"]


def test_unknown_search_split_backend_names_correct_env_var(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("EGG_WEB_SEARCH_BACKEND", "bogus")

    with pytest.raises(WebBackendError) as exc_info:
        get_search_orchestrator()

    msg = str(exc_info.value)
    assert "Unknown EGG_WEB_SEARCH_BACKEND='bogus'" in msg
    assert "auto, searxng, tavily" in msg


def test_unknown_fetch_split_backend_names_correct_env_var(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("EGG_WEB_FETCH_BACKEND", "bogus")

    with pytest.raises(WebBackendError) as exc_info:
        get_fetch_orchestrator()

    msg = str(exc_info.value)
    assert "Unknown EGG_WEB_FETCH_BACKEND='bogus'" in msg
    assert "auto, searxng, tavily" in msg
