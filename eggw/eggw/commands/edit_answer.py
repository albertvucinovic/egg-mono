from __future__ import annotations

"""EggW /editAnswer command handler."""

from .. import core
from ..edit_answer import (
    edit_answer_draft_response_data,
    prepare_edit_answer_draft_response,
    prepare_editor_response,
)
from ..models import CommandResponse


async def cmd_edit_answer(thread_id: str, selector: str) -> CommandResponse:
    """Prepare a quoted assistant answer draft for the browser modal."""

    if not core.db:
        return CommandResponse(success=False, message="/editAnswer failed: database not initialized")
    if not core.db.get_thread(thread_id):
        return CommandResponse(success=False, message="/editAnswer failed: thread not found")

    try:
        response = prepare_edit_answer_draft_response(
            core.db,
            thread_id,
            selector=selector,
            fallback_to_empty_input=True,
            fallback_unmatched_selector_to_input=True,
        )
    except ValueError as e:
        return CommandResponse(success=False, message=f"/editAnswer failed: {e}")

    return CommandResponse(
        success=True,
        message=response.message or "Prepared quoted assistant answer.",
        data=edit_answer_draft_response_data(response),
    )


async def cmd_editor(thread_id: str, arg: str) -> CommandResponse:
    """Prepare a browser draft or host-file editor response."""

    if not core.db:
        return CommandResponse(success=False, message="/editor failed: database not initialized")
    if not core.db.get_thread(thread_id):
        return CommandResponse(success=False, message="/editor failed: thread not found")

    try:
        response = prepare_editor_response(core.db, thread_id, arg)
    except (OSError, ValueError) as e:
        return CommandResponse(success=False, message=f"/editor failed: {e}")
    if response.action == "request_completion":
        return CommandResponse(
            success=True,
            message=response.message,
            data={
                "action": "request_completion",
                "input": f"/editor {(arg or '').strip()}",
                "suppress_transcript": True,
            },
        )
    return CommandResponse(
        success=True,
        message=response.message or "Prepared empty input message draft.",
        data=edit_answer_draft_response_data(response),
    )


__all__ = ["cmd_edit_answer", "cmd_editor"]
