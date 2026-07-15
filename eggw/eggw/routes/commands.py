"""Command API routes for eggw backend."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..models import CommandLifecycleResponse, CommandRequest
from .. import core
from ..commands import dispatch_command
from ..commands.thread import validate_continue_command_target

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

    # Explicit /continue targets are the exceptional fail-closed preflight:
    # validate before even generic command-audit events so a rejected target
    # leaves the thread event watermark, history, lease, and lifecycle intact.
    # The handler validates again for direct callers and mutation-boundary
    # safety. No-argument /continue passes through to normal auto-diagnosis.
    if command_name == "continue":
        parts = str(request.command or "").strip()[1:].split(None, 1)
        command_arg = parts[1] if len(parts) > 1 else ""
        target_validation = validate_continue_command_target(thread_id, command_arg)
        if not target_validation.success:
            finished_at = datetime.now(timezone.utc)
            return CommandLifecycleResponse(
                success=False,
                message=target_validation.message,
                data=None,
                command_id=command_id,
                command_name=command_name,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_sec=max(0.0, (finished_at - started_at).total_seconds()),
            )

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
    except Exception:
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
    try:
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="user_command.finished",
            payload={
                "command_id": command_id,
                "command_name": command_name,
                "success": bool(response.success),
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
