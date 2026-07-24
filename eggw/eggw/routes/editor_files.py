from __future__ import annotations

"""Authenticated, capability-bound save route for EggW's editor modal."""

from fastapi import APIRouter, HTTPException

from .. import core
from ..editor_files import EditorFileConflict, discard_registered_editor_file, save_registered_editor_file
from ..models import EditorFileSaveRequest, EditorFileSaveResponse

router = APIRouter(prefix="/api/threads", tags=["editor-files"])


@router.delete("/{thread_id}/editor-file/{handle}")
async def discard_human_editor_file(thread_id: str, handle: str):
    """Discard one thread-bound browser-editor save capability."""

    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    if not core.db.get_thread(thread_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        discarded = discard_registered_editor_file(thread_id, handle)
    except EditorFileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"discarded": discarded}


@router.put("/{thread_id}/editor-file", response_model=EditorFileSaveResponse)
async def save_human_editor_file(thread_id: str, request: EditorFileSaveRequest):
    """Atomically save through one opaque, thread-bound file capability."""

    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")
    if not core.db.get_thread(thread_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        path = save_registered_editor_file(
            thread_id,
            request.handle,
            request.content,
        )
    except (EditorFileConflict, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return EditorFileSaveResponse(path=str(path))
