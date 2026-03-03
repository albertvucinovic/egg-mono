"""Message API routes for eggw backend."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import List

from fastapi import APIRouter, HTTPException

from eggthreads import (
    SnapshotBuilder,
    ThreadsDB,
    append_message,
    build_tool_call_states,
    interrupt_thread,
)

from ..models import MessageContent, SendMessageRequest
from .. import core
from ..core import ensure_scheduler_for, get_thread_root_id

router = APIRouter(prefix="/api/threads", tags=["messages"])


def _get_messages_sync(db_path: str, thread_id: str) -> List[MessageContent]:
    """Synchronous helper to fetch messages - runs in thread pool to avoid blocking event loop."""
    # Use fresh connection to ensure we see latest writes from other processes
    fresh_db = ThreadsDB(db_path)
    t = fresh_db.get_thread(thread_id)
    if not t:
        return None  # Signal thread not found

    # Build fresh snapshot from ALL events (not cached snapshot_json)
    cur = fresh_db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (thread_id,)
    )
    events = cur.fetchall()

    builder = SnapshotBuilder()
    snap = builder.build(events)

    # Get per-message token stats from cached snapshot (if available)
    token_stats = {}
    per_message_tokens = {}
    if t.snapshot_json:
        try:
            cached_snap = json.loads(t.snapshot_json)
            token_stats = cached_snap.get("token_stats", {})
            per_message_tokens = token_stats.get("per_message", {}) if isinstance(token_stats, dict) else {}
        except:
            pass

    messages = []
    for msg in snap.get("messages", []):
        msg_id = msg.get("msg_id", "")

        # Get per-message token count from cached stats
        pm_info = per_message_tokens.get(msg_id, {}) if msg_id else {}
        total_tokens = None
        if pm_info:
            content_tok = int(pm_info.get("content_tokens", 0) or 0)
            reasoning_tok = int(pm_info.get("reasoning_tokens", 0) or 0)
            tool_calls_tok = int(pm_info.get("tool_calls_tokens", 0) or 0)
            total_tokens = pm_info.get("total_tokens") or (content_tok + reasoning_tok + tool_calls_tok)
            if total_tokens:
                total_tokens = int(total_tokens)

        # Parse timestamp
        ts_raw = msg.get("ts")
        timestamp = None
        if ts_raw:
            try:
                timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                try:
                    timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
                except Exception:
                    pass

        messages.append(MessageContent(
            id=msg_id,
            role=msg.get("role", ""),
            content=msg.get("content"),
            reasoning=msg.get("reasoning"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
            model_key=msg.get("model_key"),
            timestamp=timestamp,
            tokens=total_tokens,
        ))

    return messages


@router.get("/{thread_id}/messages", response_model=List[MessageContent])
async def get_messages(thread_id: str):
    """Get messages for a thread by building fresh snapshot from events.

    This ensures we see all messages including those written by other processes
    (e.g., TUI) that haven't been persisted to snapshot_json yet.

    Runs database operations in thread pool to avoid blocking the async event loop,
    allowing multiple tabs to fetch messages simultaneously.
    """
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Run database-heavy work in thread pool to avoid blocking event loop
    loop = asyncio.get_event_loop()
    messages = await loop.run_in_executor(None, _get_messages_sync, core.db.path, thread_id)

    if messages is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    return messages


@router.post("/{thread_id}/messages")
async def send_message(thread_id: str, request: SendMessageRequest):
    """Send a message to a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Append user message
    msg_id = append_message(core.db, thread_id, role="user", content=request.content)

    # Ensure scheduler is running for this thread's root
    ensure_scheduler_for(thread_id)

    return {"status": "sent", "message_id": msg_id}


@router.post("/{thread_id}/open")
async def open_thread(thread_id: str):
    """Open a thread for viewing. Ensures scheduler for this thread's root is running."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Ensure scheduler is running for this thread's root (don't stop other schedulers)
    # This allows multiple tabs to view different thread trees simultaneously
    from ..core import start_scheduler
    root_id = get_thread_root_id(thread_id)
    scheduler_running = root_id in core.active_schedulers
    if not scheduler_running:
        start_scheduler(root_id)

    return {
        "status": "ok",
        "thread_id": thread_id,
        "root_id": root_id,
        "scheduler_running": True,
    }


@router.post("/{thread_id}/interrupt")
async def interrupt_thread_endpoint(thread_id: str):
    """Interrupt/cancel current streaming or pending work (Ctrl+C equivalent)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Interrupt the thread
    result = interrupt_thread(core.db, thread_id, reason="user")

    # Auto-approve output for any interrupted tool calls so they get published
    # and don't block further interaction. The runner will add an "interrupted" note.
    # We need a brief delay to let the interrupt propagate and tool calls reach TC4.
    await asyncio.sleep(0.1)

    states = build_tool_call_states(core.db, thread_id)
    for tc in states.values():
        if tc.state == "TC4" and tc.finished_reason == "interrupted":
            # Emit output approval with 'whole' decision - runner handles interrupted specially
            full_output = tc.finished_output or ""
            if not isinstance(full_output, str):
                full_output = str(full_output)
            line_count = len(full_output.splitlines()) if full_output else 0
            char_count = len(full_output)

            core.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=thread_id,
                type_='tool_call.output_approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'tool_call_id': tc.tool_call_id,
                    'decision': 'whole',
                    'reason': 'Auto-approved after interrupt',
                    'preview': full_output,
                    'line_count': line_count,
                    'char_count': char_count,
                },
            )

    return {
        "status": "interrupted",
        "thread_id": thread_id,
        "invoke_id": result,
    }
