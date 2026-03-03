from typing import Dict, Any, Generator, Optional


class ProviderAdapter:
    """Base interface for provider adapters.

    Implementations must yield event dicts during streaming:
    - {"type":"content_delta","text": str}
    - {"type":"reasoning_delta","text": str}
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

