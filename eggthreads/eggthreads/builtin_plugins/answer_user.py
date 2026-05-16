from __future__ import annotations

"""Built-in interim user-facing assistant answer tool and /btw command."""

from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolRegistry


TOOL_NAME = "answer_user_while_preserving_llm_turn"
PRESERVE_TURN_FLAG = "answer_user_preserve_turn"


def answer_user_while_preserving_llm_turn_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Append a user-visible assistant note without ending the current LLM/tool turn."""

    thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: answer_user_while_preserving_llm_turn requires a calling thread."

    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return "Error: message is required."

    try:
        from .compaction import _context_db
        from ..api import append_message

        db = _context_db(ctx)
        extra: Dict[str, Any] = {PRESERVE_TURN_FLAG: True}
        if ctx.initial_model_key:
            extra["model_key"] = ctx.initial_model_key
        if ctx.stream is not None and getattr(ctx.stream, "tool_call_id", None):
            extra["tool_call_id"] = str(ctx.stream.tool_call_id)
            extra["source_tool_name"] = TOOL_NAME
        append_message(db, thread_id, "assistant", message, extra=extra)
    except Exception as e:
        return f"Error: failed to append interim answer: {e}"

    return "Interim answer shown to user."


def btw_command(context: Any, arg: str):
    """Queue a user request asking the assistant to answer with the interim-answer tool."""

    from ..command_catalog import CommandResult

    db = getattr(context, "db", None)
    current_thread = getattr(context, "current_thread", None)
    if db is None or not current_thread:
        return CommandResult(clear_input=False, message="/btw requires an active thread")

    message = (arg or "").strip()
    if not message:
        return CommandResult(clear_input=False, message="Usage: /btw <message>")

    content = (
        "Please answer the following user message using the "
        f"{TOOL_NAME} tool, then continue your current work as appropriate.\n\n"
        f"User message:\n{message}"
    )
    try:
        append = getattr(context, "append_message", None)
        if callable(append):
            append(db, current_thread, "user", content)
        else:
            from ..api import append_message

            append_message(db, current_thread, "user", content)

        snapshot = getattr(context, "create_snapshot", None)
        if callable(snapshot):
            snapshot(db, current_thread)
        else:
            from ..api import create_snapshot

            create_snapshot(db, current_thread)
    except Exception as e:
        return CommandResult(clear_input=False, message=f"/btw failed: {e}")

    start_scheduler = getattr(context, "start_scheduler", None)
    if callable(start_scheduler):
        start_scheduler(current_thread)
    return CommandResult(
        clear_input=True,
        start_schedulers=(current_thread,),
        message="Queued /btw request for the assistant.",
    )


def register_answer_user_tool(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        (
            "Send a user-facing interim answer/status while preserving the current "
            "assistant/tool turn so you can keep working afterward. Use this when "
            "you should respond to the user now but not end the ongoing workflow."
        ),
        {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The user-facing interim answer or status update to display.",
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        answer_user_while_preserving_llm_turn_tool,
        accepts_context=True,
    )


def register_answer_user_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(
        CommandSpec(
            "btw",
            btw_command,
            category="input",
            usage="/btw <message>",
            description="Ask the assistant to answer via an interim note while preserving its turn.",
        )
    )


@dataclass(frozen=True)
class AnswerUserPlugin:
    name: str = "answer_user"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_answer_user_tool(context.tool_registry)
        if context.command_registry is not None:
            register_answer_user_commands(context.command_registry)


__all__ = [
    "AnswerUserPlugin",
    "PRESERVE_TURN_FLAG",
    "TOOL_NAME",
    "answer_user_while_preserving_llm_turn_tool",
    "btw_command",
    "register_answer_user_commands",
    "register_answer_user_tool",
]
