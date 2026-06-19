from __future__ import annotations

from eggthreads.tools import create_default_tools
from eggthreads.web import SearchAttempt, SearchResponse, SearchResult, clear_search_cache
from eggthreads.web.search import SearchOrchestrator


class CountingProvider:
    def __init__(self, name="counting", *, degraded_empty=False):
        self.name = name
        self.calls = 0
        self.degraded_empty = degraded_empty

    def search_response(self, query, max_results=5):
        self.calls += 1
        query_slug = "-".join(str(query or "").split()).lower()
        if self.degraded_empty:
            return SearchResponse(
                results=[],
                attempts=[
                    SearchAttempt(
                        provider=self.name,
                        success=True,
                        degraded=True,
                        retriable=True,
                        message=f"{self.name} degraded",
                        diagnostics={"large": "x" * 2000},
                    )
                ],
            )
        return SearchResponse(
            results=[
                SearchResult(
                    title=f"{self.name} {query} {i}",
                    url=f"https://{self.name}{i}.example/{query_slug}",
                    snippet=f"Snippet {i}",
                )
                for i in range(max_results)
            ],
            attempts=[SearchAttempt(provider=self.name, success=True, message="ok")],
        )


def test_search_orchestrator_reuses_cache_for_identical_normalized_query():
    provider = CountingProvider()
    orchestrator = SearchOrchestrator([provider])

    first = orchestrator.search_response("  Hello   World  ", max_results=2)
    second = orchestrator.search_response("hello world", max_results=2)

    assert provider.calls == 1
    assert [result.url for result in first.results] == [result.url for result in second.results]

    # Mutating the returned object must not mutate the cached copy.
    first.results[0].title = "mutated"
    third = orchestrator.search_response("hello world", max_results=2)
    assert third.results[0].title != "mutated"
    assert provider.calls == 1


def test_search_cache_key_separates_query_max_results_and_provider_chain():
    provider = CountingProvider("one")
    orchestrator = SearchOrchestrator([provider])

    orchestrator.search_response("alpha", max_results=1)
    orchestrator.search_response("alpha", max_results=2)
    orchestrator.search_response("beta", max_results=1)

    other_provider = CountingProvider("two")
    SearchOrchestrator([other_provider]).search_response("alpha", max_results=1)

    assert provider.calls == 3
    assert other_provider.calls == 1


def test_search_cache_is_bounded_by_max_entries():
    provider = CountingProvider("bounded")
    orchestrator = SearchOrchestrator([provider], cache_max_entries=1)

    orchestrator.search_response("alpha", max_results=1)
    orchestrator.search_response("beta", max_results=1)
    orchestrator.search_response("alpha", max_results=1)

    assert provider.calls == 3


def test_degraded_empty_cache_can_use_short_zero_ttl():
    provider = CountingProvider("degraded", degraded_empty=True)
    orchestrator = SearchOrchestrator([provider], degraded_empty_cache_ttl_sec=0)

    first = orchestrator.search_response("x")
    second = orchestrator.search_response("x")

    assert first.degraded_empty
    assert second.degraded_empty
    assert provider.calls == 2


def test_cached_attempt_diagnostics_are_bounded():
    provider = CountingProvider("degraded", degraded_empty=True)
    orchestrator = SearchOrchestrator([provider], degraded_empty_cache_ttl_sec=60)

    orchestrator.search_response("x")
    cached = orchestrator.search_response("x")

    assert provider.calls == 1
    assert len(cached.attempts[0].diagnostics["large"]) < 600


def test_web_search_tool_output_is_preserved_from_cache(monkeypatch):
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    calls = []

    class _MockResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "results": [
                    {"title": "Cached", "url": "https://cached.example", "content": "same snippet"},
                ]
            }

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params["q"]))
        return _MockResponse()

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    tools = create_default_tools()
    first = tools.execute("web_search", {"query": "cache me", "max_results": 1})
    second = tools.execute("web_search", {"query": " cache   me ", "max_results": 1})

    assert calls == [("http://localhost:8888/search", "cache me")]
    assert first == second
    assert "https://cached.example" in second


def test_search_cache_ttl_env_can_disable_factory_cache(monkeypatch):
    monkeypatch.setenv("EGG_WEB_BACKEND", "searxng")
    monkeypatch.setenv("EGG_WEB_SEARCH_CACHE_TTL_SEC", "0")
    calls = []

    class _MockResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "results": [
                    {"title": "Uncached", "url": "https://uncached.example", "content": "fresh"},
                ]
            }

    def mock_get(url, params=None, headers=None, timeout=None):
        calls.append(params["q"])
        return _MockResponse()

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    tools = create_default_tools()
    tools.execute("web_search", {"query": "fresh", "max_results": 1})
    tools.execute("web_search", {"query": "fresh", "max_results": 1})

    assert calls == ["fresh", "fresh"]
