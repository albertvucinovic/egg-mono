from typing import Dict, Any, Generator, Optional


def _usage_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nested_usage_int(obj: Dict[str, Any], *path: str) -> Optional[int]:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return _usage_int(cur)


def _image_input_tokens(provider_usage: Dict[str, Any]) -> Optional[int]:
    """Best-effort extraction of provider-confirmed image input tokens.

    Providers do not expose a universal multimodal usage schema.  OpenAI-like
    APIs that do break input down tend to put image tokens under
    ``prompt_tokens_details`` or ``input_tokens_details``; keep the raw
    provider_usage too so unknown future shapes remain inspectable.
    """

    for key in ("total_image_input_tokens", "image_input_tokens", "input_image_tokens", "image_tokens"):
        value = _usage_int(provider_usage.get(key))
        if value is not None:
            return value

    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        for token_key in ("image_tokens", "image_input_tokens", "input_image_tokens"):
            value = _nested_usage_int(provider_usage, details_key, token_key)
            if value is not None:
                return value
    return None


def normalize_provider_usage(provider_usage: Any) -> Dict[str, int]:
    """Normalize known provider usage objects to Egg's token fields."""
    if not isinstance(provider_usage, dict):
        return {}

    out: Dict[str, int] = {}

    def set_field(name: str, value: Optional[int]) -> None:
        if value is not None:
            out[name] = value

    if "prompt_tokens" in provider_usage or "completion_tokens" in provider_usage:
        set_field("total_input_tokens", _usage_int(provider_usage.get("prompt_tokens")))
        set_field("total_image_input_tokens", _image_input_tokens(provider_usage))
        set_field("total_output_tokens", _usage_int(provider_usage.get("completion_tokens")))
        set_field("cached_input_tokens", _nested_usage_int(provider_usage, "prompt_tokens_details", "cached_tokens"))
        set_field("total_reasoning_tokens", _nested_usage_int(provider_usage, "completion_tokens_details", "reasoning_tokens"))
        return out

    if "cache_read_input_tokens" in provider_usage or "cache_creation_input_tokens" in provider_usage:
        input_tokens = _usage_int(provider_usage.get("input_tokens")) or 0
        cache_creation = _usage_int(provider_usage.get("cache_creation_input_tokens")) or 0
        cache_read = _usage_int(provider_usage.get("cache_read_input_tokens")) or 0
        out["total_input_tokens"] = input_tokens + cache_creation + cache_read
        set_field("total_image_input_tokens", _image_input_tokens(provider_usage))
        out["cached_input_tokens"] = cache_read
        out["cache_creation_input_tokens"] = cache_creation
        set_field("total_output_tokens", _usage_int(provider_usage.get("output_tokens")))
        return out

    if "input_tokens" in provider_usage or "output_tokens" in provider_usage:
        set_field("total_input_tokens", _usage_int(provider_usage.get("input_tokens")))
        set_field("total_image_input_tokens", _image_input_tokens(provider_usage))
        set_field("total_output_tokens", _usage_int(provider_usage.get("output_tokens")))
        set_field("cached_input_tokens", _nested_usage_int(provider_usage, "input_tokens_details", "cached_tokens"))
        set_field("total_reasoning_tokens", _nested_usage_int(provider_usage, "output_tokens_details", "reasoning_tokens"))

    return out


def attach_provider_usage(message: Dict[str, Any], provider_usage: Any) -> None:
    if not isinstance(provider_usage, dict) or not provider_usage:
        return
    normalized = normalize_provider_usage(provider_usage)
    if normalized:
        message["api_usage"] = normalized
    message["provider_usage"] = provider_usage


class ProviderAdapter:
    """Base interface for provider adapters.

    Implementations must yield event dicts during streaming:
    - {"type":"content_delta","text": str}
    - {"type":"reasoning_delta","text": str}
    - {"type":"reasoning_summary_delta","text": str}
    - {"type":"tool_calls_delta","delta": list}
    - {"type":"done","message": dict}
    """

    def stream(self,
               url: str,
               headers: Dict[str, str],
               payload: Dict[str, Any],
               timeout: int = 600,
               session: Optional[Any] = None) -> Generator[Dict[str, Any], None, None]:
        raise NotImplementedError

    async def stream_async(self,
                   url: str,
                   headers: Dict[str, str],
                   payload: Dict[str, Any],
                   timeout: int = 600,
                   session: Optional[Any] = None):
        """Async variant of stream().

        Default implementation bridges the synchronous stream() into an async
        generator by running it in a background thread and forwarding events via
        an asyncio.Queue. Adapter implementations may override for true async IO.
        """
        import asyncio
        import threading

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        error_holder = {}

        def _producer():
            try:
                for evt in self.stream(url, headers, payload, timeout=timeout, session=session):
                    # Forward into the event loop thread-safely
                    fut = asyncio.run_coroutine_threadsafe(queue.put(evt), loop)
                    try:
                        fut.result()
                    except Exception:
                        break
            except Exception as e:
                error_holder['e'] = e
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        threading.Thread(target=_producer, daemon=True).start()
        while True:
            item = await queue.get()
            if item is None:
                if 'e' in error_holder:
                    raise error_holder['e']
                break
            yield item

