from __future__ import annotations

"""Built-in skills plugin.

The shared service here is intentionally small: both the `skill` tool and the
future `/skills` + `/skill` commands should call the same renderer so behavior
stays aligned.
"""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolRegistry


def render_skill_request(args: Dict[str, Any]) -> str:
    """Render the requested skill listing/search/document."""

    from ..skills import render_skill_tool_output

    name = args.get("name")
    if name is None:
        # Accept a raw positional argument for local/tool bridge callers.
        name = args.get("_arg")
    query = args.get("query")
    return render_skill_tool_output(
        str(name) if name is not None else None,
        query=str(query) if query is not None else None,
    )


def register_skill_tool(registry: ToolRegistry) -> None:
    registry.register(
        name="skill",
        description=(
            "List available Egg skill documents, search skills, or load one skill by name. "
            "Skills are markdown instructions/examples/snippets; this tool is read-only "
            "and does not install new runtime APIs."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional skill name to load, for example 'rlm'. Omit to list skills.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional plain substring search over skill names, descriptions, and documents.",
                },
            },
        },
        impl=render_skill_request,
    )


@dataclass(frozen=True)
class SkillsPlugin:
    name: str = "skills"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        register_skill_tool(context.tool_registry)


__all__ = ["SkillsPlugin", "register_skill_tool", "render_skill_request"]
