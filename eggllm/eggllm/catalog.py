import json
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from .provider_http import build_provider_headers


def derive_models_url(provider: str, provider_config: Dict[str, Any]) -> str:
    """Derive a provider's catalog endpoint from its configured API base.

    Returns either the derived URL or an ``Error: ...`` string when the
    provider cannot be refreshed via ``/updateAllModels``.
    """
    api_base = str(provider_config.get('api_base') or '')
    if not api_base:
        return f"Error: Provider '{provider}' is missing api_base in models.json."

    auth_type = provider_config.get('auth_type', 'api_key')
    if auth_type == 'chatgpt_oauth':
        # ChatGPT/Codex OAuth-backed providers are Responses-only and do not
        # expose an OpenAI-compatible /models endpoint that can be derived by
        # path rewriting. Their model catalog should be curated in models.json.
        return (
            f"Error: Provider '{provider}' uses ChatGPT OAuth / Responses API, "
            "which does not support catalog refresh via /updateAllModels. "
            "Add models explicitly to models.json instead."
        )

    # Derive a /models endpoint from an OpenAI-compatible base
    models_url = api_base.rstrip('/')
    for seg in ("/chat/completions", "/completions", "/responses"):
        if models_url.endswith(seg):
            models_url = models_url[: -len(seg)]
            break
    if not models_url.endswith('/models'):
        models_url = models_url + '/models'
    return models_url


def format_update_all_models_text(
    providers_config: Dict[str, Any],
    provider: Optional[str] = None,
    *,
    result: Optional[str] = None,
    all_models_path: Optional[str | Path] = None,
) -> str:
    """Return a human-readable help/status block for ``/updateAllModels``."""

    configured: list[tuple[str, Dict[str, Any]]] = []
    for name in sorted(providers_config.keys()):
        if name == '_meta':
            continue
        cfg = providers_config.get(name)
        if isinstance(cfg, dict):
            configured.append((name, cfg))

    def _auth_label(cfg: Dict[str, Any]) -> str:
        auth_type = str(cfg.get('auth_type') or 'api_key')
        if auth_type == 'chatgpt_oauth':
            return 'ChatGPT OAuth'
        return 'API key'

    def _status_text(message: Optional[str]) -> str:
        if not message:
            return 'info'
        if message.startswith('Error:'):
            return 'error'
        if message.startswith('Warning:'):
            return 'warning'
        return 'ok'

    lines: List[str] = []

    if not provider:
        lines.append('Update a provider model catalog and refresh all-models.json.')
        lines.append('')
        lines.append('Usage:')
        lines.append('  /updateAllModels <provider>')
        lines.append('')
        lines.append('Configured providers:')
        if configured:
            for name, cfg in configured:
                api_base = str(cfg.get('api_base') or '(missing api_base)')
                lines.append(f'  - {name} ({_auth_label(cfg)})')
                lines.append(f'      api_base: {api_base}')
        else:
            lines.append('  (none)')

        if all_models_path:
            lines.append('')
            lines.append(f'Catalog file: {all_models_path}')

        lines.append('')
        lines.append('What it does:')
        lines.append('  - fetches the provider model catalog')
        lines.append('  - writes or updates all-models.json')
        lines.append('  - enables /model all:provider:model selection and autocomplete')
        lines.append('')
        lines.append('How catalog discovery works:')
        lines.append('  - start from the provider api_base')
        lines.append('  - replace /chat/completions, /completions, or /responses with /models')
        lines.append('')
        lines.append('Note:')
        lines.append('  - a provider may support Responses inference but still not expose')
        lines.append('    a standard model-list endpoint for /updateAllModels')
        lines.append('  - in that case, define the models explicitly in models.json')
        return '\n'.join(lines)

    cfg = providers_config.get(provider)
    lines.append(f'Update All Models: {provider}')
    lines.append('')

    if not isinstance(cfg, dict):
        lines.append('Status:')
        lines.append('  error: unknown provider')
        lines.append('')
        lines.append('Configured providers:')
        if configured:
            for name, cfg2 in configured:
                lines.append(f'  - {name} ({_auth_label(cfg2)})')
        else:
            lines.append('  (none)')
        lines.append('')
        lines.append('Usage:')
        lines.append('  /updateAllModels <provider>')
        return '\n'.join(lines)

    api_base = str(cfg.get('api_base') or '(missing api_base)')
    models_url = derive_models_url(provider, cfg)
    auth_label = _auth_label(cfg)
    lines.append('Provider:')
    lines.append(f'  name:              {provider}')
    lines.append(f'  auth:              {auth_label}')
    api_key_env = str(cfg.get('api_key_env') or '').strip()
    if api_key_env:
        env_state = 'set' if os.environ.get(api_key_env) else 'missing'
        lines.append(f'  api_key_env:       {api_key_env} ({env_state})')
    lines.append(f'  api_base:          {api_base}')
    if models_url.startswith('Error:'):
        lines.append('  catalog_endpoint:  unavailable')
    else:
        lines.append(f'  catalog_endpoint:  {models_url}')

    if all_models_path:
        lines.append(f'  catalog_file:      {all_models_path}')

    if result is not None:
        msg_lines = [s for s in str(result).splitlines()] or ['']
        lines.append('')
        lines.append('Result:')
        lines.append(f'  status:            {_status_text(result)}')
        lines.append(f'  message:           {msg_lines[0]}')
        for extra in msg_lines[1:]:
            lines.append(f'                     {extra}')

    lines.append('')
    if result is None or _status_text(result) == 'ok':
        lines.append('Next:')
        lines.append(f'  - use /model all:{provider}:<model_id> to select a catalog model')
        lines.append(f'  - autocomplete should offer all:{provider}:... after refresh')
    elif str(cfg.get('auth_type') or 'api_key') == 'chatgpt_oauth':
        lines.append('Why this provider is different:')
        lines.append('  - it can accept Responses-style inference requests')
        lines.append('  - but /updateAllModels needs a separate model-list endpoint')
        lines.append('  - we do not currently know a standard catalog endpoint for')
        lines.append('    ChatGPT or Codex OAuth providers that we can derive safely')
        lines.append('')
        lines.append('What to do instead:')
        lines.append('  - add the models you want explicitly to models.json')
        lines.append('  - then select them with /model <name>')
    else:
        lines.append('Hint:')
        lines.append('  - verify the provider api_base and credentials')
        lines.append('  - then run /updateAllModels again')

    return '\n'.join(lines)


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

    def _derive_models_url(self, provider: str, provider_config: Dict[str, Any]) -> str:
        return derive_models_url(provider, provider_config)

    def update_provider(self, provider: str, providers_config: Dict[str, Any]) -> str:
        prov_cfg = providers_config.get(provider)
        if not isinstance(prov_cfg, dict):
            return f"Error: Unknown provider '{provider}'."
        models_url = self._derive_models_url(provider, prov_cfg)
        if models_url.startswith('Error:'):
            return models_url

        try:
            headers = build_provider_headers(provider, prov_cfg, accept_sse=False)
        except Exception as e:
            return f"Error: {e}"

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

