from __future__ import annotations

"""Built-in interim user-facing assistant answer tool and /btw command."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolExecutionResult, ToolRegistry


TOOL_NAME = "answer_user_while_preserving_llm_turn"
GET_USER_MESSAGE_TOOL_NAME = "get_user_message_while_preserving_llm_turn"
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


def _context_tool_call_id(ctx: ToolContext) -> str:
    if ctx.stream is not None and getattr(ctx.stream, "tool_call_id", None):
        return str(ctx.stream.tool_call_id)
    raw_tool_call_id = ctx.raw.get("tool_call_id")
    if raw_tool_call_id:
        return str(raw_tool_call_id)
    return ""


def _message_create_event_seq(db: Any, thread_id: str, msg_id: str) -> int:
    row = db.conn.execute(
        """
        SELECT event_seq FROM events
         WHERE thread_id=? AND msg_id=? AND type='msg.create'
         ORDER BY event_seq ASC LIMIT 1
        """,
        (thread_id, msg_id),
    ).fetchone()
    return int(row[0]) if row else -1


def _next_normal_user_message_after(db: Any, thread_id: str, after_seq: int) -> Dict[str, Any] | None:
    rows = db.conn.execute(
        """
        SELECT event_seq, msg_id, payload_json FROM events
         WHERE thread_id=? AND type='msg.create' AND event_seq>?
         ORDER BY event_seq ASC
        """,
        (thread_id, after_seq),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else (row["payload_json"] or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        if payload.get("role") != "user":
            continue
        if payload.get("tool_calls") or payload.get("no_api") or payload.get("keep_user_turn"):
            continue
        return {
            "event_seq": int(row["event_seq"]),
            "msg_id": str(row["msg_id"]),
            "content": str(payload.get("content") or ""),
        }
    return None


async def get_user_message_while_preserving_llm_turn_tool(args: Dict[str, Any], ctx: ToolContext) -> str | ToolExecutionResult:
    """Append a visible assistant note, then return the next user message."""

    thread_id = ctx.thread_id or str(args.get("_thread_id") or "").strip()
    if not thread_id:
        return "Error: get_user_message_while_preserving_llm_turn requires a calling thread."

    assistant_note = args.get("assistant_note")
    if not isinstance(assistant_note, str) or not assistant_note.strip():
        return "Error: assistant_note is required."

    try:
        from .compaction import _context_db
        from ..api import append_message, create_snapshot, edit_message

        db = _context_db(ctx)
        tool_call_id = _context_tool_call_id(ctx)
        extra: Dict[str, Any] = {
            PRESERVE_TURN_FLAG: True,
            "source_tool_name": GET_USER_MESSAGE_TOOL_NAME,
        }
        if ctx.initial_model_key:
            extra["model_key"] = ctx.initial_model_key
        if tool_call_id:
            extra["tool_call_id"] = tool_call_id
            extra["awaiting_user_message_tool_call_id"] = tool_call_id

        note_msg_id = append_message(db, thread_id, "assistant", assistant_note, extra=extra)
        note_seq = _message_create_event_seq(db, thread_id, note_msg_id)
        create_snapshot(db, thread_id)
    except Exception as e:
        return f"Error: failed to append user-message prompt: {e}"

    while True:
        if ctx.cancel_check is not None:
            try:
                if ctx.cancel_check():
                    return ToolExecutionResult(
                        "--- INTERRUPTED ---\n"
                        "get_user_message_while_preserving_llm_turn stopped before receiving user input.",
                        reason="interrupted",
                    )
            except Exception:
                pass

        try:
            found = _next_normal_user_message_after(db, thread_id, note_seq)
            if found is not None:
                edit_message(
                    db,
                    thread_id,
                    found["msg_id"],
                    found["content"],
                    extra={
                        "no_api": True,
                        "keep_user_turn": True,
                        "consumed_by_tool_call_id": tool_call_id,
                        "consumed_by_tool_name": GET_USER_MESSAGE_TOOL_NAME,
                    },
                )
                create_snapshot(db, thread_id)
                return found["content"]
        except Exception as e:
            return f"Error: failed while waiting for user input: {e}"

        await asyncio.sleep(0.05)


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
    registry.register(
        GET_USER_MESSAGE_TOOL_NAME,
        (
            "Show a user-facing assistant note while preserving the current "
            "assistant/tool turn, wait for the next user message, and return "
            "that user message as this tool's result."
        ),
        {
            "type": "object",
            "properties": {
                "assistant_note": {
                    "type": "string",
                    "description": "The visible assistant note to show before waiting for the user's reply.",
                }
            },
            "required": ["assistant_note"],
            "additionalProperties": False,
        },
        get_user_message_while_preserving_llm_turn_tool,
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
    "GET_USER_MESSAGE_TOOL_NAME",
    "PRESERVE_TURN_FLAG",
    "TOOL_NAME",
    "answer_user_while_preserving_llm_turn_tool",
    "btw_command",
    "get_user_message_while_preserving_llm_turn_tool",
    "register_answer_user_commands",
    "register_answer_user_tool",
]
