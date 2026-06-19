from __future__ import annotations

import pytest

from eggthreads.tools import create_default_tools
from eggthreads.web import (
    DirectHttpFetchProvider,
    FetchAttempt,
    FetchOrchestrator,
    FetchResponse,
    WebBackendError,
    get_fetch_orchestrator,
)


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


@pytest.mark.parametrize(
    ("headers", "expected_hint"),
    [
        ({"cf-mitigated": "challenge"}, "cloudflare_challenge_header"),
        ({"Set-Cookie": "ak_bmsc=abc; Path=/; HttpOnly"}, "akamai_challenge_header"),
        ({"x-px-captcha": "1"}, "perimeterx_challenge_header"),
        ({"Set-Cookie": "datadome=abc; Path=/; Secure"}, "datadome_challenge_header"),
        ({"x-iinfo": "1-123-0", "Set-Cookie": "incap_ses_123=abc"}, "incapsula_challenge_header"),
        ({"Server": "ddos-guard"}, "ddos_guard_challenge_header"),
    ],
)
def test_direct_http_provider_challenge_headers_are_degraded(monkeypatch, headers, expected_hint):
    challenge_headers = headers

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><body><h1>Security check</h1></body></html>",
            url=url,
            headers={"Content-Type": "text/html", **challenge_headers},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    quality = exc_info.value.diagnostics["fetch_quality"]
    assert "provider_challenge_headers" in quality["signals"]
    assert expected_hint in ", ".join(quality["details"])


