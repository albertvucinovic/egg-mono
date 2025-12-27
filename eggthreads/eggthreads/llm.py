from __future__ import annotations

"""eggthreads helpers for constructing an LLM client.

eggthreads is designed to *optionally* integrate with eggllm.
Applications that need to run LLM turns (RA1) should construct a
provider client explicitly and pass it to :class:`eggthreads.SubtreeScheduler`
or :class:`eggthreads.ThreadRunner`.

This module provides a small convenience wrapper so examples and apps
can depend only on the eggthreads public API.
"""

from pathlib import Path
from typing import Any

import sys


def create_llm_client(
    *,
    models_path: str | Path = "models.json",
    all_models_path: str | Path = "all-models.json",
) -> Any:
    """Create and return an ``eggllm.LLMClient``.

    This function intentionally raises a clear error if eggllm is not
    importable. eggthreads does not silently fall back to a dummy client
    because the runner cannot make progress without streaming.
    """

    # eggllm is an optional dependency at packaging time, but required to
    # actually run LLM turns. In the mono-repo development layout, eggllm
    # is often a sibling directory of eggthreads. We support that layout
    # by adding the sibling folder to sys.path when present.
    try:
        from eggllm import LLMClient  # type: ignore
    except Exception as e1:
        # Mono-repo development layout: <repo>/eggthreads/eggthreads/llm.py
        # and <repo>/eggllm/eggllm/__init__.py.
        try:
            repo_root = Path(__file__).resolve().parents[2]
            eggllm_root = (repo_root / "eggllm").resolve()
            if eggllm_root.exists() and str(eggllm_root) not in sys.path:
                sys.path.insert(0, str(eggllm_root))
            from eggllm import LLMClient  # type: ignore
        except Exception as e2:
            raise RuntimeError(
                "eggllm is required to run LLM turns (RA1) but could not be imported. "
                "Install eggllm (or ensure it is on PYTHONPATH). "
                f"Import error: {e1}"
            ) from e2

    return LLMClient(models_path=models_path, all_models_path=all_models_path)


__all__ = ["create_llm_client"]
