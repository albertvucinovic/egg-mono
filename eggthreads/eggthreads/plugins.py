from __future__ import annotations

"""Internal plugin registration helpers.

This module intentionally starts small: plugins can register tools through a
shared context, and later phases can add command/provider/policy registries to
that same context without changing the basic plugin shape.
"""

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


class PluginContext(Protocol):
    """Registration context exposed to Egg plugins."""

    tool_registry: Any


class EggPlugin(Protocol):
    """Protocol for built-in and future external Egg plugins."""

    name: str
    version: str

    def register(self, context: PluginContext) -> None:
        """Register this plugin's contributions into the provided context."""


@dataclass(frozen=True)
class ToolPluginContext:
    """Plugin context used while constructing a tool registry."""

    tool_registry: Any


def register_plugins(context: PluginContext, plugins: Iterable[EggPlugin]) -> None:
    """Register plugins in deterministic caller-provided order."""

    for plugin in plugins:
        plugin.register(context)


@dataclass(frozen=True)
class FunctionPlugin:
    """Small adapter for registering an existing function as a plugin.

    This keeps the first plugin-manager step minimal: current monolithic
    registration code can be reused while the built-in tools are split into
    feature plugins in later phases.
    """

    name: str
    version: str
    register_func: Callable[[PluginContext], None]

    def register(self, context: PluginContext) -> None:
        self.register_func(context)


__all__ = [
    "EggPlugin",
    "FunctionPlugin",
    "PluginContext",
    "ToolPluginContext",
    "register_plugins",
]
