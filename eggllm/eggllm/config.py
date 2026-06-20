import json
import os
from pathlib import Path
from typing import Dict, Tuple, Any

from .capabilities import PROVIDER_MODEL_DEFAULT_KEYS


IMAGE_GENERATION_MODELS_FILENAME = "image-generation-models.json"


IMAGE_GENERATION_MODEL_DEFAULTS: Dict[str, Any] = {
    "model_kind": "image_generation",
    "task_capabilities": ["image_generation"],
    "input_modalities": ["text"],
    "output_modalities": ["image"],
}


IMAGE_GENERATION_PRESETS: Dict[str, Dict[str, Any]] = {
    "openai_image": {"api_type": "openai_images"},
    "openai_images": {"api_type": "openai_images"},
    "codex_image": {"api_type": "codex_images"},
    "codex_images": {"api_type": "codex_images"},
    "chatgpt_codex_image": {"api_type": "codex_images"},
    "chatgpt_codex_images": {"api_type": "codex_images"},
}


TEMPLATE_GUIDE = r'''
Your models.json can be organized by provider like this:
{
  "default_model": "OpenAI GPT-4o",
  "providers": {
    "openai": {
      "api_base": "https://api.openai.com/v1/chat/completions",
      "api_key_env": "OPENAI_API_KEY",
      "models": {
        "OpenAI GPT-4o": {"model_name": "gpt-4o-mini", "alias": ["g4o-mini"]},
        "OpenAI o3": {"model_name": "o3-mini", "reasoning": true}
      }
    }
  }
}
'''


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def default_image_generation_models_path(models_path: str | Path = "models.json") -> Path:
    """Return the sibling image-generation model config path.

    ``models.json`` owns provider credentials/base URLs.  The separate
    ``image-generation-models.json`` file owns only the image-generation
    backends that Egg's image-generation command/tool may use.
    """

    env_path = (os.environ.get("EGG_IMAGE_GENERATION_MODELS_PATH") or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path(models_path).with_name(IMAGE_GENERATION_MODELS_FILENAME)


def load_models_config(models_path: str | Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load configuration from a single models.json file organized by provider.

    Returns (models_config, providers_config)
    - models_config: flat mapping of display_name -> {provider, model_name, alias(list), ...}
    - providers_config: mapping provider -> {api_base, api_key_env, parameters?}; plus optional _meta: {default_model}
    """
    models_path = Path(models_path)
    data = _read_json(models_path)
    models_config: Dict[str, Dict[str, Any]] = {}
    providers_config: Dict[str, Dict[str, Any]] = {}
    if not isinstance(data, dict):
        return models_config, providers_config

    # New format with providers
    if "providers" in data and isinstance(data["providers"], dict):
        providers = data.get("providers", {})
        default_model = data.get("default_model")
        for prov_name, prov_obj in providers.items():
            if not isinstance(prov_obj, dict):
                continue
            api_base = prov_obj.get("api_base", "")
            api_key_env = prov_obj.get("api_key_env", "")
            parameters = prov_obj.get("parameters") if isinstance(prov_obj.get("parameters"), dict) else None
            providers_config[prov_name] = {
                "api_base": api_base,
                "api_key_env": api_key_env,
            }
            auth_type = prov_obj.get("auth_type")
            if auth_type:
                providers_config[prov_name]["auth_type"] = auth_type
            if parameters:
                providers_config[prov_name]["parameters"] = parameters
            for key in PROVIDER_MODEL_DEFAULT_KEYS:
                if key in prov_obj:
                    providers_config[prov_name][key] = prov_obj[key]

            models_map = prov_obj.get("models", {})
            if isinstance(models_map, dict):
                for display_name, m in models_map.items():
                    entry: Dict[str, Any] = {"provider": prov_name}
                    if isinstance(m, str):
                        entry["model_name"] = m
                        entry["alias"] = []
                    elif isinstance(m, dict):
                        entry.update(m)
                        alias = entry.get("alias", [])
                        if isinstance(alias, str):
                            alias = [alias]
                        elif not isinstance(alias, list):
                            alias = []
                        entry["alias"] = alias
                    else:
                        continue
                    models_config[display_name] = entry
        if default_model:
            providers_config.setdefault("_meta", {})["default_model"] = default_model
        return models_config, providers_config

    # Old format (flat models.json + providers.json) — minimal support
    # Keep backward compatibility in case it's used elsewhere.
    providers_json = models_path.with_name("providers.json")
    providers_data = _read_json(providers_json) or {}
    if isinstance(data, dict):
        for display_name, m in data.items():
            if not isinstance(m, dict):
                continue
            entry = dict(m)
            alias = entry.get("alias", [])
            if isinstance(alias, str):
                alias = [alias]
            elif not isinstance(alias, list):
                alias = []
            entry["alias"] = alias
            models_config[display_name] = entry
    if isinstance(providers_data, dict):
        providers_config.update(providers_data)
    return models_config, providers_config


def _normalize_alias(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _normalize_image_generation_model_entry(display_name: str, raw: Any) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    entry: Dict[str, Any] = dict(IMAGE_GENERATION_MODEL_DEFAULTS)
    preset = str(raw.get("preset") or "").strip().lower().replace("-", "_")
    if preset:
        entry.update(IMAGE_GENERATION_PRESETS.get(preset, {}))
    entry.update(raw)
    entry["alias"] = _normalize_alias(entry.get("alias"))
    entry.setdefault("model_name", display_name)
    return entry


def load_image_generation_models_config(
    image_generation_models_path: str | Path | None = None,
    *,
    models_path: str | Path = "models.json",
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load dedicated image-generation model config and shared providers.

    ``models_path`` is still read, but only for provider definitions such as
    API base URLs, API key env vars, OAuth auth type, and provider-level shared
    parameters.  The returned model mapping comes exclusively from
    ``image-generation-models.json``.  Normal chat models in ``models.json`` are
    deliberately not scanned as image-generation backends.

    Expected image-generation config shape::

        {
          "default_model": "OpenAI Image: gpt-image-1",
          "models": {
            "OpenAI Image: gpt-image-1": {
              "provider": "openai",
              "api_type": "openai_images",
              "model_name": "gpt-image-1"
            }
          }
        }

    The loader fills in the internal image-generation metadata defaults, so the
    user-facing file does not need to repeat ``model_kind`` or modality fields.
    """

    image_path = Path(image_generation_models_path) if image_generation_models_path is not None else default_image_generation_models_path(models_path)
    data = _read_json(image_path)
    _, providers_config = load_models_config(models_path)

    models_config: Dict[str, Dict[str, Any]] = {}
    if not isinstance(data, dict):
        return models_config, providers_config

    raw_models = data.get("models")
    if isinstance(raw_models, dict):
        for display_name, raw_model in raw_models.items():
            entry = _normalize_image_generation_model_entry(str(display_name), raw_model)
            if entry is None:
                continue
            models_config[str(display_name)] = entry

    default_model = data.get("default_model") or data.get("default")
    if isinstance(default_model, str) and default_model.strip():
        providers_config.setdefault("_meta", {})["default_model"] = default_model.strip()

    return models_config, providers_config


__all__ = [
    "IMAGE_GENERATION_MODELS_FILENAME",
    "IMAGE_GENERATION_MODEL_DEFAULTS",
    "IMAGE_GENERATION_PRESETS",
    "TEMPLATE_GUIDE",
    "default_image_generation_models_path",
    "load_image_generation_models_config",
    "load_models_config",
]

