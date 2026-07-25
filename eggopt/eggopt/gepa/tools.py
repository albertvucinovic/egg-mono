from __future__ import annotations

from eggthreads import ToolRegistry, create_default_tools

GEPA_SAFE_TOOLS = frozenset(
    {
        "python_exec",
        "python_repl",
        "bash",
        "bash_repl",
        "add_local_file_to_model_context",
        "read_long_tool_output",
        "skill",
        "tool_help",
        "threads",
        "execute_tool_in_other_thread",
    }
)


def gepa_safe_tools(
    tools: ToolRegistry | None = None,
    *,
    allowed_tools: set[str] | frozenset[str] | None = None,
) -> tuple[ToolRegistry, frozenset[str]]:
    """Return GEPA's mutation tool registry and explicit capabilities."""

    if tools is not None and not isinstance(tools, ToolRegistry):
        raise TypeError("tools must be an Eggthreads ToolRegistry or None")
    registry = create_default_tools() if tools is None else tools
    allowed = GEPA_SAFE_TOOLS if allowed_tools is None else frozenset(allowed_tools)
    available = {item["function"]["name"] for item in registry.tools_spec()}
    missing = allowed - available
    if missing:
        raise ValueError(
            f"GEPA tool registry is missing allowed tools: {sorted(missing)}"
        )
    return registry, allowed


def default_gepa_safe_tools() -> ToolRegistry:
    return gepa_safe_tools()[0]


__all__ = ["GEPA_SAFE_TOOLS", "default_gepa_safe_tools", "gepa_safe_tools"]
