from __future__ import annotations

import eggthreads as ts
from eggthreads.plugins import FunctionPlugin, ToolPluginContext, register_plugins
from eggthreads.tools import ToolRegistry, create_default_tools, create_tool_registry


def _tool_names(registry: ToolRegistry) -> list[str]:
    return sorted(registry._tools.keys())


def test_create_tool_registry_matches_default_tools() -> None:
    assert _tool_names(create_tool_registry()) == _tool_names(create_default_tools())


def test_function_plugin_registers_tool() -> None:
    registry = ToolRegistry()

    def register(context: ToolPluginContext) -> None:
        context.tool_registry.register(
            "example_tool",
            "Example tool",
            {"type": "object", "properties": {}},
            lambda args: "ok",
        )

    register_plugins(
        ToolPluginContext(tool_registry=registry),
        [FunctionPlugin("example", "0", register)],
    )

    assert registry.execute("example_tool", {}) == "ok"
    assert _tool_names(registry) == ["example_tool"]
