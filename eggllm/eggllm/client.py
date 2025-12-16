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

    # -------- Cost helpers -------------------------------------------------
    def current_model_cost_config(self, model_key: Optional[str] = None) -> Dict[str, Any]:
        """Return the per-1K-token cost configuration for a model.

        ``models.json`` may include an optional ``"cost"`` object per
        model entry::

            {
              "providers": {
                "openai": {
                  "models": {
                    "OpenAI GPT-4o": {
                      "model_name": "gpt-4o-mini",
                      "cost": {
                        "input_tokens": 0.25,
                        "cached_input": 0.05,
                        "output_tokens": 1.00
                      }
                    }
                  }
                }
              }
            }

        Each value is interpreted as *cents per 1K tokens* in the
        corresponding tier.  We convert these to dollars internally by
        dividing by 100.
        """

        key = model_key or self.current_model_key
        try:
            mc = self.registry.get_model_config(key)
        except Exception:
            mc = {}
        cost = mc.get("cost") or {}
        if not isinstance(cost, dict):
            cost = {}

        def _cents_to_usd(v: Any) -> float:
            try:
                cents = float(v)
            except Exception:
                return 0.0
            return cents / 100.0

        out = {
            "input_tokens": _cents_to_usd(cost.get("input_tokens") or 0.0),
            "cached_input": _cents_to_usd(cost.get("cached_input") or 0.0),
            "output_tokens": _cents_to_usd(cost.get("output_tokens") or 0.0),
        }
        return out

    def approximate_thread_cost(self, api_usage: Dict[str, Any], model_key: Optional[str] = None) -> Dict[str, float]:
        """Approximate dollar cost for a thread given API token usage.

        ``api_usage`` is expected to be the structure produced by
        eggthreads' token counting, with at least::

            {
              "total_input_tokens": int,
              "total_output_tokens": int,
              "cached_tokens": int,
            }

        We apply the current model's cost config (per 1K tokens) and
        return::

            {
              "input":  <float dollars>,
              "output": <float dollars>,
              "cached": <float dollars>,
              "total":  <float dollars>,
            }

        Missing cost config or malformed api_usage yields zeros.
        """

        try:
            # Total logical input tokens seen across all calls.
            cin_total = int(api_usage.get("total_input_tokens") or 0)
            cout = int(api_usage.get("total_output_tokens") or 0)
            # Heuristic estimate of how many of those input tokens
            # were served from KV cache rather than re-sent in full.
            ccached = int(api_usage.get("cached_input_tokens") or 0)
        except Exception:
            cin_total = cout = ccached = 0

        cost_cfg = self.current_model_cost_config(model_key)
        pin = float(cost_cfg.get("input_tokens") or 0.0)
        pcached = float(cost_cfg.get("cached_input") or 0.0)
        pout = float(cost_cfg.get("output_tokens") or 0.0)

        def _usd(tokens: int, price_per_1k: float) -> float:
            if tokens <= 0 or price_per_1k <= 0.0:
                return 0.0
            return float(tokens) * (price_per_1k / 1000.0)

        # To avoid double-counting, we treat cached input tokens as a
        # separate, cheaper tier. The remaining "new" tokens are
        # billed at the full input rate.
        new_input_tokens = max(cin_total - ccached, 0)

        c_in = _usd(new_input_tokens, pin)
        c_cached = _usd(ccached, pcached)
        c_out = _usd(cout, pout)
        total = c_in + c_cached + c_out
        return {
            "input": c_in,
            "cached": c_cached,
            "output": c_out,
            "total": total,
        }

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

    def update_all_providers(self, providers: Optional[List[str]] = None) -> Dict[str, str]:
         """Update all-models.json catalogs for multiple providers.

         - If `providers` is None, updates all configured providers (excluding meta keys).
         - Returns a mapping provider -> result message (success or error/warning).
         """
         results: Dict[str, str] = {}
         if providers is None:
             providers = [p for p in self.get_providers() if p != "_meta"]
         for prov in providers:
             try:
                 results[prov] = self.update_all_models(prov)
             except Exception as e:
                 results[prov] = f"Error: {e}"
         return results

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
            """Sanitize messages before sending to the provider.

            We always strip internal bookkeeping keys such as
            ``model_key`` and ``local_tool``.  Reasoning/thinking
            content is already shaped by eggthreads according to the
            per-model thinking options; at this layer we simply avoid
            deleting an arbitrary thinking key.
            """
            sanitized = []
            keys_to_remove = {"model_key", "local_tool"}
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

    async def astream_chat(self,
                    messages: List[Dict[str, Any]],
                    tools: Optional[List[Dict[str, Any]]] = None,
                    tool_choice: Optional[str] = "auto",
                    timeout: int = 600,
                    extra_headers: Optional[Dict[str, str]] = None):
        """Async streaming variant of stream_chat().

        This keeps the synchronous API untouched for chat.sh and other callers,
        while providing an asyncio-friendly interface for eggthreads.
        """
        mc = self.registry.get_model_config(self.current_model_key)
        api_model_name = mc.get("model_name")
        if not api_model_name:
            raise ValueError("API model name not found")

        provider_name, base_url, headers = self.current_provider_and_url()
        if extra_headers:
            headers = {**headers, **extra_headers}

        def _sanitize(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            sanitized = []
            keys_to_remove = {"model_key", "local_tool"}
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

        async for evt in self._provider_adapter.stream_async(base_url, headers, payload, timeout=timeout):
            yield evt

    # Backward-compat alias
    stream_chat_async = astream_chat

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
            keys_to_remove = {"model_key", "local_tool"}
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

