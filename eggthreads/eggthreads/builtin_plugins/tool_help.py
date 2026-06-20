from __future__ import annotations

"""LLM-facing tool help/introspection plugin."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tool_help import HELP_TOOL_NAME, render_tool_help_request
from ..tools import ToolContext, ToolRegistry


def tool_help_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Return shared, context-aware help for Egg tools."""

    registry = ctx.raw.get("tool_registry")
    result = render_tool_help_request(
        args,
        registry=registry,
        db=ctx.db,
        thread_id=ctx.thread_id,
        raw_context=ctx.raw,
        default_include_schema=False,
        default_include_unavailable=False,
    )
    return result.text


def register_tool_help_tool(registry: ToolRegistry) -> None:
    registry.register(
        name=HELP_TOOL_NAME,
        description=(
            "Describe Egg tools on demand. Omit tool_name to list available tools, "
            "or pass a tool name to get parameters, examples, availability, and dynamic "
            "context such as currently configured image-generation models for generate_image."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Optional tool name to inspect, for example 'generate_image'. Omit to list tools available in this context.",
                },
                "include_schema": {
                    "type": "boolean",
                    "description": "When true, include the raw JSON schema/spec sent to the LLM for the selected tool.",
                },
                "include_unavailable": {
                    "type": "boolean",
                    "description": "When true, include tools that are local-only, disabled, or otherwise unavailable in the current thread listing.",
                },
            },
            "additionalProperties": False,
        },
        impl=tool_help_tool,
        accepts_context=True,
    )


@dataclass(frozen=True)
class ToolHelpPlugin:
    name: str = "tool_help"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_tool_help_tool(context.tool_registry)


__all__ = ["ToolHelpPlugin", "register_tool_help_tool", "tool_help_tool"]
