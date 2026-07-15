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

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
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

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
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

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
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


def test_search_chain_falls_back_on_tavily_quota_without_marking_retryable(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []
    quota_detail = "Plan usage limit exceeded for this billing cycle " + ("x" * 1000)

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _MockResponse(432, text=quota_detail)

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

    response = get_search_orchestrator().search_response("quota search", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search", "quota search"),
    ]
    assert [result.url for result in response.results] == ["https://searxng.example"]
    assert response.degraded is True
    assert "Tavily API status 432" in response.diagnostic_messages()[0]
    tavily_attempt = response.attempts[0]
    assert tavily_attempt.provider == "tavily"
    assert tavily_attempt.success is False
    assert tavily_attempt.retriable is False
    assert tavily_attempt.fallback_eligible is True
    assert "Tavily API status 432" in tavily_attempt.message
    assert "Plan usage limit exceeded" in tavily_attempt.message
    assert len(tavily_attempt.message) < 500
    assert tavily_attempt.diagnostics == {
        "status_code": 432,
        "response_detail": quota_detail[:399] + "…",
        "failure_kind": "quota_exhausted",
    }


@pytest.mark.parametrize(
    "quota_message",
    [
        "This request exceeds your plan's set usage limit. Please upgrade your plan.",
        "This request exceeds your plans usage limit",
    ],
)
def test_search_chain_falls_back_on_reported_tavily_plan_limit_phrases(
    monkeypatch, quota_message
):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _MockResponse(403, text=f'{{"detail": "{quota_message}"}}')

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(200, {
            "results": [
                {"title": "S", "url": "https://searxng.example", "content": "fallback"},
            ],
        })

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search"),
    ]
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is True
    assert response.attempts[0].diagnostics["response_detail"] == quota_message
    assert [result.url for result in response.results] == ["https://searxng.example"]


@pytest.mark.parametrize(
    "status_code, body",
    [
        (400, 'Invalid query echoed user text "quota exceeded"'),
        (400, '{"detail": "quota exceeded"}'),
        (401, '{"detail": "plan usage limit exceeded"}'),
        (403, '{"detail": "invalid API key"}'),
        (403, '{"detail": "permission denied"}'),
        (403, '{"detail": "forbidden"}'),
        (403, 'Invalid query echoed user text "quota exceeded"'),
        (403, '{"detail": "request mentions plan usage but no limit was exceeded"}'),
        (403, '{"detail": "This request nearly exceeds your plan usage limit"}'),
        (403, '{"detail": "This request no longer exceeds your plan usage limit"}'),
        (403, '{"detail": "This request does not exceed your plan usage limit"}'),
        (403, '{"query": "This request exceeds your plan usage limit"}'),
        (403, '<html>Example: This request exceeds your plan usage limit</html>'),
        (403, '{"detail": "quota exceeded"}'),
        (404, '{"detail": "plan usage limit exceeded"}'),
    ],
)
def test_search_chain_does_not_fallback_on_nonquota_tavily_failures(
    monkeypatch, status_code, body
):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(status_code, text=body)

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("nonquota Tavily failures must not advance the chain")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("quota exceeded", max_results=1)

    assert response.results == []
    assert len(response.attempts) == 1
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is False
    assert response.attempts[0].diagnostics["status_code"] == status_code


@pytest.mark.parametrize(
    "body",
    [
        '{"message":"Forbidden","detail":{"error":"Your plan usage limit has been exceeded"}}',
        '{"detail":{"error":"Your plan usage limit has been exceeded"},"message":"Forbidden"}',
        '{"error":{"detail":"Your plan usage limit has been exceeded"},"message":"Forbidden"}',
    ],
)
def test_search_chain_checks_all_recognized_error_fields_regardless_of_order(
    monkeypatch, body
):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _MockResponse(403, text=body)

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(200, {
            "results": [
                {"title": "S", "url": "https://searxng.example", "content": "fallback"},
            ],
        })

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search"),
    ]
    assert response.attempts[0].fallback_eligible is True
    assert response.attempts[0].retriable is False


