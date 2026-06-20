"""Configuration loading for eggw backend."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import state


def load_models_config() -> Tuple[Dict[str, Any], Optional[str]]:
    """Load models configuration using eggllm's config loader."""
    from eggllm.config import load_models_config as eggllm_load_models
    from eggllm.catalog import AllModelsCatalog
    from eggllm.registry import ModelRegistry

    models_path = state.MODELS_PATH
    if not models_path.exists():
        return {}, None

    models_config, providers_config = eggllm_load_models(models_path)

    # Get default_model from the raw JSON
    default_model = None
    try:
        registry = ModelRegistry(models_config, providers_config, AllModelsCatalog(state.ALL_MODELS_PATH))
        default_model = registry.default_chat_model_key()
    except Exception:
        try:
            with open(models_path) as f:
                raw_config = json.load(f)
                default_model = raw_config.get("default_model")
        except Exception:
            pass

    return models_config, default_model


def effective_model_config(key: str, config: dict, llm_client: Any = None) -> dict:
    """Return EggW's best effective model config for filtering/validation."""

    registry = getattr(llm_client, "registry", None) if llm_client is not None else None
    if registry is not None and hasattr(registry, "get_effective_model_config"):
        try:
            return registry.get_effective_model_config(key)
        except Exception:
            pass
    return config


def is_chat_model_key(key: str, config: dict, llm_client: Any = None) -> bool:
    """Return True when a configured model key is usable for normal chat."""

    from eggllm.capabilities import is_chat_model

    return is_chat_model(effective_model_config(key, config, llm_client))


def chat_model_keys(models_config: Dict[str, Any], llm_client: Any = None) -> list[str]:
    """Return configured model keys usable for normal chat, preserving order."""

    return [key for key, cfg in models_config.items() if is_chat_model_key(key, cfg, llm_client)]


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
