from __future__ import annotations

"""Built-in persistent session provider plugin."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext
from ..session import DockerSessionProvider, MemorySessionProvider


def register_session_providers(registry: Any) -> None:
    registry.register(MemorySessionProvider())
    registry.register(DockerSessionProvider())


@dataclass(frozen=True)
class SessionProvidersPlugin:
    name: str = "session_providers"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.session_provider_registry is not None:
            register_session_providers(context.session_provider_registry)


__all__ = [
    "SessionProvidersPlugin",
    "register_session_providers",
]
