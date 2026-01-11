"""Configuration loading for eggw backend."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .state import MODELS_PATH


def load_models_config() -> Tuple[Dict[str, Any], Optional[str]]:
    """Load models configuration using eggllm's config loader."""
    from eggllm.config import load_models_config as eggllm_load_models

    if not MODELS_PATH.exists():
        return {}, None

    models_config, _ = eggllm_load_models(MODELS_PATH)

    # Get default_model from the raw JSON
    default_model = None
    try:
        with open(MODELS_PATH) as f:
            raw_config = json.load(f)
            default_model = raw_config.get("default_model")
    except Exception:
        pass

    return models_config, default_model


def shorten_output_preview(text: str, max_lines: int = 200, max_chars: int = 8000) -> str:
    """Return a shortened preview for very long tool outputs.

    This keeps at most max_lines and max_chars of content and appends
    an ellipsis notice when truncation occurs.
    """
    if not isinstance(text, str) or not text:
        return ""
    lines = text.splitlines()
    truncated = text
    if len(lines) > max_lines:
        truncated = "\n".join(lines[:max_lines])
    if len(truncated) > max_chars:
        truncated = truncated[:max_chars]
    if truncated != text:
        truncated = truncated.rstrip()
        truncated += "\n\n...[output truncated for preview]..."
    return truncated
