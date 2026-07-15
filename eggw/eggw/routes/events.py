"""Event streaming (SSE and WebSocket) routes for eggw backend."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse

from eggthreads import (
    ThreadsDB,
    ThreadEventCursorError,
    ThreadEventFeed,
    ThreadEventFeedNotFound,
    append_normal_user_message,
    build_tool_call_states,
    approve_tool_calls_for_thread,
    finalize_tool_output,
    resolve_event_cursor,
)

from .. import core
from ..core import get_thread_root_id
from ..core.scheduler import scheduler_running

router = APIRouter(tags=["events"])

EVENT_FEED_BATCH_SIZE = 256
EVENT_FEED_POLL_SEC = 0.015
EVENT_FEED_MAX_BACKOFF = 0.03


def _sse_frame(event) -> dict[str, str]:
    """Return one id-addressable SSE frame from a canonical envelope."""

    return {
        "id": str(event.event_seq),
        "event": event.type,
        "data": json.dumps(event.as_dict(), separators=(",", ":")),
    }


@router.get("/api/threads/{thread_id}/events")
async def stream_events(
    thread_id: str,
    after_seq: str | None = Query(
        default=None,
        description=(
            "Replay events strictly after this cursor. Explicit after_seq takes "
            "precedence over Last-Event-ID."
        ),
    ),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Stream canonical events after a durable cursor.

    Cursor precedence is ``after_seq`` > ``Last-Event-ID`` > connection-time
    default. Initial callers resolve the explicit replay cursor through
    ``/state?snapshot_cursor=...`` and pass it as ``after_seq``. Without an
    explicit cursor, a live unexpired lease replays its exact invocation from
    ``stream.open``; otherwise the feed starts at the current cursor. Every
    event has ``id: event_seq`` so reconnect advances only through consumed
    frames and resumes without duplicates.
    """

    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # FastAPI replaces these defaults for HTTP requests; direct compatibility
    # callers may invoke the route function without dependency injection.
    if not isinstance(after_seq, (str, int)):
        after_seq = None
    if not isinstance(last_event_id, str):
        last_event_id = None

    # Resolve existence/cursor on a fresh connection so another Egg process's
    # just-committed thread/events are immediately visible without coupling the
    # long-lived stream to EggW's scheduler connection.
    cursor_db = ThreadsDB(core.db.path)
    feed = ThreadEventFeed(cursor_db, batch_size=EVENT_FEED_BATCH_SIZE)
    try:
        if not feed.thread_exists(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        if after_seq is not None or (last_event_id is not None and last_event_id.strip()):
            initial_cursor = resolve_event_cursor(
                after_seq=after_seq,
                last_event_id=last_event_id,
            )
        else:
            idle_cursor = feed.current_cursor(thread_id)
            initial_cursor = feed.replay_cursor(thread_id, idle_cursor).after_seq
    except ThreadEventCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ThreadEventFeedNotFound:
        raise HTTPException(status_code=404, detail="Thread not found") from None
    finally:
        cursor_db.conn.close()

    async def event_generator():
        # A dedicated connection preserves cross-process visibility without
        # contending with the scheduler's global connection.
        sse_db = ThreadsDB(core.db.path)
        sse_feed = ThreadEventFeed(sse_db, batch_size=EVENT_FEED_BATCH_SIZE)
        cursor = initial_cursor
        idle = 0
        last_batch_time = time.monotonic()
        try:
            while True:
                try:
                    batch = sse_feed.read_after(thread_id, cursor)
                except ThreadEventFeedNotFound:
                    return
                if batch.events:
                    now = time.monotonic()
                    elapsed_ms = (now - last_batch_time) * 1000
                    if len(batch.events) > 5:
                        types = [event.type for event in batch.events]
                        eggw_scheduler = bool(
                            thread_id and scheduler_running(get_thread_root_id(thread_id))
                        )
                        print(
                            f"[SSE] Large batch: {len(batch.events)} events "
                            f"(seq {batch.events[0].event_seq}-{batch.events[-1].event_seq}), "
                            f"{elapsed_ms:.0f}ms since last, eggw_sched={eggw_scheduler}, "
                            f"types={types[:5]}{'...' if len(types) > 5 else ''}"
                        )
                    last_batch_time = now
                    idle = 0
                    for event in batch.events:
                        # Advance only for events actually yielded. Cancellation
                        # before the next frame leaves that cursor resumable.
                        yield _sse_frame(event)
                        cursor = event.event_seq
                    continue

                idle = min(idle + 1, 4)
                delay = (
                    EVENT_FEED_POLL_SEC
                    if idle < 2
                    else min(EVENT_FEED_POLL_SEC * (idle + 1), EVENT_FEED_MAX_BACKOFF)
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            sse_db.conn.close()

    return EventSourceResponse(event_generator(), ping=1)


class ConnectionManager:
    """Manage WebSocket connections."""

    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, thread_id: str):
        # Echo only the fixed application protocol, never the credential-bearing
        # protocol used by browser WebSocket authentication.
        selected_protocol = "eggw" if "eggw" in websocket.scope.get("subprotocols", []) else None
        await websocket.accept(subprotocol=selected_protocol)
        if thread_id not in self.active_connections:
            self.active_connections[thread_id] = []
        self.active_connections[thread_id].append(websocket)

    def disconnect(self, websocket: WebSocket, thread_id: str):
        if thread_id in self.active_connections:
            self.active_connections[thread_id].remove(websocket)
            if not self.active_connections[thread_id]:
                del self.active_connections[thread_id]

    async def broadcast(self, thread_id: str, message: dict):
        if thread_id in self.active_connections:
            for connection in self.active_connections[thread_id]:
                await connection.send_json(message)


manager = ConnectionManager()


@router.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """WebSocket endpoint for real-time communication."""
    await manager.connect(websocket, thread_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "send_message":
                content = data.get("content", "")
                if content and core.db:
                    append_normal_user_message(core.db, thread_id, content)
                    await manager.broadcast(thread_id, {
                        "type": "message_sent",
                        "thread_id": thread_id,
                    })

            elif msg_type == "approve_tool":
                tc_id = data.get("tool_call_id")
                approved = data.get("approved", False)
                if tc_id and core.db:
                    states = build_tool_call_states(core.db, thread_id)
                    tc = states.get(tc_id)
                    if tc and tc.state in ("TC1", "TC4"):
                        decision = "granted" if approved else "denied"
                        if tc.state == "TC4":
                            decision = data.get("output_decision", "whole" if approved else "omit")
                            finalize_tool_output(
                                core.db,
                                thread_id,
                                tc_id,
                                decision=decision,
                                source="user_omit" if decision == "omit" else "user",
                                reason="User decided over web socket",
                                expected_event_seq=tc.state_event_seq,
                            )
                        else:
                            approve_tool_calls_for_thread(
                                core.db,
                                thread_id,
                                decision=decision,
                                tool_call_id=tc_id,
                            )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket, thread_id)
