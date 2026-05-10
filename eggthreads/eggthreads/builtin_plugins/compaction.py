from __future__ import annotations

"""Built-in thread compaction tool and command."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolRegistry


def _context_db(ctx: ToolContext):
    """Return a DB connection safe for the current tool execution thread."""

    try:
        from .execution import _thread_db

        return _thread_db(ctx.db)
    except Exception:
        from ..db import ThreadsDB

        db_path = getattr(ctx.db, "path", None)
        return ThreadsDB(db_path) if db_path is not None else ThreadsDB()


def compact_thread_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Commit a compaction boundary for the calling thread."""

    from ..api import commit_thread_compaction
    from ..tool_state import build_tool_call_states

    thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: compact_thread requires a calling thread."

    db = _context_db(ctx)
    selector = args.get("start_message")
    selector_text = str(selector).strip() if selector is not None else None
    tool_call_id = None
    if ctx.stream is not None and getattr(ctx.stream, "tool_call_id", None):
        tool_call_id = str(ctx.stream.tool_call_id)
    elif args.get("_tool_call_id"):
        tool_call_id = str(args.get("_tool_call_id"))

    committed_from_msg_id = str(args.get("_msg_id") or "") or None
    if not committed_from_msg_id and tool_call_id:
        try:
            state = build_tool_call_states(db, thread_id).get(tool_call_id)
            if state is not None and state.parent_msg_id:
                committed_from_msg_id = state.parent_msg_id
        except Exception:
            committed_from_msg_id = None

    result = commit_thread_compaction(
        db,
        thread_id,
        selector_text,
        created_by="assistant_tool",
        tool_call_id=tool_call_id,
        committed_from_msg_id=committed_from_msg_id,
    )
    return "Thread compacted." if result.success else result.message


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
    return CommandResult(clear_input=bool(result.success))


def compact_with_summary_command(context: Any, arg: str):
    from ..api import append_compaction_summary_request, create_snapshot
    from ..command_catalog import CommandResult

    db = getattr(context, "db", None)
    current_thread = getattr(context, "current_thread", None)
    if db is None or not current_thread:
        return CommandResult(clear_input=False, message="/compactWithSummary requires an active thread")

    try:
        request_msg_id = append_compaction_summary_request(
            db,
            current_thread,
            created_by="user_command",
        )
        create_snapshot(db, current_thread)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/compactWithSummary failed: {e}")

    message = (
        "Queued compaction summary request; the assistant will write a normal "
        "summary and then call compact_thread()."
    )
    log = getattr(context, "log_system", None)
    if callable(log):
        log(message)
        result_message = None
    else:
        result_message = message
    start_scheduler = getattr(context, "start_scheduler", None)
    if callable(start_scheduler):
        start_scheduler(current_thread)
    return CommandResult(clear_input=True, start_schedulers=(current_thread,), message=result_message)


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
    registry.register(
        CommandSpec(
            "compactWithSummary",
            compact_with_summary_command,
            category="threads",
            usage="/compactWithSummary",
            description="Ask the assistant to summarize, then call compact_thread without deleting UI history.",
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
    "compact_with_summary_command",
    "register_compaction_commands",
    "register_compaction_tool",
]
