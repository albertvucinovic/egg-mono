from typing import Dict, Any, Optional, List


class ModelRegistry:
    def __init__(self, models_config: Dict[str, Dict[str, Any]], providers_config: Dict[str, Dict[str, Any]], catalog):
        self.models_config = models_config or {}
        self.providers_config = providers_config or {}
        self.catalog = catalog  # AllModelsCatalog
        # Holds ephemeral entries for 'all:provider:model' during current session
        self._ephemeral: Dict[str, Dict[str, Any]] = {}

    def list_models_by_provider(self) -> Dict[str, List[str]]:
        by_provider: Dict[str, List[str]] = {}
        for name, cfg in self.models_config.items():
            prov = cfg.get("provider", "unknown")
            by_provider.setdefault(prov, []).append(name)
        return by_provider

    def default_model_key(self) -> Optional[str]:
        meta = self.providers_config.get("_meta")
        if isinstance(meta, dict):
            return meta.get("default_model")
        return None

    def _add_ephemeral(self, prov: str, mid: str) -> str:
        virtual_key = f"all:{prov}:{mid}"
        self.models_config[virtual_key] = self._ephemeral[virtual_key] = {
            "provider": prov,
            "model_name": mid,
            "alias": [],
        }
        return virtual_key

    def resolve(self, key: str) -> Optional[str]:
        if not key:
            return None
        dk = key.strip()
        if dk.lower().startswith('all:'):
            rest = dk[4:]
            if ':' not in rest:
                return None
            prov, _, mid = rest.partition(':')
            if not prov or not mid:
                return None
            catalog_ids = self.catalog.get_all_models_for_provider(prov) if self.catalog else []
            if mid in catalog_ids:
                return self._add_ephemeral(prov, mid)
            # allow blind ephemeral if user insists
            return self._add_ephemeral(prov, mid)

        if dk in self.models_config:
            return dk
        lk = dk.lower()
        for display, cfg in self.models_config.items():
            aliases = [a.lower() for a in (cfg.get("alias") or []) if isinstance(a, str)]
            if lk in aliases:
                return display
        if ":" in dk:
            prov, name = dk.split(":", 1)
            for display, cfg in self.models_config.items():
                if cfg.get("provider") == prov and (display == name or name.lower() in [a.lower() for a in cfg.get("alias", [])]):
                    return display
        return None

    def provider_config(self, provider: str) -> Dict[str, Any]:
        return self.providers_config.get(provider) or {}

    def get_model_config(self, display_key: str) -> Dict[str, Any]:
        return self.models_config.get(display_key) or {}

    def model_options(self, display_key: str) -> Dict[str, Any]:
        """Return per-model options dict for the given display key.

        The models.json schema allows an optional ``"options"`` object
        per model entry, e.g.::

            {
              "providers": {
                "openai": {
                  "models": {
                    "GPT 5.1 high": {
                      "model_name": "gpt-5.1-high",
                      "options": {
                        "thinking_content_policy": "last assistant turn",
                        "thinking_content_key": "thinking_content"
                      }
                    }
                  }
                }
              }
            }

        This helper returns that ``options`` mapping (or ``{}`` if
        absent / malformed) so that callers such as eggthreads can
        adjust behaviour (e.g. thinking-content handling) on a
        per-model basis.
        """
        cfg = self.get_model_config(display_key)
        opts = cfg.get("options") if isinstance(cfg, dict) else None
        return opts if isinstance(opts, dict) else {}

    def merge_parameters(self, display_key: str) -> Dict[str, Any]:
        m = self.get_model_config(display_key)
        prov = m.get("provider")
        prov_cfg = self.provider_config(prov)
        out: Dict[str, Any] = {}
        if isinstance(prov_cfg.get("parameters"), dict):
            out.update(prov_cfg["parameters"])
        if isinstance(m.get("parameters"), dict):
            out.update(m["parameters"])
        return out

    def get_concrete_model_info(self, display_key: str) -> Dict[str, Any]:
        """Return a dict with nested providers structure containing the provider config and the specific model config."""
        model_cfg = self.get_model_config(display_key)
        if not model_cfg:
            raise KeyError(f"Model not found: {display_key}")
        provider = model_cfg.get("provider")
        if not provider:
            raise KeyError(f"Model {display_key} has no provider")
        provider_cfg = self.provider_config(provider)
        # Preserve all provider-level keys (api_base, api_key_env, auth_type, parameters, etc.)
        prov_dict = {k: v for k, v in provider_cfg.items() if k != "models"}
        # Build model dict without 'provider' key
        model_dict = {k: v for k, v in model_cfg.items() if k != "provider"}
        # Ensure model_name is present (should be)
        if "model_name" not in model_dict:
            model_dict["model_name"] = display_key
        result = {
            "providers": {
                provider: {
                    **prov_dict,
                    "models": {
                        display_key: model_dict
                    }
                }
            }
        }
        return result

    def add_ephemeral_from_concrete_info(self, concrete_info: Dict[str, Any], display_key: Optional[str] = None) -> str:
        """Add provider and model config from concrete_info to the registry.

        concrete_info must have a "providers" dict with exactly one provider,
        and that provider must have a "models" dict with exactly one model.
        If display_key is provided, the model will be registered under that
        display key; otherwise the original key from concrete_info is used.
        """
        providers = concrete_info.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("concrete_info must have a 'providers' dict")
        if len(providers) != 1:
            raise ValueError("concrete_info must contain exactly one provider")
        provider_name = next(iter(providers))
        provider_info = providers[provider_name]
        # Merge into existing providers_config, preserving keys like auth_type
        # that may already be loaded from models.json
        existing = self.providers_config.get(provider_name, {})
        prov_cfg = dict(existing)
        for k, v in provider_info.items():
            if k == "models":
                continue
            prov_cfg[k] = v
        self.providers_config[provider_name] = prov_cfg
        # Process models
        models = provider_info.get("models")
        if not isinstance(models, dict):
            raise ValueError("Provider info must have a 'models' dict")
        if len(models) != 1:
            raise ValueError("Provider must have exactly one model in concrete_info")
        original_model_key, model_cfg = next(iter(models.items()))
        final_display_key = display_key if display_key is not None else original_model_key
        # Ensure provider field is set
        model_cfg_with_provider = dict(model_cfg)
        model_cfg_with_provider["provider"] = provider_name
        # Add to models_config
        self.models_config[final_display_key] = model_cfg_with_provider
        # Also add to _ephemeral for cleanup (optional)
        self._ephemeral[final_display_key] = model_cfg_with_provider
        return final_display_key

