from __future__ import annotations

"""Built-in thread compaction tool and command."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolRegistry


def compact_thread_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Commit a compaction boundary for the calling thread."""

    from ..api import commit_thread_compaction
    from ..db import ThreadsDB

    thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: compact_thread requires a calling thread."

    db = ctx.db if ctx.db is not None else ThreadsDB()
    selector = args.get("start_message")
    selector_text = str(selector).strip() if selector is not None else None
    result = commit_thread_compaction(
        db,
        thread_id,
        selector_text,
        created_by="assistant_tool",
        committed_from_msg_id=str(args.get("_msg_id") or "") or None,
    )
    return result.message


def show_compaction_start_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Report the effective compaction start without changing thread state."""

    import json

    from ..api import show_compaction_start
    from ..db import ThreadsDB

    thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: show_compaction_start requires a calling thread."

    db = ctx.db if ctx.db is not None else ThreadsDB()
    return json.dumps(show_compaction_start(db, thread_id), ensure_ascii=False, indent=2)


def compact_thread_command(context: Any, arg: str):
    from ..api import commit_thread_compaction
    from ..command_catalog import CommandResult

    db = getattr(context, "db", None)
    current_thread = getattr(context, "current_thread", None)
    if db is None or not current_thread:
        return CommandResult(clear_input=False, message="/compact requires an active thread")

    selector = (arg or "").strip() or None
    result = commit_thread_compaction(
        db,
        current_thread,
        selector,
        created_by="user_command",
    )

    log = getattr(context, "log_system", None)
    if callable(log):
        log(result.message if result.success else f"/compact: {result.message}")
    return CommandResult(clear_input=bool(result.success), message=result.message)


def register_compaction_tool(registry: ToolRegistry) -> None:
    registry.register(
        "compact_thread",
        (
            "Set where future provider/API context for this thread starts; this does not delete "
            "or hide earlier UI/raw history. Use only when the user asks, an automatic compaction "
            "request asks, or context pressure makes a faithful new start appropriate; do not compact "
            "in the middle of substantive work merely because this tool is available. If writing a "
            "summary, write it first as normal assistant content, then call compact_thread with "
            "start_message omitted. Use 'last_user' when the goal is to keep the latest user turn "
            "and following continuation as the new start. start_message may be an explicit message "
            "id, 'last_user', or 'last_llm'; if omitted, the latest provider-visible user or "
            "assistant message is used."
        ),
        {
            "type": "object",
            "properties": {
                "start_message": {
                    "type": "string",
                    "description": "Optional start selector: explicit message id, last_user, or last_llm. Omit for latest user/assistant message.",
                }
            },
            "additionalProperties": False,
        },
        compact_thread_tool,
        accepts_context=True,
    )
    registry.register(
        "show_compaction_start",
        (
            "Read-only status for this thread's current effective compaction start. "
            "Shows the compaction marker, start message id/event seq, and a bounded "
            "preview of the start message. It does not fetch old pre-compaction history."
        ),
        {"type": "object", "properties": {}, "additionalProperties": False},
        show_compaction_start_tool,
        accepts_context=True,
    )


def register_compaction_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(
        CommandSpec(
            "compact",
            compact_thread_command,
            category="threads",
            usage="/compact [msg_id|last_user|last_llm]",
            description="Set the provider/API context start for this thread without deleting UI history.",
        )
    )


@dataclass(frozen=True)
class CompactionPlugin:
    name: str = "compaction"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_compaction_tool(context.tool_registry)
        if context.command_registry is not None:
            register_compaction_commands(context.command_registry)


__all__ = [
    "CompactionPlugin",
    "compact_thread_command",
    "compact_thread_tool",
    "register_compaction_commands",
    "register_compaction_tool",
    "show_compaction_start_tool",
]
