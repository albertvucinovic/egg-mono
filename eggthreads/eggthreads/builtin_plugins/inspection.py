from __future__ import annotations

"""Shared read-only transcript inspection command."""

from dataclasses import dataclass
from typing import Any

from ..inspection import (
    resolve_show_record,
    show_record_completion_items,
    show_record_target,
)
from ..plugins import PluginContext


def _target(context: Any) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        return None
    return db, str(thread_id)


def show_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context)
    if target is None:
        return CommandResult(clear_input=False, message="/show failed: no current thread.")
    db, thread_id = target
    resolution = resolve_show_record(db, thread_id, arg)
    if resolution.status != "selected" or resolution.selected is None:
        return CommandResult(clear_input=False, message=resolution.message)

    payload = show_record_target(
        resolution.selected,
        watermark_event_seq=resolution.watermark_event_seq,
    )
    return CommandResult(
        clear_input=True,
        message=resolution.message,
        data={"action": "show_record", "target": payload, "suppress_transcript": True},
    )


def show_completions(context: Any, arg: str):
    target = _target(context)
    if target is None:
        return []
    db, thread_id = target
    return show_record_completion_items(db, thread_id, arg)


def register_inspection_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(
        CommandSpec(
            "show",
            show_command,
            category="display",
            usage="/show <id_hint>",
            description=(
                "Inspect one current-thread message, Assistant Note, assistant tool declaration, "
                "or durable tool result by full ID or unique case-sensitive prefix/suffix."
            ),
            complete=show_completions,
        )
    )


@dataclass(frozen=True)
class InspectionPlugin:
    name: str = "inspection"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_inspection_commands(context.command_registry)


__all__ = ["InspectionPlugin", "register_inspection_commands", "show_command", "show_completions"]
