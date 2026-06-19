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
    monkeypatch.delenv("EGG_WEB_SEARCH_CHAIN", raising=False)
    monkeypatch.delenv("EGG_WEB_FETCH_CHAIN", raising=False)
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
    valid_values = msg.split("Valid values:", 1)[1]
    assert "browser" not in valid_values.lower()
    assert "playwright" not in valid_values.lower()


def test_unknown_fetch_split_backend_names_correct_env_var(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("EGG_WEB_FETCH_BACKEND", "bogus")

    with pytest.raises(WebBackendError) as exc_info:
        get_fetch_orchestrator()

    msg = str(exc_info.value)
    assert "Unknown EGG_WEB_FETCH_BACKEND='bogus'" in msg
    assert "auto, searxng, tavily" in msg
    valid_values = msg.split("Valid values:", 1)[1]
    assert "browser" not in valid_values.lower()
    assert "playwright" not in valid_values.lower()


def test_default_provider_lists_do_not_include_browser_providers(monkeypatch):
    _clear_web_env(monkeypatch)

    search = get_search_orchestrator()
    fetch = get_fetch_orchestrator()
    provider_names = [provider.name for provider in [*search.providers, *fetch.providers]]

    assert provider_names == ["searxng", "direct_http"]
    assert not any("browser" in name.lower() or "playwright" in name.lower() for name in provider_names)


def test_search_chain_falls_back_from_tavily_to_searxng(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url))
        return _MockResponse(503, text="temporarily unavailable")

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url, params["q"]))
        return _MockResponse(200, {
            "results": [
                {"title": "S", "url": "https://searxng.example", "content": "fallback"},
            ],
        })

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("chain search", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search", "chain search"),
    ]
    assert [attempt.provider for attempt in response.attempts] == ["tavily", "searxng"]
    assert [result.url for result in response.results] == ["https://searxng.example"]


def test_fetch_chain_falls_back_from_tavily_to_direct_http(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "tavily,direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url))
        return _MockResponse(200, {
            "results": [],
            "failed_results": [{"url": "https://example.com/page", "error": "timeout"}],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None, params=None):
        calls.append(("get", url))
        return _MockResponse(
            200,
            text="direct fallback",
            url=url,
            headers={"Content-Type": "text/plain"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_fetch_orchestrator().fetch_response("https://example.com/page")

    assert calls == [
        ("post", "https://api.tavily.com/extract"),
        ("get", "https://example.com/page"),
    ]
    assert [attempt.provider for attempt in response.attempts] == ["tavily", "direct_http"]
    assert response.content == "direct fallback"


def test_chain_env_overrides_split_and_global_backend(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_BACKEND", "tavily")
    monkeypatch.setenv("EGG_WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("EGG_WEB_FETCH_BACKEND", "tavily")
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "searxng")
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    search = get_search_orchestrator()
    fetch = get_fetch_orchestrator()

    assert [provider.name for provider in search.providers] == ["searxng"]
    assert [provider.name for provider in fetch.providers] == ["direct_http"]


def test_explicit_name_arg_overrides_chain_env(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "searxng")
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    search = get_search_orchestrator("tavily")
    fetch = get_fetch_orchestrator("tavily")

    assert [provider.name for provider in search.providers] == ["tavily"]
    assert [provider.name for provider in fetch.providers] == ["tavily"]


def test_fetch_chain_searxng_maps_to_direct_http(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "searxng")

    fetch = get_fetch_orchestrator()

    assert [provider.name for provider in fetch.providers] == ["direct_http"]


def test_unknown_chain_values_name_correct_env_and_valid_values(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,playwright")

    with pytest.raises(WebBackendError) as search_exc_info:
        get_search_orchestrator()

    search_msg = str(search_exc_info.value)
    assert "Unknown EGG_WEB_SEARCH_CHAIN provider 'playwright'" in search_msg
    assert "searxng, searx, tavily" in search_msg
    assert "playwright" not in search_msg.split("Valid values:", 1)[1]
    assert "browser" not in search_msg.split("Valid values:", 1)[1].lower()

    monkeypatch.delenv("EGG_WEB_SEARCH_CHAIN", raising=False)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "tavily,browser")

    with pytest.raises(WebBackendError) as fetch_exc_info:
        get_fetch_orchestrator()

    fetch_msg = str(fetch_exc_info.value)
    assert "Unknown EGG_WEB_FETCH_CHAIN provider 'browser'" in fetch_msg
    assert "searxng, searx, tavily, direct_http" in fetch_msg
    assert "playwright" not in fetch_msg.split("Valid values:", 1)[1]
    assert "browser" not in fetch_msg.split("Valid values:", 1)[1].lower()
