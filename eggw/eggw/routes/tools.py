"""Tool call API routes for eggw backend."""
from __future__ import annotations

import os
from typing import List

from fastapi import APIRouter, HTTPException

from eggthreads import (
    build_tool_call_states,
    approve_tool_calls_for_thread,
)

from models import ToolCallInfo, ApprovalRequest
import core
from core import ensure_scheduler_for, shorten_output_preview

router = APIRouter(prefix="/api/threads", tags=["tools"])


@router.get("/{thread_id}/tools", response_model=List[ToolCallInfo])
async def get_tool_calls(thread_id: str):
    """Get tool calls for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    states = build_tool_call_states(core.db, thread_id)
    tools = []
    for tc_id, tc in states.items():
        tools.append(ToolCallInfo(
            id=tc_id,
            name=tc.name,
            arguments=tc.arguments,
            state=tc.state,
            output=tc.finished_output,
            approval_decision=tc.approval_decision,
            output_decision=tc.output_decision,
        ))
    return tools


@router.post("/{thread_id}/tools/approve")
async def approve_tool(thread_id: str, request: ApprovalRequest):
    """Approve or deny a tool call."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Get current tool states
    states = build_tool_call_states(core.db, thread_id)
    tc = states.get(request.tool_call_id)

    if not tc:
        raise HTTPException(status_code=404, detail="Tool call not found")

    if tc.state == "TC1":
        # Execution approval
        # Check for special 'all-in-turn' decision first
        if request.decision == "all-in-turn":
            # Approve all tool calls in this turn
            approve_tool_calls_for_thread(
                core.db,
                thread_id,
                decision="all-in-turn",
                reason="Approved all by user from web UI",
            )
        else:
            # Normal approve/deny for single tool call
            decision = "granted" if request.approved else "denied"
            approve_tool_calls_for_thread(
                core.db,
                thread_id,
                decision=decision,
                tool_call_id=request.tool_call_id,
            )
    elif tc.state == "TC4":
        # Output approval - emit tool_call.output_approval event directly
        # (not tool_call.approval which is for execution approval)
        output_decision = request.output_decision or ("whole" if request.approved else "omit")

        # Get the full output from the tool call state
        full_output = tc.finished_output or ""
        if not isinstance(full_output, str):
            full_output = str(full_output)

        # Compute preview based on decision
        if output_decision == "whole":
            preview = full_output
        elif output_decision == "partial":
            preview = shorten_output_preview(full_output)
        else:  # omit
            preview = "Output omitted."

        # Compute stats for the payload
        line_count = len(full_output.splitlines()) if full_output else 0
        char_count = len(full_output)

        # Emit tool_call.output_approval event
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_='tool_call.output_approval',
            msg_id=None,
            invoke_id=None,
            payload={
                'tool_call_id': request.tool_call_id,
                'decision': output_decision,
                'reason': 'User decided in web UI',
                'preview': preview,
                'line_count': line_count,
                'char_count': char_count,
            },
        )
    else:
        raise HTTPException(status_code=400, detail=f"Tool call in state {tc.state} cannot be approved")

    # Ensure scheduler is running to process the approved tool
    ensure_scheduler_for(thread_id)

    return {"status": "ok"}
