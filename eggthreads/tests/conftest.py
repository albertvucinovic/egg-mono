from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_process_local_web_caches():
    from eggthreads.web import clear_fetch_cache, clear_search_cache

    clear_fetch_cache()
    clear_search_cache()
    yield
    clear_fetch_cache()
    clear_search_cache()
