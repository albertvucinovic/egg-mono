"""Tool management commands for eggw backend."""
from __future__ import annotations

from typing import Any, Dict

from eggthreads import (
    build_tool_call_states,
    disable_tool_for_thread,
    enable_tool_for_thread,
    set_thread_allow_raw_tool_output,
    get_thread_tools_config,
    get_tool_statuses_for_config,
)
from eggthreads.tool_help import collect_tool_entries, render_tool_help_request

from ..models import CommandResponse
from .. import core


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all available tools with their specs.

    Returns a dict mapping tool name to {"spec": ..., "local_only": bool}
    """
    return collect_tool_entries()


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

        policy_error = getattr(cfg, "policy_error", None)
        if policy_error:
            lines.append(f"Tool policy error (fail closed): {policy_error}")

        # Overall tools status
        llm_tools_enabled = bool(getattr(cfg, "llm_tools_enabled", True))
        tools_status = "ENABLED" if llm_tools_enabled else "DISABLED"
        lines.append(f"Tools for LLM: {tools_status}")

        # Secrets mode
        allow_raw_tool_output = bool(getattr(cfg, "allow_raw_tool_output", False))
        secrets_mode = "raw (secrets visible)" if allow_raw_tool_output else "masked"
        lines.append(f"Tool output secrets: {secrets_mode}")

        allowed_tools = getattr(cfg, "allowed_tools", None)
        if allowed_tools is None:
            lines.append("Tool allowlist: all registered tools")
            allowed_tools_data = None
        else:
            allowed_tools_data = sorted(allowed_tools)
            allowed_names = ", ".join(allowed_tools_data) or "(none)"
            lines.append(f"Tool allowlist: {allowed_names}")

        lines.append("")
        lines.append("Available tools:")

        # List all tools with their status
        tool_statuses = []
        for tool_status in get_tool_statuses_for_config(cfg, available_tools):
            status_parts = [tool_status["status_label"]]
            if tool_status.get("local_only", False):
                status_parts.append("local-only")

            status_str = ", ".join(status_parts)
            lines.append(f"  {tool_status['name']}: {status_str}")
            tool_statuses.append({
                "name": tool_status["name"],
                "enabled": tool_status["enabled"],
                "status": tool_status["status"],
                "disabled": tool_status["disabled"],
                "allowed_by_allowlist": tool_status["allowed_by_allowlist"],
                "local_only": tool_status["local_only"],
            })

        lines.append("")
        lines.append("Use /disableTool <name> or /enableTool <name> to control individual tools")
        lines.append("Use /toolInfo <name> to see tool description")

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "llm_tools_enabled": llm_tools_enabled,
                "allow_raw_tool_output": allow_raw_tool_output,
                "allowed_tools": allowed_tools_data,
                "disabled_tools": sorted(getattr(cfg, "disabled_tools", set()) or set()),
                "policy_error": policy_error,
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
    """Handle /toolInfo command using the shared tool help renderer."""
    tool_name = tool_name.strip()
    if not tool_name:
        return CommandResponse(
            success=False,
            message="Usage: /toolInfo <tool_name>",
        )

    try:
        available_tools = get_available_tools()
        result = render_tool_help_request(
            {"tool_name": tool_name},
            entries=available_tools,
            db=core.db,
            thread_id=thread_id,
            raw_context={
                "models_path": str(core.MODELS_PATH),
                "all_models_path": str(core.ALL_MODELS_PATH),
                "image_generation_models_path": str(core.IMAGE_GENERATION_MODELS_PATH),
            },
            default_include_schema=True,
            default_include_unavailable=True,
        )
        if not result.found:
            return CommandResponse(success=False, message=result.text)
        resolved_name = result.tool_name or tool_name
        tool_info = available_tools.get(resolved_name, {})

        return CommandResponse(
            success=True,
            message=result.text,
            data={
                "name": resolved_name,
                "spec": tool_info.get("spec"),
                "local_only": bool(tool_info.get("local_only", False)),
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/toolInfo error: {e}")
