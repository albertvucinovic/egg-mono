"""Tool management commands for eggw backend."""
from __future__ import annotations

from eggthreads import (
    build_tool_call_states,
    disable_tool_for_thread,
    enable_tool_for_thread,
    set_thread_allow_raw_tool_output,
    get_thread_tools_config,
)

from models import CommandResponse
import core


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
    """Handle /toolsStatus command."""
    try:
        cfg = get_thread_tools_config(core.db, thread_id)
        status = "enabled" if cfg.llm_tools_enabled else "disabled"
        disabled = sorted(cfg.disabled_tools) if cfg.disabled_tools else []
        disabled_str = ", ".join(disabled) if disabled else "(none)"
        return CommandResponse(
            success=True,
            message=f"Tools: {status}\nDisabled: {disabled_str}",
            data={"enabled": cfg.llm_tools_enabled, "disabled": disabled},
        )
    except Exception as e:
        # Fallback to just listing tool calls
        states = build_tool_call_states(core.db, thread_id)
        if not states:
            return CommandResponse(success=True, message="No tool calls in this thread")

        lines = []
        for tc_id, tc in states.items():
            lines.append(f"  {tc.name} [{tc.state}] - {tc_id[-8:]}")

        return CommandResponse(
            success=True,
            message=f"Tool calls ({len(states)}):\n" + "\n".join(lines),
            data={"count": len(states)},
        )


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
