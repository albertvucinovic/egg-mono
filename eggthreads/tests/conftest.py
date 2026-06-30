from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_process_local_web_caches(monkeypatch):
    # Unit tests deliberately exercise the in-process memory REPL provider.
    # Production code blocks that provider when sandboxing is enabled unless an
    # explicit unsafe/dev override is present.
    monkeypatch.setenv("EGG_ALLOW_MEMORY_SESSION_WITH_SANDBOX", "1")
    from eggthreads.web import clear_fetch_cache, clear_search_cache

    clear_fetch_cache()
    clear_search_cache()
    yield
    clear_fetch_cache()
    clear_search_cache()
