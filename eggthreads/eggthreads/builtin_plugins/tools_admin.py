from __future__ import annotations

"""Built-in tools administration commands.

This plugin owns slash commands that configure Egg's tool registry and
per-thread tool policy. The concrete tool implementations live in their own
feature plugins; this module is the admin/control surface for that shared core
configuration.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..api import approve_tool_calls_for_thread
from ..plugins import PluginContext
from ..tools_config import (
    disable_tool_for_thread,
    enable_tool_for_thread,
    get_thread_tools_config,
    get_tool_statuses_for_config,
    set_thread_allow_raw_tool_output,
    set_thread_tools_enabled,
)


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def available_tools() -> dict[str, dict[str, Any]]:
    from ..tool_help import collect_tool_entries

    return collect_tool_entries()


def _tools_enabled_command(context: Any, enabled: bool):
    from ..command_catalog import CommandResult

    command_name = "toolsOn" if enabled else "toolsOff"
    target = _target(context, command_name)
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        set_thread_tools_enabled(db, thread_id, enabled)
        if enabled:
            _log(context, "Tools enabled for this thread (LLM may call tools).")
        else:
            _log(context, "Tools disabled for this thread (LLM tool calls suppressed).")
    except Exception as e:
        _log(context, f"/{command_name.lower()} error: {e}")
    return CommandResult(clear_input=True)


def tools_on_command(context: Any, arg: str):
    return _tools_enabled_command(context, True)


def tools_off_command(context: Any, arg: str):
    return _tools_enabled_command(context, False)


def disable_tool_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    name = (arg or "").strip()
    if not name:
        _log(context, "Usage: /disabletool <tool_name>")
        return CommandResult(clear_input=False)
    target = _target(context, "disableTool")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        disable_tool_for_thread(db, thread_id, name)
        _log(context, f"Tool '{name}' disabled for this thread.")
    except Exception as e:
        _log(context, f"/disabletool error: {e}")
    return CommandResult(clear_input=True)


def enable_tool_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    name = (arg or "").strip()
    if not name:
        _log(context, "Usage: /enabletool <tool_name>")
        return CommandResult(clear_input=False)
    target = _target(context, "enableTool")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        enable_tool_for_thread(db, thread_id, name)
        _log(context, f"Tool '{name}' enabled for this thread.")
    except Exception as e:
        _log(context, f"/enabletool error: {e}")
    return CommandResult(clear_input=True)


def tools_secrets_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    mode = (arg or "").strip().lower()
    if mode not in ("on", "off"):
        _log(context, "Usage: /toolsSecrets <on|off>  (on = allow raw tool output, off = mask secrets)")
        return CommandResult(clear_input=False)
    target = _target(context, "toolsSecrets")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    allow_raw = mode == "on"
    try:
        set_thread_allow_raw_tool_output(db, thread_id, allow_raw)
        if allow_raw:
            _log(context, "Tool output secrets: raw mode ENABLED (secrets will not be masked).")
        else:
            _log(context, "Tool output secrets: masking ENABLED (attempting to mask detected secrets).")
    except Exception as e:
        _log(context, f"/toolsecrets error: {e}")
    return CommandResult(clear_input=True)


def tools_status_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context, "toolsStatus")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        cfg = get_thread_tools_config(db, thread_id)
        tool_statuses = get_tool_statuses_for_config(cfg, available_tools())
    except Exception as e:
        _log(context, f"/toolStatus error: {e}")
        return CommandResult(clear_input=False)

    lines = []
    tools_status = "ENABLED" if cfg.llm_tools_enabled else "DISABLED"
    lines.append(f"Tools for LLM: {tools_status}")

    secrets_mode = "raw (secrets visible)" if getattr(cfg, "allow_raw_tool_output", False) else "masked"
    lines.append(f"Tool output secrets: {secrets_mode}")

    allowed_tools = getattr(cfg, "allowed_tools", None)
    if allowed_tools is None:
        lines.append("Tool allowlist: all registered tools")
    else:
        allowed_names = ", ".join(sorted(allowed_tools)) or "(none)"
        lines.append(f"Tool allowlist: {allowed_names}")

    lines.append("")
    lines.append("Available tools:")
    for tool_status in tool_statuses:
        status_parts = [tool_status["status_label"]]
        if tool_status.get("local_only", False):
            status_parts.append("local-only")
        lines.append(f"  {tool_status['name']}: {', '.join(status_parts)}")

    lines.append("")
    lines.append("Use /disableTool <name> or /enableTool <name> to control individual tools")
    lines.append("Use /toolInfo <name> to see tool description")

    text = "\n".join(lines)
    try:
        _log(context, "Tools status (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Tools Status", text, border_style="blue")
        else:
            _log(context, text)
    except Exception:
        _log(context, text)
    return CommandResult(clear_input=True)


def tool_info_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    from ..tool_help import render_tool_help_request

    tool_name = (arg or "").strip()
    if not tool_name:
        _log(context, "Usage: /toolInfo <tool_name>")
        return CommandResult(clear_input=False)

    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    raw_context = {
        key: value
        for key, value in {
            "models_path": getattr(context, "models_path", None),
            "all_models_path": getattr(context, "all_models_path", None),
            "image_generation_models_path": getattr(context, "image_generation_models_path", None),
        }.items()
        if value is not None
    }
    result = render_tool_help_request(
        {"tool_name": tool_name},
        entries=available_tools(),
        db=db,
        thread_id=thread_id,
        raw_context=raw_context,
        default_include_schema=True,
        default_include_unavailable=True,
    )
    text = result.text
    if not result.found:
        _log(context, text)
        return CommandResult(clear_input=False)

    resolved_name = result.tool_name or tool_name
    try:
        _log(context, f"Tool info: {resolved_name} (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block(f"Tool: {resolved_name}", text, border_style="blue")
        else:
            _log(context, text)
    except Exception:
        _log(context, text)
    return CommandResult(clear_input=True)


def toggle_auto_approval_for_thread(
    db: Any,
    thread_id: str,
    log_message: Callable[[str], None] | None,
    approve_func: Callable[..., Any],
) -> None:
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval' ORDER BY event_seq ASC",
            (thread_id,),
        )
        last_decision = None
        for (payload_json,) in cur.fetchall():
            try:
                payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
            except Exception:
                payload = {}
            decision = payload.get("decision")
            if decision in ("global_approval", "revoke_global_approval"):
                last_decision = decision
        enable = last_decision != "global_approval"
    except Exception:
        enable = True

    decision = "global_approval" if enable else "revoke_global_approval"
    try:
        approve_func(
            db,
            thread_id,
            decision=decision,
            reason="Toggled by user via /toggleAutoApproval",
        )
        if log_message is not None:
            log_message(
                "Global tool auto-approval ENABLED for this thread."
                if enable
                else "Global tool auto-approval DISABLED for this thread.",
            )
    except Exception as e:
        if log_message is not None:
            log_message(f"Error toggling auto-approval: {e}")


def toggle_auto_approval_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context, "toggleAutoApproval")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    toggle_auto_approval_for_thread(db, thread_id, context.log_system, approve_tool_calls_for_thread)
    return CommandResult(clear_input=True)


def register_tools_admin_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("toolsOn", tools_on_command, category="tools", usage="/toolsOn", description="Enable LLM tool calls for this thread."))
    registry.register(CommandSpec("toolsOff", tools_off_command, category="tools", usage="/toolsOff", description="Disable LLM tool calls for this thread."))
    registry.register(CommandSpec("disableTool", disable_tool_command, category="tools", usage="/disableTool <name>", description="Disable a specific tool for this thread."))
    registry.register(CommandSpec("enableTool", enable_tool_command, category="tools", usage="/enableTool <name>", description="Enable a specific tool for this thread."))
    registry.register(CommandSpec("toolsStatus", tools_status_command, category="tools", usage="/toolsStatus", description="Show tool configuration and availability."))
    registry.register(CommandSpec("toolInfo", tool_info_command, category="tools", usage="/toolInfo <name>", description="Show a tool schema and metadata."))
    registry.register(CommandSpec("toolsSecrets", tools_secrets_command, category="tools", usage="/toolsSecrets <on|off>", description="Toggle raw tool output in local UI."))
    registry.register(CommandSpec("toggleAutoApproval", toggle_auto_approval_command, category="tools", usage="/toggleAutoApproval", description="Toggle global tool auto-approval."))


@dataclass(frozen=True)
class ToolsAdminPlugin:
    name: str = "tools_admin"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_tools_admin_commands(context.command_registry)


__all__ = [
    "ToolsAdminPlugin",
    "available_tools",
    "disable_tool_command",
    "enable_tool_command",
    "register_tools_admin_commands",
    "toggle_auto_approval_command",
    "toggle_auto_approval_for_thread",
    "tool_info_command",
    "tools_off_command",
    "tools_on_command",
    "tools_secrets_command",
    "tools_status_command",
]
