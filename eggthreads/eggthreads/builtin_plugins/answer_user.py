from __future__ import annotations

"""Built-in interim user-facing assistant answer tool and /btw command."""

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolContext, ToolExecutionResult, ToolRegistry
from ..content_parts import content_to_plain_text


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


def _event_payload(row: Any) -> Dict[str, Any]:
    try:
        raw = row["payload_json"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _normal_user_message(payload: Dict[str, Any]) -> bool:
    return bool(
        payload.get("role") == "user"
        and not payload.get("tool_calls")
        and not payload.get("no_api")
        and not payload.get("keep_user_turn")
    )


def _claim_next_normal_user_message(
    db: Any,
    thread_id: str,
    *,
    note_seq: int,
    tool_call_id: str,
) -> Dict[str, Any] | None:
    """Atomically claim the next eligible reply for exactly one waiting call.

    A reply belongs to the newest get-user note that existed when the reply was
    appended. Older concurrent waits are terminalized by the runner after this
    exact-ID claim. The SQLite write reservation makes competing processes
    observe one consumed-by winner rather than both returning the same message.
    """

    if not tool_call_id:
        return None
    savepoint = f"claim_get_user_reply_{os.urandom(8).hex()}"
    db.conn.execute(f"SAVEPOINT {savepoint}")
    try:
        # Acquire SQLite's single-writer reservation before selecting a reply.
        locked = db.conn.execute(
            "UPDATE threads SET status=status WHERE thread_id=?",
            (thread_id,),
        )
        if locked.rowcount != 1:
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return None

        from ..api import _get_user_wait_notes
        from ..tool_state import build_tool_call_states

        states = build_tool_call_states(db, thread_id)
        if tool_call_id in states:
            newest_unresolved_id = next(
                (
                    str(note["tool_call_id"])
                    for note in reversed(_get_user_wait_notes(db, thread_id))
                    if (
                        (state := states.get(str(note["tool_call_id"]))) is not None
                        and state.state == "TC3"
                    )
                ),
                "",
            )
            if newest_unresolved_id != tool_call_id:
                db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return None

        rows = db.conn.execute(
            """
            SELECT event_seq, msg_id, payload_json FROM events
             WHERE thread_id=? AND type='msg.create' AND event_seq>?
             ORDER BY event_seq ASC
            """,
            (thread_id, note_seq),
        ).fetchall()
        for row in rows:
            payload = _event_payload(row)
            if not _normal_user_message(payload):
                continue
            reply_seq = int(row["event_seq"])
            msg_id = str(row["msg_id"] or "")
            if not msg_id:
                continue

            newer_note = db.conn.execute(
                """
                SELECT 1 FROM events
                 WHERE thread_id=? AND type='msg.create'
                   AND event_seq>? AND event_seq<?
                   AND json_extract(payload_json, '$.answer_user_preserve_turn')=1
                   AND json_extract(payload_json, '$.source_tool_name')=?
                   AND COALESCE(json_extract(payload_json, '$.awaiting_user_message_tool_call_id'), '')<>''
                 LIMIT 1
                """,
                (thread_id, note_seq, reply_seq, GET_USER_MESSAGE_TOOL_NAME),
            ).fetchone()
            if newer_note is not None:
                db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return None

            already_claimed = db.conn.execute(
                """
                SELECT 1 FROM events
                 WHERE thread_id=? AND type='msg.edit' AND msg_id=?
                   AND json_extract(payload_json, '$.consumed_by_tool_name')=?
                   AND COALESCE(json_extract(payload_json, '$.consumed_by_tool_call_id'), '')<>''
                 LIMIT 1
                """,
                (thread_id, msg_id, GET_USER_MESSAGE_TOOL_NAME),
            ).fetchone()
            if already_claimed is not None:
                db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return None

            content = content_to_plain_text(payload.get("content"))
            db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=thread_id,
                type_="msg.edit",
                msg_id=msg_id,
                payload={
                    "content": payload.get("content", ""),
                    "no_api": True,
                    "keep_user_turn": True,
                    "consumed_by_tool_call_id": tool_call_id,
                    "consumed_by_tool_name": GET_USER_MESSAGE_TOOL_NAME,
                },
            )
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return {"event_seq": reply_seq, "msg_id": msg_id, "content": content}

        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return None
    except sqlite3.OperationalError as exc:
        db.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return None
        raise
    except Exception:
        db.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


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
        from ..api import append_message, create_snapshot

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
        from ..api import terminalize_superseded_get_user_waits

        terminalize_superseded_get_user_waits(
            db,
            thread_id,
            authoritative_tool_call_id=tool_call_id,
        )
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
            found = _claim_next_normal_user_message(
                db,
                thread_id,
                note_seq=note_seq,
                tool_call_id=tool_call_id,
            )
            if found is not None:
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