def test_search_chain_does_not_classify_truncated_json_prefix(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    body = (
        '{"detail":"This request exceeds your plan usage limit",'
        '"padding":"' + ("x" * 5000) + '"}'
    )

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(403, text=body)

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("incomplete JSON must not become provider error authority")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert response.results == []
    assert response.attempts[0].fallback_eligible is False
    assert len(response.attempts[0].diagnostics["response_detail"]) == 400


def test_search_chain_uses_only_bounded_text_prefix_for_semantic_quota(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(403, text=("x" * 5000) + " plan usage limit exceeded")

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("text beyond the bounded prefix must not drive classification")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert response.results == []
    assert response.attempts[0].fallback_eligible is False
    detail = response.attempts[0].diagnostics["response_detail"]
    assert len(detail) == 400
    assert detail.endswith("…")
    assert "plan usage limit" not in detail


def test_auto_search_falls_back_on_tavily_quota(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _MockResponse(432, text="Plan usage limit exceeded")

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(200, {
            "results": [
                {"title": "S", "url": "https://searxng.example", "content": "fallback"},
            ],
        })

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("quota search", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search"),
    ]
    assert [attempt.provider for attempt in response.attempts] == ["tavily", "searxng"]
    assert [result.url for result in response.results] == ["https://searxng.example"]


def test_pinned_tavily_quota_failure_is_terminal(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(432, text="Plan usage limit exceeded")

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("SearXNG must not run for a pinned Tavily provider")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        get_search_orchestrator().search_response("quota search", max_results=1)

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert "Plan usage limit exceeded" in str(error)


def test_fetch_chain_falls_back_from_tavily_to_direct_http(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "tavily,direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
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


def test_search_chain_stops_on_non_fallback_tavily_client_error(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return _MockResponse(401, text="invalid API key")

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("permanent Tavily client errors must not advance the chain")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert response.results == []
    assert len(response.attempts) == 1
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is False
    assert response.attempts[0].diagnostics["status_code"] == 401


class _Throwing432Response:
    status_code = 432

    @property
    def text(self):
        raise RecursionError("deep response body")

    def json(self):
        raise RecursionError("deep JSON")


def test_search_chain_falls_back_on_throwing_432_body(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_CHAIN", "tavily,searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _Throwing432Response()

    def mock_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(200, {
            "results": [
                {"title": "S", "url": "https://searxng.example", "content": "fallback"},
            ],
        })

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_search_orchestrator().search_response("x", max_results=1)

    assert calls == [
        ("post", "https://api.tavily.com/search"),
        ("get", "http://localhost:8888/search"),
    ]
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is True
    assert response.attempts[0].diagnostics == {
        "status_code": 432,
        "response_detail": "",
        "failure_kind": "quota_exhausted",
    }


def test_pinned_tavily_search_throwing_432_body_is_terminal(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    import requests
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: _Throwing432Response(),
    )

    with pytest.raises(WebBackendError) as exc_info:
        get_search_orchestrator().search_response("x", max_results=1)

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert error.diagnostics["response_detail"] == ""


def test_fetch_chain_falls_back_on_tavily_quota_to_direct_http_not_searxng(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "tavily,direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _MockResponse(432, text="Plan usage limit exceeded")

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
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is True
    assert response.attempts[0].diagnostics["failure_kind"] == "quota_exhausted"
    assert response.content == "direct fallback"


def test_fetch_chain_falls_back_on_throwing_432_body(monkeypatch):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "tavily,direct_http")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append(("post", url))
        return _Throwing432Response()

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
    assert response.attempts[0].retriable is False
    assert response.attempts[0].fallback_eligible is True
    assert response.attempts[0].diagnostics["response_detail"] == ""
    assert [attempt.provider for attempt in response.attempts] == ["tavily", "direct_http"]


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: _MockResponse(432, text="Plan usage limit exceeded"),
        _Throwing432Response,
    ],
    ids=["normal", "throwing-body"],
)
def test_pinned_tavily_extract_quota_failure_is_terminal(monkeypatch, response_factory):
    _clear_web_env(monkeypatch)
    monkeypatch.setenv("EGG_WEB_FETCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    response = response_factory()

    def mock_post(url, json=None, headers=None, timeout=None, stream=None):
        return response

    def mock_get(url, headers=None, timeout=None, allow_redirects=None, params=None):
        raise AssertionError("Direct HTTP must not run for pinned Tavily extract")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        get_fetch_orchestrator().fetch_response("https://example.com/page")

    error = exc_info.value
    assert error.status_code == 432
    assert error.retriable is False
    assert error.fallback_eligible is True
    assert len(error.diagnostics["response_detail"]) <= 401


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
