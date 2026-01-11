"""Command API routes for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from models import CommandRequest, CommandResponse
import core
from commands import dispatch_command

router = APIRouter(prefix="/api/threads", tags=["commands"])


@router.post("/{thread_id}/command", response_model=CommandResponse)
async def execute_command(thread_id: str, request: CommandRequest):
    """Execute a slash command or shell command."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    return await dispatch_command(thread_id, request.command)
