from __future__ import annotations

"""Edit-answer draft API routes for eggw backend."""

from fastapi import APIRouter, HTTPException

from .. import core
from ..edit_answer import prepare_edit_answer_draft_response
from ..models import EditAnswerDraftRequest, EditAnswerDraftResponse


router = APIRouter(prefix="/api/threads", tags=["edit-answer"])


@router.post("/{thread_id}/edit-answer-draft", response_model=EditAnswerDraftResponse)
async def create_edit_answer_draft(thread_id: str, request: EditAnswerDraftRequest | None = None):
    """Prepare a quoted assistant-answer draft without sending it."""

    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    thread = core.db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    request = request or EditAnswerDraftRequest()
    try:
        return prepare_edit_answer_draft_response(
            core.db,
            thread_id,
            selector=request.selector,
            source_msg_id=request.source_msg_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
