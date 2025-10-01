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

