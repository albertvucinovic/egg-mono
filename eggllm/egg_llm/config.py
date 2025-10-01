import json
from pathlib import Path
from typing import Dict, Tuple, Any


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
            if parameters:
                providers_config[prov_name]["parameters"] = parameters

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

