"""Command API routes for eggw backend."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..models import CommandLifecycleResponse, CommandRequest
from .. import core
from ..commands import dispatch_command

router = APIRouter(prefix="/api/threads", tags=["commands"])


def _command_name(command: str) -> str:
    text = str(command or "").strip()
    if text.startswith("$$"):
        return "$$"
    if text.startswith("$"):
        return "$"
    if text.startswith("/"):
        return text[1:].split(None, 1)[0] or "/"
    return "command"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@router.post("/{thread_id}/command", response_model=CommandLifecycleResponse)
async def execute_command(thread_id: str, request: CommandRequest):
    """Execute a slash command or shell command."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    command_name = _command_name(request.command)
    command_id = os.urandom(10).hex()
    started_at = datetime.now(timezone.utc)
    try:
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="user_command.started",
            payload={
                "command_id": command_id,
                "command_name": command_name,
                "command": request.command,
                "started_at": _iso(started_at),
            },
        )
    except Exception:
        pass

    try:
        response = await dispatch_command(thread_id, request.command, staged_attachments=request.staged_attachments)
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        try:
            core.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=thread_id,
                type_="user_command.finished",
                payload={
                    "command_id": command_id,
                    "command_name": command_name,
                    "success": False,
                    "message": f"Command failed: {exc}",
                    "data": None,
                    "suppress_transcript": False,
                    "started_at": _iso(started_at),
                    "finished_at": _iso(finished_at),
                    "elapsed_sec": max(0.0, (finished_at - started_at).total_seconds()),
                },
            )
        except Exception:
            pass
        raise

    finished_at = datetime.now(timezone.utc)
    elapsed_sec = max(0.0, (finished_at - started_at).total_seconds())
    response_data = response.data if isinstance(response.data, dict) else None
    suppress_transcript = bool(response_data and response_data.get("suppress_transcript"))
    try:
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="user_command.finished",
            payload={
                "command_id": command_id,
                "command_name": command_name,
                "success": bool(response.success),
                "message": response.message,
                "data": response.data,
                "suppress_transcript": suppress_transcript,
                "started_at": _iso(started_at),
                "finished_at": _iso(finished_at),
                "elapsed_sec": elapsed_sec,
            },
        )
    except Exception:
        pass

    return CommandLifecycleResponse(
        success=response.success,
        message=response.message,
        data=response.data,
        command_id=command_id,
        command_name=command_name,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_sec=elapsed_sec,
    )