def test_cloudflare_server_header_alone_does_not_reject_good_content(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><body><h1>Article</h1><p>Useful documentation content with enough words to avoid near-empty scoring.</p></body></html>",
            url=url,
            headers={"Content-Type": "text/html", "Server": "cloudflare"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    response = DirectHttpFetchProvider().fetch_response("https://example.com/article")

    assert "Useful documentation content" in response.content


def test_direct_http_meta_refresh_placeholder_is_degraded(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text="<html><head><meta http-equiv='refresh' content='0; url=/login'></head><body>Continue</body></html>",
            url=url,
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    assert "meta_refresh_placeholder" in exc_info.value.diagnostics["fetch_quality"]["signals"]


def test_direct_http_script_form_heavy_placeholder_is_degraded(monkeypatch):
    scripts = "".join("<script>check()</script>" for _ in range(6))

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text=f"<html><body>{scripts}<form></form><form></form>Security check</body></html>",
            url=url,
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    assert "script_form_heavy_placeholder" in exc_info.value.diagnostics["fetch_quality"]["signals"]


@pytest.mark.parametrize(
    ("body", "expected_signal"),
    [
        ("Unsupported browser. Please upgrade your browser to continue.", "unsupported_browser_placeholder"),
        ("Sign in to continue. Create an account to continue.", "login_or_consent_wall"),
        ("Accept cookies to continue. Manage consent.", "login_or_consent_wall"),
    ],
)
def test_direct_http_browser_login_cookie_placeholders_are_degraded(monkeypatch, body, expected_signal):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(
            200,
            text=f"<html><body>{body}</body></html>",
            url=url,
            headers={"Content-Type": "text/html"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/page")

    assert expected_signal in exc_info.value.diagnostics["fetch_quality"]["signals"]


def test_direct_http_cookie_login_article_false_positive_guard(monkeypatch):
    article = """
    <html><body><main><h1>Authentication and cookies</h1>
    <p>This documentation explains how login cookies work in a browser. It is a
    normal article with enough substantive content to avoid being confused with
    a cookie consent wall or login placeholder. Developers can read examples,
    implementation notes, and troubleshooting guidance here.</p>
    </main></body></html>
    """

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(200, text=article, url=url, headers={"Content-Type": "text/html"})

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    response = DirectHttpFetchProvider().fetch_response("https://example.com/docs/cookies")

    assert "normal article" in response.content


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


def test_direct_http_marks_403_terminal(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(403, text="forbidden", url=url)

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/forbidden")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.status_code == 403
    assert exc.retriable is False


def test_direct_http_marks_429_retriable(monkeypatch):
    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(429, text="too many requests", url=url)

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/rate-limited")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.status_code == 429
    assert exc.retriable is True


def test_direct_http_timeout_is_retriable(monkeypatch):
    import requests

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        raise requests.Timeout("slow response")

    monkeypatch.setattr(requests, "get", mock_get)

    with pytest.raises(WebBackendError) as exc_info:
        DirectHttpFetchProvider().fetch_response("https://example.com/slow")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.retriable is True
    assert "slow response" in str(exc)


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


def test_fetch_chain_falls_back_after_direct_http_429(monkeypatch):
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "direct_http,tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    calls = []

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(("get", url))
        return _MockResponse(429, text="too many requests", url=url)

    def mock_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url))
        return _MockResponse(200, {
            "results": [
                {"url": "https://example.com/rate-limited", "raw_content": "hosted fallback"},
            ],
            "failed_results": [],
        })

    import requests
    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(requests, "post", mock_post)

    response = get_fetch_orchestrator().fetch_response("https://example.com/rate-limited")

    assert calls == [
        ("get", "https://example.com/rate-limited"),
        ("post", "https://api.tavily.com/extract"),
    ]
    assert [attempt.provider for attempt in response.attempts] == ["direct_http", "tavily"]
    assert response.content == "hosted fallback"


def test_fetch_chain_does_not_fallback_after_direct_http_403(monkeypatch):
    monkeypatch.setenv("EGG_WEB_FETCH_CHAIN", "direct_http,tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        return _MockResponse(403, text="forbidden", url=url)

    def mock_post(url, json=None, headers=None, timeout=None):
        raise AssertionError("terminal 403 should not fall back to Tavily")

    import requests
    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(requests, "post", mock_post)

    with pytest.raises(WebBackendError) as exc_info:
        get_fetch_orchestrator().fetch_response("https://example.com/forbidden")

    exc = exc_info.value
    assert exc.provider == "direct_http"
    assert exc.status_code == 403
    assert exc.retriable is False


class CountingFetchProvider:
    def __init__(self, name="counting", *, degraded=False, content="Body"):
        self.name = name
        self.calls = 0
        self.degraded = degraded
        self.content = content

    def fetch_response(self, url):
        self.calls += 1
        return FetchResponse(
            final_url=f"{str(url).strip()}/final",
            content=self.content,
            content_type="text/plain",
            attempts=[
                FetchAttempt(
                    provider=self.name,
                    success=True,
                    degraded=self.degraded,
                    retriable=self.degraded,
                    message=f"{self.name} fetched",
                    diagnostics={"large": "x" * 2000},
                )
            ],
        )


class FailingFetchProvider:
    name = "failing"

    def __init__(self):
        self.calls = 0

    def fetch_response(self, url):
        self.calls += 1
        raise WebBackendError("temporary fetch failure", provider=self.name, retriable=True)


def test_fetch_orchestrator_reuses_cache_for_identical_normalized_url():
    provider = CountingFetchProvider()
    orchestrator = FetchOrchestrator([provider])

    first = orchestrator.fetch_response(" HTTPS://Example.com:443/Page#section ")
    second = orchestrator.fetch_response("https://example.com/Page")

    assert provider.calls == 1
    assert first.final_url == second.final_url
    assert second.content == "Body"

    # Mutating the returned object must not mutate the cached copy.
    first.content = "mutated"
    third = orchestrator.fetch_response("https://example.com/Page")
    assert third.content == "Body"
    assert provider.calls == 1


def test_fetch_cache_key_separates_url_and_provider_chain():
    provider = CountingFetchProvider("one")
    orchestrator = FetchOrchestrator([provider])

    orchestrator.fetch_response("https://example.com/a")
    orchestrator.fetch_response("https://example.com/b")

    other_provider = CountingFetchProvider("two")
    FetchOrchestrator([other_provider]).fetch_response("https://example.com/a")

    assert provider.calls == 2
    assert other_provider.calls == 1


def test_fetch_cache_is_bounded_by_max_entries():
    provider = CountingFetchProvider("bounded")
    orchestrator = FetchOrchestrator([provider], cache_max_entries=1)

    orchestrator.fetch_response("https://example.com/alpha")
    orchestrator.fetch_response("https://example.com/beta")
    orchestrator.fetch_response("https://example.com/alpha")

    assert provider.calls == 3


def test_fetch_cache_stores_final_url_alias_after_redirect():
    provider = CountingFetchProvider("redirect")
    orchestrator = FetchOrchestrator([provider])

    first = orchestrator.fetch_response("https://example.com/start")
    second = orchestrator.fetch_response("https://example.com/start/final")

    assert provider.calls == 1
    assert first.final_url == "https://example.com/start/final"
    assert second.final_url == "https://example.com/start/final"


def test_fetch_cache_ttl_zero_disables_cache():
    provider = CountingFetchProvider("ttl")
    orchestrator = FetchOrchestrator([provider], cache_ttl_sec=0)

    orchestrator.fetch_response("https://example.com/x")
    orchestrator.fetch_response("https://example.com/x")

    assert provider.calls == 2


def test_fetch_cache_skips_degraded_empty_and_oversized_responses():
    degraded_provider = CountingFetchProvider("degraded", degraded=True)
    FetchOrchestrator([degraded_provider]).fetch_response("https://example.com/degraded")
    FetchOrchestrator([degraded_provider]).fetch_response("https://example.com/degraded")
    assert degraded_provider.calls == 2

    empty_provider = CountingFetchProvider("empty", content="")
    orchestrator = FetchOrchestrator([empty_provider])
    orchestrator.fetch_response("https://example.com/empty")
    orchestrator.fetch_response("https://example.com/empty")
    assert empty_provider.calls == 2

    large_provider = CountingFetchProvider("large", content="abcdef")
    orchestrator = FetchOrchestrator([large_provider], cache_max_chars=5)
    orchestrator.fetch_response("https://example.com/large")
    orchestrator.fetch_response("https://example.com/large")
    assert large_provider.calls == 2


def test_fetch_cache_does_not_cache_provider_failures():
    failing = FailingFetchProvider()
    fallback = CountingFetchProvider("fallback")
    orchestrator = FetchOrchestrator([failing, fallback])

    first = orchestrator.fetch_response("https://example.com/fallback")
    second = orchestrator.fetch_response("https://example.com/fallback")

    assert failing.calls == 2
    assert fallback.calls == 2
    assert [attempt.provider for attempt in first.attempts] == ["failing", "fallback"]
    assert [attempt.provider for attempt in second.attempts] == ["failing", "fallback"]


def test_cached_fetch_attempt_diagnostics_are_bounded():
    provider = CountingFetchProvider("bounded-diagnostics")
    orchestrator = FetchOrchestrator([provider])

    orchestrator.fetch_response("https://example.com/diagnostics")
    cached = orchestrator.fetch_response("https://example.com/diagnostics")

    assert provider.calls == 1
    assert len(cached.attempts[0].diagnostics["large"]) < 600


def test_fetch_cache_ttl_env_can_disable_factory_cache(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    monkeypatch.setenv("EGG_WEB_FETCH_CACHE_TTL_SEC", "0")
    calls = []

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(url)
        return _MockResponse(
            200,
            text="uncached direct fetch",
            url=url,
            headers={"Content-Type": "text/plain"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    tools = create_default_tools()
    first = tools.execute("fetch_url", {"url": "https://example.com/fresh"})
    second = tools.execute("fetch_url", {"url": "https://example.com/fresh"})

    assert calls == ["https://example.com/fresh", "https://example.com/fresh"]
    assert first == second


def test_fetch_url_tool_output_is_preserved_from_cache(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    calls = []

    def mock_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(url)
        return _MockResponse(
            200,
            text="# Cached\n\nSame output",
            url="https://example.com/final",
            headers={"Content-Type": "text/markdown"},
        )

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    tools = create_default_tools()
    first = tools.execute("fetch_url", {"url": "https://example.com/start"})
    second = tools.execute("fetch_url", {"url": "https://example.com/start"})
    third = tools.execute("fetch_url", {"url": "https://example.com/final"})

    assert calls == ["https://example.com/start"]
    assert first == "URL: https://example.com/final\n\n# Cached\n\nSame output"
    assert second == first
    assert third == first
