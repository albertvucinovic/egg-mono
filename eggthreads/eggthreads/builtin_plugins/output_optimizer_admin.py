from __future__ import annotations

"""Shared commands for native output optimizer configuration."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext
from ..output_optimizer.config import (
    OUTPUT_OPTIMIZER_MODES,
    format_thread_output_optimizer_status,
    normalize_output_optimizer_mode,
    set_thread_output_optimizer_enabled,
    set_thread_output_optimizer_mode,
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


def output_optimizer_status_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context, "outputOptimizerStatus")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        text = format_thread_output_optimizer_status(db, thread_id)
    except Exception as e:
        _log(context, f"/outputOptimizerStatus error: {e}")
        return CommandResult(clear_input=False)

    try:
        _log(context, "Output optimizer status (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Output Optimizer", text, border_style="blue")
        else:
            _log(context, text)
    except Exception:
        _log(context, text)
    return CommandResult(clear_input=True, message=text)


def _output_optimizer_enabled_command(context: Any, enabled: bool):
    from ..command_catalog import CommandResult

    command_name = "outputOptimizerOn" if enabled else "outputOptimizerOff"
    target = _target(context, command_name)
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        set_thread_output_optimizer_enabled(db, thread_id, enabled, reason=f"/{command_name}")
        status = "ENABLED" if enabled else "DISABLED"
        message = f"Native output optimizer {status} for this thread."
        _log(context, message)
    except Exception as e:
        _log(context, f"/{command_name} error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True, message=message)


def output_optimizer_on_command(context: Any, arg: str):
    return _output_optimizer_enabled_command(context, True)


def output_optimizer_off_command(context: Any, arg: str):
    return _output_optimizer_enabled_command(context, False)


def output_optimizer_mode_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    raw_mode = (arg or "").strip()
    try:
        mode = normalize_output_optimizer_mode(raw_mode)
    except ValueError:
        message = "Usage: /outputOptimizerMode conservative|balanced|aggressive"
        _log(context, message)
        return CommandResult(clear_input=False, message=message)

    target = _target(context, "outputOptimizerMode")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        set_thread_output_optimizer_mode(db, thread_id, mode, reason="/outputOptimizerMode")
        message = f"Native output optimizer mode set to {mode} for this thread."
        _log(context, message)
    except Exception as e:
        _log(context, f"/outputOptimizerMode error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True, message=message)


def _complete_modes(context: Any, arg: str):
    prefix = (arg or "").strip().lower()
    return [mode for mode in OUTPUT_OPTIMIZER_MODES if mode.startswith(prefix)]


def register_output_optimizer_admin_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(
        CommandSpec(
            "outputOptimizerStatus",
            output_optimizer_status_command,
            category="output optimizer",
            usage="/outputOptimizerStatus",
            description="Show native output optimizer status.",
        )
    )
    registry.register(
        CommandSpec(
            "outputOptimizerOn",
            output_optimizer_on_command,
            category="output optimizer",
            usage="/outputOptimizerOn",
            description="Enable native output optimization for this thread.",
        )
    )
    registry.register(
        CommandSpec(
            "outputOptimizerOff",
            output_optimizer_off_command,
            category="output optimizer",
            usage="/outputOptimizerOff",
            description="Disable native output optimization for this thread.",
        )
    )
    registry.register(
        CommandSpec(
            "outputOptimizerMode",
            output_optimizer_mode_command,
            category="output optimizer",
            usage="/outputOptimizerMode conservative|balanced|aggressive",
            description="Set native output optimizer mode.",
            complete=_complete_modes,
        )
    )


@dataclass(frozen=True)
class OutputOptimizerAdminPlugin:
    name: str = "output_optimizer_admin"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_output_optimizer_admin_commands(context.command_registry)


__all__ = [
    "OutputOptimizerAdminPlugin",
    "output_optimizer_mode_command",
    "output_optimizer_off_command",
    "output_optimizer_on_command",
    "output_optimizer_status_command",
    "register_output_optimizer_admin_commands",
]
