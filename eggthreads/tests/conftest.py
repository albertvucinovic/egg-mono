from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_process_local_search_cache():
    from eggthreads.web import clear_search_cache

    clear_search_cache()
    yield
    clear_search_cache()
