from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools
from eggthreads.web import DirectHttpFetchProvider, WebBackendError, get_fetch_orchestrator


class _MockResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload=None,
        text: str = "",
        url: str | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


@pytest.fixture
def tools():
    return create_default_tools()


def test_auto_fetch_uses_tavily_first_when_key_exists(monkeypatch, tools):
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url, json, timeout))
        return _MockResponse(200, {
            "results": [
                {"url": "https://example.com", "raw_content": "# Title\n\nBody"},
            ],
            "failed_results": [],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("Direct HTTP should not be called when Tavily succeeds")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://example.com"})

    assert calls == [("post", "https://api.tavily.com/extract", {"urls": ["https://example.com"], "format": "markdown"}, 30)]
    assert out == "URL: https://example.com\n\n# Title\n\nBody"


def test_auto_fetch_falls_back_to_direct_http_after_tavily_failed_result(monkeypatch):
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url))
        return _MockResponse(200, {
            "results": [],
            "failed_results": [{"url": "https://example.com", "error": "timeout"}],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(
            200,
            text="fallback body",
            url="https://example.com",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    response = get_fetch_orchestrator().fetch_response("https://example.com")

    assert calls == [("post", "https://api.tavily.com/extract"), ("get", "https://example.com")]
    assert [attempt.provider for attempt in response.attempts] == ["tavily", "direct_http"]
    assert response.content == "fallback body"
    assert response.to_tool_output() == "URL: https://example.com\n\nfallback body"


def test_auto_fetch_reports_concise_diagnostics_when_all_providers_fail(monkeypatch, tools):
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResponse(200, {
            "results": [],
            "failed_results": [{"url": "https://example.com", "error": "timeout"}],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(503, text="service unavailable", url=url)

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://example.com"})

    assert out.startswith("Error: fetch failed after provider fallback:")
    assert "failed to fetch https://example.com: timeout" in out
    assert "fetch status 503 for https://example.com" in out


def test_auto_fetch_reports_placeholder_when_all_providers_degraded(monkeypatch, tools):
    monkeypatch.setenv("EGG_WEB_BACKEND", "auto")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResponse(200, {
            "results": [],
            "failed_results": [{"url": "https://example.com", "error": "timeout"}],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="""
            <html><head><title>Just a moment...</title></head>
            <body>Checking your browser before accessing example.com.</body></html>
            """,
            url="https://example.com/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page",
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://example.com"})

    assert out.startswith("Error: fetch failed after provider fallback:")
    assert "failed to fetch https://example.com: timeout" in out
    assert "Direct HTTP content degraded" in out
    assert "cloudflare_challenge" in out
    assert "javascript_required_placeholder" in out
    assert "Checking your browser before accessing example.com" not in out


def test_auto_fetch_without_tavily_key_uses_direct_http_only(monkeypatch, tools):
    monkeypatch.delenv("EGG_WEB_BACKEND", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        raise AssertionError("Tavily should not be called without a key in auto mode")

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append((url, headers["Accept-Language"]))
        return _MockResponse(
            200,
            text="# Local\n\nNo key fetch",
            url=url,
            headers={"Content-Type": "text/markdown"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://example.com/local"})

    assert calls == [("https://example.com/local", "en-US,en;q=0.9")]
    assert out == "URL: https://example.com/local\n\n# Local\n\nNo key fetch"


def test_explicit_searxng_fetch_uses_direct_http_compatibility(monkeypatch, tools):
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_post(url, json=None, headers=None, timeout=None):
        raise AssertionError("Tavily should not be called when SearXNG is pinned")

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(url)
        return _MockResponse(
            200,
            text="direct compatibility",
            url=url,
            headers={"Content-Type": "text/plain"},
        )

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://example.com/direct"})

    assert calls == ["https://example.com/direct"]
    assert out == "URL: https://example.com/direct\n\ndirect compatibility"


def test_direct_http_empty_html_extraction_is_degraded(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><head><title></title></head><body><script>app()</script></body></html>",
            url=url,
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/empty")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.degraded is True
    assert exc.retriable is True
    assert "empty_html_extraction" in str(exc)
    assert exc.diagnostics["fetch_quality"]["signals"] == ["empty_html_extraction"]


def test_direct_http_near_empty_html_extraction_is_degraded(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><body>OK</body></html>",
            url=url,
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/empty-ish")

    assert "near_empty_html_extraction" in str(exc_info.value)


@pytest.mark.parametrize(
    ("html", "expected_signal"),
    [
        (
            "<html><title>Just a moment...</title><body>Just a moment while we verify your browser /cdn-cgi/trace</body></html>",
            "cloudflare_challenge",
        ),
        (
            "<html><title>Attention Required!</title><body>Cloudflare Ray ID security check</body></html>",
            "cloudflare_challenge",
        ),
        (
            "<html><body><div class='g-recaptcha'></div>Complete this CAPTCHA to continue</body></html>",
            "captcha_challenge",
        ),
        (
            "<html><body><div class='h-captcha'></div>Security check</body></html>",
            "captcha_challenge",
        ),
        (
            "<html><body><div class='cf-turnstile'></div>Security check</body></html>",
            "captcha_challenge",
        ),
        (
            "<html><body>Please enable JavaScript and cookies to continue. Checking your browser.</body></html>",
            "javascript_required_placeholder",
        ),
        (
            "<html><body>Access Denied. We detected unusual traffic from your computer network.</body></html>",
            "bot_block_placeholder",
        ),
    ],
)
def test_direct_http_blocked_placeholder_pages_are_degraded(monkeypatch, html, expected_signal):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(200, text=html, url=url, headers={"Content-Type": "text/html"})

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.degraded is True
    assert exc.retriable is True
    assert expected_signal in exc.diagnostics["fetch_quality"]["signals"]


def test_direct_http_suspicious_final_path_with_generic_content_is_degraded(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><body>Please verify</body></html>",
            url="https://example.com/challenge/verify",
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    assert "suspicious_final_url_path" in exc_info.value.diagnostics["fetch_quality"]["signals"]


def test_direct_http_extracts_html(monkeypatch):
    html = "<html><body><h1>Title</h1><p>Paragraph body.</p></body></html>"

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text=html,
            url="https://example.com/final",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    response = DirectHttpFetchProvider().fetch_response("https://example.com/page")

    assert response.final_url == "https://example.com/final"
    assert response.content_type == "text/html"
    assert "Title" in response.content
    assert "Paragraph body" in response.content


def test_direct_http_returns_text_markdown_directly(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="# Heading\n\n* item",
            url=url,
            headers={"Content-Type": "text/markdown"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    response = DirectHttpFetchProvider().fetch_response("https://example.com/readme.md")

    assert response.content_type == "text/markdown"
    assert response.content == "# Heading\n\n* item"


def test_direct_http_pretty_prints_and_bounds_json(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text='{"b":2,"a":{"nested":[1,2,3]}}',
            url=url,
            headers={"Content-Type": "application/json"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    response = DirectHttpFetchProvider(max_chars=35).fetch_response("https://example.com/data.json")

    assert response.content_type == "application/json"
    assert response.content.startswith('''{
  "b": 2,''')
    assert response.content.endswith("… (truncated)")


def test_explicit_tavily_fetch_does_not_fallback_to_direct_http(monkeypatch, tools):
    monkeypatch.setenv("EGG_WEB_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResponse(200, {
            "results": [],
            "failed_results": [{"url": "https://bad.example.com", "error": "timeout"}],
        })

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        raise AssertionError("Direct HTTP should not be called when Tavily is pinned")

    import requests
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setattr(requests, "get", mock_get)

    out = tools.execute("fetch_url", {"url": "https://bad.example.com"})

    assert "Error:" in out
    assert "failed to fetch https://bad.example.com: timeout" in out


def test_direct_http_rejects_non_http_urls():
    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("file:///etc/passwd")

    assert "Unsupported URL scheme" in str(exc_info.value)


def test_direct_http_marks_503_retriable(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(503, text="service unavailable", url=url)

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/down")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.status_code == 503
    assert exc.retriable is True


def test_direct_http_marks_404_terminal(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(404, text="not found", url=url)

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/missing")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.status_code == 404
    assert exc.retriable is False
