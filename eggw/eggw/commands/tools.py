"""Tool management commands for eggw backend."""
from __future__ import annotations

import json
from typing import Dict, List, Any

from eggthreads import (
    build_tool_call_states,
    disable_tool_for_thread,
    enable_tool_for_thread,
    set_thread_allow_raw_tool_output,
    get_thread_tools_config,
    create_default_tools,
)

from ..models import CommandResponse
from .. import core


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all available tools with their specs.

    Returns a dict mapping tool name to {"spec": ..., "local_only": bool}
    """
    registry = create_default_tools()
    tools = {}
    for name, entry in registry._tools.items():
        tools[name] = {
            "spec": entry["spec"],
            "local_only": entry.get("local_only", False),
        }
    return tools


async def cmd_tools_on(thread_id: str) -> CommandResponse:
    """Handle /toolsOn command - enable all tools."""
    try:
        from eggthreads import set_thread_tools_enabled
        set_thread_tools_enabled(core.db, thread_id, True)
        return CommandResponse(success=True, message="Tools enabled for this thread")
    except Exception as e:
        return CommandResponse(success=False, message=f"Error: {e}")


async def cmd_tools_off(thread_id: str) -> CommandResponse:
    """Handle /toolsOff command - disable all tools."""
    try:
        from eggthreads import set_thread_tools_enabled
        set_thread_tools_enabled(core.db, thread_id, False)
        return CommandResponse(success=True, message="Tools disabled for this thread")
    except Exception as e:
        return CommandResponse(success=False, message=f"Error: {e}")


async def cmd_tools_status(thread_id: str) -> CommandResponse:
    """Handle /toolsStatus command - show tools configuration and available tools."""
    try:
        cfg = get_thread_tools_config(core.db, thread_id)
        available_tools = get_available_tools()

        # Build status message
        lines = []

        # Overall tools status
        tools_status = "ENABLED" if cfg.llm_tools_enabled else "DISABLED"
        lines.append(f"Tools for LLM: {tools_status}")

        # Secrets mode
        secrets_mode = "raw (secrets visible)" if cfg.allow_raw_tool_output else "masked"
        lines.append(f"Tool output secrets: {secrets_mode}")

        lines.append("")
        lines.append("Available tools:")

        # List all tools with their status
        disabled_set = {n.lower() for n in cfg.disabled_tools}
        tool_statuses = []
        for name, info in sorted(available_tools.items()):
            is_disabled = name.lower() in disabled_set
            is_local_only = info.get("local_only", False)

            status_parts = []
            if is_disabled:
                status_parts.append("DISABLED")
            else:
                status_parts.append("enabled")
            if is_local_only:
                status_parts.append("local-only")

            status_str = ", ".join(status_parts)
            lines.append(f"  {name}: {status_str}")
            tool_statuses.append({
                "name": name,
                "enabled": not is_disabled,
                "local_only": is_local_only,
            })

        lines.append("")
        lines.append("Use /disableTool <name> or /enableTool <name> to control individual tools")
        lines.append("Use /toolInfo <name> to see tool description")

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "llm_tools_enabled": cfg.llm_tools_enabled,
                "allow_raw_tool_output": cfg.allow_raw_tool_output,
                "disabled_tools": sorted(cfg.disabled_tools),
                "tools": tool_statuses,
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/toolsStatus error: {e}")


async def cmd_disable_tool(thread_id: str, tool_name: str) -> CommandResponse:
    """Handle /disableTool command."""
    tool_name = tool_name.strip()
    if not tool_name:
        return CommandResponse(
            success=False,
            message="Usage: /disableTool <tool_name>",
        )

    try:
        disable_tool_for_thread(core.db, thread_id, tool_name)
        return CommandResponse(
            success=True,
            message=f"Tool '{tool_name}' disabled for this thread",
            data={"tool": tool_name, "disabled": True},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/disableTool error: {e}")


async def cmd_enable_tool(thread_id: str, tool_name: str) -> CommandResponse:
    """Handle /enableTool command."""
    tool_name = tool_name.strip()
    if not tool_name:
        return CommandResponse(
            success=False,
            message="Usage: /enableTool <tool_name>",
        )

    try:
        enable_tool_for_thread(core.db, thread_id, tool_name)
        return CommandResponse(
            success=True,
            message=f"Tool '{tool_name}' enabled for this thread",
            data={"tool": tool_name, "enabled": True},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/enableTool error: {e}")


async def cmd_tools_secrets(thread_id: str, mode: str) -> CommandResponse:
    """Handle /toolsSecrets command - toggle raw output mode for tools."""
    mode = mode.strip().lower()

    if mode == "on":
        try:
            set_thread_allow_raw_tool_output(core.db, thread_id, True)
            return CommandResponse(
                success=True,
                message="Raw tool output enabled - secrets may be visible",
                data={"raw_output": True},
            )
        except Exception as e:
            return CommandResponse(success=False, message=f"/toolsSecrets error: {e}")
    elif mode == "off":
        try:
            set_thread_allow_raw_tool_output(core.db, thread_id, False)
            return CommandResponse(
                success=True,
                message="Raw tool output disabled - secrets will be filtered",
                data={"raw_output": False},
            )
        except Exception as e:
            return CommandResponse(success=False, message=f"/toolsSecrets error: {e}")
    else:
        return CommandResponse(
            success=False,
            message="Usage: /toolsSecrets <on|off>",
        )


async def cmd_tool_info(thread_id: str, tool_name: str) -> CommandResponse:
    """Handle /toolInfo command - show tool description in JSON format."""
    tool_name = tool_name.strip()
    if not tool_name:
        return CommandResponse(
            success=False,
            message="Usage: /toolInfo <tool_name>",
        )

    try:
        available_tools = get_available_tools()

        # Try exact match first, then case-insensitive
        tool_info = available_tools.get(tool_name)
        if not tool_info:
            # Try case-insensitive match
            for name, info in available_tools.items():
                if name.lower() == tool_name.lower():
                    tool_info = info
                    tool_name = name  # Use the canonical name
                    break

        if not tool_info:
            available_names = sorted(available_tools.keys())
            return CommandResponse(
                success=False,
                message=f"Tool '{tool_name}' not found.\nAvailable tools: {', '.join(available_names)}",
            )

        spec = tool_info["spec"]
        local_only = tool_info.get("local_only", False)

        # Format as JSON for display
        formatted_spec = json.dumps(spec, indent=2)

        lines = [
            f"Tool: {tool_name}",
            f"Local-only: {local_only}",
            "",
            "Spec (sent to LLM):",
            formatted_spec,
        ]

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "name": tool_name,
                "spec": spec,
                "local_only": local_only,
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/toolInfo error: {e}")
