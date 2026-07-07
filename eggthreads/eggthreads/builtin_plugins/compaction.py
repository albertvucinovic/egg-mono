from __future__ import annotations

"""Built-in thread compaction tool and command."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolRegistry
from ..token_count import thread_token_stats


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
        "Compaction committed and summary request queued; the assistant will "
        "write a continuation summary before other work."
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


def set_auto_compact_threshold_command(context: Any, arg: str):
    from ..api import set_thread_compaction_context_length
    from ..command_catalog import CommandResult

    db = getattr(context, "db", None)
    current_thread = getattr(context, "current_thread", None)
    if db is None or not current_thread:
        return CommandResult(clear_input=False, message="/setAutoCompactThreshold requires an active thread")

    text = (arg or "").strip()
    if not text:
        return CommandResult(clear_input=False, message="Usage: /setAutoCompactThreshold <tokens> (0 disables auto-compaction)")
    try:
        threshold = int(text)
    except ValueError:
        return CommandResult(clear_input=False, message=f"Invalid number: {text}. Usage: /setAutoCompactThreshold <tokens>")

    event_seq = set_thread_compaction_context_length(
        db,
        current_thread,
        threshold,
        created_by="user_command",
    )
    if threshold <= 0:
        message = f"Auto-compaction disabled for thread {current_thread[-8:]} (event #{event_seq})."
    else:
        message = f"Auto-compaction threshold set to {threshold:,} tokens for thread {current_thread[-8:]} (event #{event_seq})."
    log = getattr(context, "log_system", None)
    if callable(log):
        log(message)
    printer = getattr(context, "console_print_block", None)
    if callable(printer):
        printer("Auto-compaction", message, border_style="cyan")
    return CommandResult(clear_input=True, message=message)


def build_context_status(db: Any, thread_id: str, *, llm: Any = None) -> tuple[str, Dict[str, Any]]:
    from ..api import get_context_limit, resolve_auto_compact_threshold, thread_compaction_status

    def fmt_tok(value: Any) -> str:
        try:
            n = int(value)
        except Exception:
            return "n/a"
        return f"{n:,}"

    stats = thread_token_stats(db, thread_id, llm=llm)
    context_tokens = int(stats.get("context_tokens") or 0)
    full_thread_tokens = int(stats.get("full_thread_tokens") or context_tokens)
    compacted_away_tokens = max(0, full_thread_tokens - context_tokens)
    compaction = thread_compaction_status(db, thread_id)
    context_limit = get_context_limit(db, thread_id)
    auto_threshold = resolve_auto_compact_threshold(db, thread_id)

    compaction_active = bool(compaction.get("compacted"))
    if compaction_active:
        provider_calculation = "provider/API prompt after compaction"
        full_calculation = "full effective thread before compaction filtering"
    else:
        provider_calculation = "full provider/API prompt (no compaction active)"
        full_calculation = "same as current provider context; no compaction filtering"

    lines = [
        f"Thread {thread_id[-8:]} context:",
        "  current_provider_context:",
        f"    context_tokens:       {fmt_tok(context_tokens)}",
        f"    calculation:          {provider_calculation}",
        "  full_thread_context:",
        f"    context_tokens:       {fmt_tok(full_thread_tokens)}",
        f"    calculation:          {full_calculation}",
    ]
    if compacted_away_tokens:
        lines.append(f"  compacted_away_tokens: {fmt_tok(compacted_away_tokens)}")
    if context_limit:
        pct = (context_tokens / context_limit * 100) if context_limit > 0 else 0
        lines.append(f"  context_limit:         {fmt_tok(context_limit)} ({pct:.1f}% provider context used)")
    else:
        lines.append("  context_limit:         unlimited")

    lines.append("")
    if auto_threshold.enabled and auto_threshold.threshold_tokens is not None:
        pct = (context_tokens / auto_threshold.threshold_tokens * 100) if auto_threshold.threshold_tokens > 0 else 0
        lines.append(f"  auto_compact_threshold: {fmt_tok(auto_threshold.threshold_tokens)} ({pct:.1f}% used, source: {auto_threshold.source})")
    else:
        lines.append(f"  auto_compact_threshold: disabled (source: {auto_threshold.source})")

    if compaction_active:
        lines.extend([
            "  compaction:             active",
            f"  prompt_start_msg_id:    {compaction.get('current_prompt_start_msg_id') or 'unknown'}",
            f"  prompt_start_event_seq: {compaction.get('current_prompt_start_event_seq') or 'unknown'}",
            f"  compaction_event_seq:   {compaction.get('marker_event_seq') or 'unknown'}",
        ])
    else:
        lines.append("  compaction:             inactive")
    lines.append(f"  raw_compaction_markers: {compaction.get('raw_marker_count', 0)}")

    return "\n".join(lines), {
        "context_tokens": context_tokens,
        "current_provider_context_tokens": context_tokens,
        "full_thread_tokens": full_thread_tokens,
        "full_thread_context_tokens": full_thread_tokens,
        "compacted_away_tokens": compacted_away_tokens,
        "context_limit": context_limit,
        "auto_compact_enabled": auto_threshold.enabled,
        "auto_compact_threshold": auto_threshold.threshold_tokens,
        "auto_compact_source": auto_threshold.source,
        "compaction": compaction,
    }


def context_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    db = getattr(context, "db", None)
    current_thread = getattr(context, "current_thread", None)
    if db is None or not current_thread:
        return CommandResult(clear_input=False, message="/context requires an active thread")

    text, _data = build_context_status(db, current_thread, llm=getattr(context, "llm_client", None))
    printer = getattr(context, "console_print_block", None)
    if callable(printer):
        printer("Context", text, border_style="cyan")
        return CommandResult(clear_input=True, message="Context status (see console).")
    return CommandResult(clear_input=True, message=text)


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
            "context",
            context_command,
            category="threads",
            usage="/context",
            description="Show context, compaction, context limit, and auto-compaction status.",
        )
    )
    registry.register(
        CommandSpec(
            "compactWithSummary",
            compact_with_summary_command,
            category="threads",
            usage="/compactWithSummary",
            description="Commit compaction, then ask the assistant for a continuation summary without deleting UI history.",
        )
    )
    registry.register(
        CommandSpec(
            "setAutoCompactThreshold",
            set_auto_compact_threshold_command,
            category="threads",
            usage="/setAutoCompactThreshold <tokens>",
            description="Set the thread auto-compaction token threshold; 0 disables auto-compaction.",
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
    "build_context_status",
    "context_command",
    "register_compaction_commands",
    "register_compaction_tool",
    "set_auto_compact_threshold_command",
]
