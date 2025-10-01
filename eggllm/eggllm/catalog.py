import json
import os
import time
from pathlib import Path
from typing import Dict, Any, List


class AllModelsCatalog:
    def __init__(self, all_models_path: str | Path):
        self._path = Path(all_models_path)
        self._providers: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and 'providers' in data:
                return data['providers']
        except Exception:
            pass
        return {}

    def _save(self):
        out = {"providers": self._providers}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

    def get_providers(self) -> List[str]:
        return list(self._providers.keys())

    def get_all_models_for_provider(self, provider: str) -> List[str]:
        prov = self._providers.get(provider) or {}
        models = prov.get('models')
        if isinstance(models, list):
            ids: List[str] = []
            for m in models:
                if isinstance(m, str):
                    ids.append(m)
                elif isinstance(m, dict) and m.get('id'):
                    ids.append(str(m['id']))
            return ids
        return []

    def get_all_models_suggestions(self, prefix: str) -> List[str]:
        base = 'all:'
        rest = prefix[len(base):]
        out: List[str] = []
        if ':' not in rest:
            for prov in sorted(self.get_providers()):
                cand = f"all:{prov}:"
                if cand.lower().startswith(prefix.lower()):
                    out.append(cand)
        else:
            prov, partial = rest.split(':', 1)
            for mid in self.get_all_models_for_provider(prov):
                cand = f"all:{prov}:{mid}"
                if cand.lower().startswith(prefix.lower()):
                    out.append(cand)
        return out

    def update_provider(self, provider: str, providers_config: Dict[str, Any]) -> str:
        prov_cfg = providers_config.get(provider)
        if not isinstance(prov_cfg, dict):
            return f"Error: Unknown provider '{provider}'."
        api_base = str(prov_cfg.get('api_base') or '')
        key_env = prov_cfg.get('api_key_env')
        api_key = os.environ.get(key_env) if key_env else None
        if not api_base:
            return f"Error: Provider '{provider}' is missing api_base in models.json."

        # Derive a /models endpoint from an OpenAI-compatible base
        models_url = api_base.rstrip('/')
        for seg in ("/chat/completions", "/completions", "/responses"):
            if models_url.endswith(seg):
                models_url = models_url[: -len(seg)]
                break
        if not models_url.endswith('/models'):
            models_url = models_url + '/models'

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        import requests
        try:
            resp = requests.get(models_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            return f"Error: Failed to fetch models from {provider}: {e}"
        try:
            data = resp.json()
        except Exception as e:
            return f"Error: Non-JSON response from {provider}: {e}"

        model_ids: List[str] = []
        if isinstance(data, dict):
            items = data.get('data')
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get('id'):
                        model_ids.append(str(it['id']))
        if not model_ids and isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get('id'):
                    model_ids.append(str(it['id']))
                elif isinstance(it, str):
                    model_ids.append(it)
        if not model_ids:
            return f"Warning: No models parsed from {provider} at {models_url}."

        self._providers[provider] = {
            'fetched_at': int(time.time()),
            'source': models_url,
            'models': model_ids,
        }
        self._save()
        return f"Updated all-models.json for provider '{provider}' with {len(model_ids)} models."

