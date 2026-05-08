from __future__ import annotations

"""Built-in sandbox provider plugin."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext
from ..sandbox import BwrapProvider, DockerProvider, SrtProvider


def register_sandbox_providers(registry: Any) -> None:
    registry.register(SrtProvider())
    registry.register(DockerProvider())
    registry.register(BwrapProvider())


@dataclass(frozen=True)
class SandboxProvidersPlugin:
    name: str = "sandbox_providers"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.sandbox_provider_registry is not None:
            register_sandbox_providers(context.sandbox_provider_registry)


__all__ = [
    "SandboxProvidersPlugin",
    "register_sandbox_providers",
]
