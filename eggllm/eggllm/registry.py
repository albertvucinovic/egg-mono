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

