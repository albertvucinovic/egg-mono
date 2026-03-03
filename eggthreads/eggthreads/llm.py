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

    try:
        from eggllm import LLMClient  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "eggllm is required to run LLM turns (RA1) but could not be imported. "
            "Install eggllm (or ensure it is on PYTHONPATH). "
            f"Import error: {exc}"
        ) from exc

    return LLMClient(models_path=models_path, all_models_path=all_models_path)


__all__ = ["create_llm_client"]
