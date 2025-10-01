import os
from pathlib import Path
from typing import Dict, Any, Generator, Optional, List

from .config import load_models_config
from .catalog import AllModelsCatalog
from .registry import ModelRegistry
from .providers.openai_compat import OpenAICompatAdapter


class LLMClient:
    def __init__(self, models_path: str | Path = "models.json", all_models_path: str | Path = "all-models.json"):
        self.models_path = Path(models_path)
        self.all_models_path = Path(all_models_path)

        models_config, providers_config = load_models_config(self.models_path)
        if not models_config:
            raise ValueError("No models configured in models.json")
        self.catalog = AllModelsCatalog(self.all_models_path)
        self.registry = ModelRegistry(models_config, providers_config, self.catalog)

        # Model selection precedence: EG_CHILD_MODEL > DEFAULT_MODEL > config default > first
        desired = os.environ.get("EG_CHILD_MODEL") or os.environ.get("DEFAULT_MODEL") or self.registry.default_model_key()
        self.current_model_key: Optional[str] = None
        if desired:
            resolved = self.registry.resolve(desired)
            if resolved:
                self.current_model_key = resolved
        if not self.current_model_key:
            self.current_model_key = list(models_config.keys())[0]

        self._provider_adapter = OpenAICompatAdapter()  # default adapter

    # Public: configuration / model management
    def get_providers(self) -> List[str]:
        return [p for p in self.registry.providers_config.keys() if p != "_meta"]

    def list_models_by_provider(self) -> Dict[str, List[str]]:
        byp = self.registry.list_models_by_provider()
        for v in byp.values():
            v.sort()
        return dict(sorted(byp.items()))

    def set_model(self, key: str) -> str:
        resolved = self.registry.resolve(key)
        if not resolved:
            raise KeyError(f"Unknown model: {key}")
        self.current_model_key = resolved
        # Also propagate as environment for consumers that rely on it
        os.environ["EG_CHILD_MODEL"] = resolved
        os.environ["DEFAULT_MODEL"] = resolved
        return resolved

    def current_provider_and_url(self) -> tuple[str, str, Dict[str, str]]:
        mc = self.registry.get_model_config(self.current_model_key)
        provider_name = mc.get("provider")
        pc = self.registry.provider_config(provider_name)
        if not pc:
            raise KeyError(f"Provider '{provider_name}' not found")
        base_url = pc.get("api_base")
        if not base_url:
            raise ValueError(f"Provider '{provider_name}' missing api_base")

        headers = {"Content-Type": "application/json"}
        api_key_env = pc.get("api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise EnvironmentError(f"Env var '{api_key_env}' is not set for '{provider_name}'")
            headers["Authorization"] = f"Bearer {api_key}"
        return provider_name, base_url, headers

    def update_all_models(self, provider: str) -> str:
        return self.catalog.update_provider(provider, self.registry.providers_config)

    # Public: chat streaming
    def stream_chat(self,
                    messages: List[Dict[str, Any]],
                    tools: Optional[List[Dict[str, Any]]] = None,
                    tool_choice: Optional[str] = "auto",
                    timeout: int = 600,
                    extra_headers: Optional[Dict[str, str]] = None) -> Generator[Dict[str, Any], None, None]:
        mc = self.registry.get_model_config(self.current_model_key)
        api_model_name = mc.get("model_name")
        if not api_model_name:
            raise ValueError("API model name not found")

        provider_name, base_url, headers = self.current_provider_and_url()
        if extra_headers:
            headers = {**headers, **extra_headers}

        # Sanitize messages: remove local-only keys the provider won't accept
        def _sanitize(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            sanitized = []
            keys_to_remove = {"reasoning_content", "model_key", "local_tool"}
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                if role == "tool" and m.get("local_tool"):
                    continue
                s = {k: v for k, v in m.items() if k not in keys_to_remove}
                if s.get("content") is None and "tool_calls" not in s:
                    s["content"] = ""
                if s.get("role") == "assistant" and "tool_calls" in s and not s["tool_calls"]:
                    del s["tool_calls"]
                sanitized.append(s)
            return sanitized

        merged_params = self.registry.merge_parameters(self.current_model_key)
        payload: Dict[str, Any] = {
            "model": api_model_name,
            "messages": _sanitize(messages),
            "stream": True,
        }
        if tools is not None:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        payload.update(merged_params)

        # Use OpenAI-compatible adapter for now
        for evt in self._provider_adapter.stream(base_url, headers, payload, timeout=timeout):
            yield evt

    def complete_chat(self,
                      messages: List[Dict[str, Any]],
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[str] = "auto",
                      timeout: int = 600,
                      extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        final: Optional[Dict[str, Any]] = None
        for evt in self.stream_chat(messages, tools=tools, tool_choice=tool_choice, timeout=timeout, extra_headers=extra_headers):
            if evt.get("type") == "done":
                final = evt.get("message")
        return final or {"role": "assistant", "content": ""}

    def send_context_only(self, messages: List[Dict[str, Any]], context_message: str, tools: Optional[List[Dict[str, Any]]] = None):
        mc = self.registry.get_model_config(self.current_model_key)
        api_model_name = mc.get("model_name")
        if not api_model_name:
            raise ValueError("API model name not found")
        provider_name, base_url, headers = self.current_provider_and_url()

        def _sanitize(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            keys_to_remove = {"reasoning_content", "model_key", "local_tool"}
            out: List[Dict[str, Any]] = []
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get("role") == "tool" and m.get("local_tool"):
                    continue
                s = {k: v for k, v in m.items() if k not in keys_to_remove}
                out.append(s)
            return out

        base_messages = _sanitize(messages)
        one_off = base_messages + [{"role": "user", "content": context_message}]

        import requests
        payload = {
            "model": api_model_name,
            "messages": one_off,
            "stream": False,
            "max_tokens": 1,
        }
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        requests.post(base_url, headers=headers, json=payload, timeout=30).raise_for_status()

