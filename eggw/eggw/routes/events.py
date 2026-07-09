"""Event streaming (SSE and WebSocket) routes for eggw backend."""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse

from eggthreads import (
    ThreadsDB,
    EventWatcher,
    append_message,
    build_tool_call_states,
    approve_tool_calls_for_thread,
)

from .. import core
from ..core import get_thread_root_id
from ..core.scheduler import scheduler_running

router = APIRouter(tags=["events"])


def _active_stream_replay_after_seq(db: ThreadsDB, thread_id: str, stream_open_seq: int) -> int:
    """Return the event_seq to replay after when attaching mid-stream.

    Replaying only from ``stream.open`` is enough for the live deltas, but it
    can miss user messages written by another client immediately before the
    provider stream began.  Include those pending user turns when they are after
    the previous completed stream boundary so EggW stays synchronized with
    terminal Egg while the answer is already streaming.
    """

    try:
        previous_close_row = db.conn.execute(
            """
            SELECT MAX(event_seq) AS event_seq FROM events
            WHERE thread_id = ? AND type = 'stream.close' AND event_seq < ?
            """,
            (thread_id, stream_open_seq),
        ).fetchone()
        previous_close_seq = int(previous_close_row["event_seq"]) if previous_close_row and previous_close_row["event_seq"] is not None else -1
    except Exception:
        previous_close_seq = -1

    try:
        rows = db.conn.execute(
            """
            SELECT event_seq, payload_json FROM events
            WHERE thread_id = ?
              AND type = 'msg.create'
              AND event_seq > ?
              AND event_seq < ?
            ORDER BY event_seq ASC
            """,
            (thread_id, previous_close_seq, stream_open_seq),
        ).fetchall()
    except Exception:
        rows = []

    first_user_seq = None
    for row in rows:
        try:
            payload_json = row["payload_json"]
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("role") == "user":
            try:
                first_user_seq = int(row["event_seq"])
            except Exception:
                pass
            break

    if first_user_seq is not None:
        return first_user_seq - 1

    return stream_open_seq - 1


@router.get("/api/threads/{thread_id}/events")
async def stream_events(thread_id: str):
    """Stream events for a thread via SSE.

    If a stream is already in progress, starts from that stream.open event
    to catch up with the current streaming session. Otherwise starts from
    current max event_seq to avoid replaying history.

    Uses server-side batching to reduce HTTP overhead during streaming.
    """
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Check if there's an active streaming session we should catch up to.
    # Find the last stream.open and stream.close events to determine if streaming
    # is in progress. If stream.open is more recent, we're mid-stream and should
    # replay from just before that stream.open.
    try:
        cur = core.db.conn.execute("""
            SELECT type, event_seq FROM events
            WHERE thread_id = ? AND type IN ('stream.open', 'stream.close')
            ORDER BY event_seq DESC LIMIT 2
        """, (thread_id,))
        recent_stream_events = cur.fetchall()

        # Default to current max seq (don't replay history)
        current_max_seq = core.db.max_event_seq(thread_id)

        if recent_stream_events:
            last_event = recent_stream_events[0]
            last_type = last_event["type"] if "type" in last_event.keys() else last_event[0]
            last_seq = last_event["event_seq"] if "event_seq" in last_event.keys() else last_event[1]

            if last_type == "stream.open":
                # Stream is in progress - start from just before stream.open,
                # but include the preceding user turn when another client wrote
                # it just before provider streaming began.
                current_max_seq = _active_stream_replay_after_seq(core.db, thread_id, int(last_seq))
    except Exception:
        current_max_seq = -1

    async def event_generator():
        # Use a dedicated database connection for SSE to avoid contention
        # with the scheduler which uses the global db connection.
        # IMPORTANT: Use the same path as the global db to ensure we see
        # the same data (including events from other processes like TUI).
        sse_db = ThreadsDB(core.db.path)

        # Use short poll interval and minimal backoff for responsive streaming
        # max_backoff=0.03 (30ms) prevents event accumulation during idle periods
        watcher = EventWatcher(sse_db, thread_id, after_seq=current_max_seq,
                               poll_sec=0.015, max_backoff=0.03)
        try:
            import time
            last_batch_time = time.monotonic()

            async for batch in watcher.aiter():
                now = time.monotonic()
                elapsed_ms = (now - last_batch_time) * 1000

                # Log large batches with detailed timing info to diagnose delays
                if len(batch) > 5:
                    # Check if events were written in burst or read delay
                    event_types = [row["type"] if "type" in row.keys() else "?" for row in batch]
                    first_seq = batch[0]["event_seq"]
                    last_seq = batch[-1]["event_seq"]

                    # Check which scheduler is running (if any)
                    eggw_scheduler = bool(thread_id and scheduler_running(get_thread_root_id(thread_id)))

                    print(f"[SSE] Large batch: {len(batch)} events (seq {first_seq}-{last_seq}), "
                          f"{elapsed_ms:.0f}ms since last, eggw_sched={eggw_scheduler}, "
                          f"types={event_types[:5]}{'...' if len(event_types) > 5 else ''}")

                last_batch_time = now

                # Batch all events from this poll into a single SSE message
                # This reduces HTTP overhead significantly during fast streaming
                if len(batch) == 1:
                    # Single event - send directly
                    row = batch[0]
                    event_type = row["type"] if "type" in row.keys() else "unknown"
                    payload = {}
                    if "payload_json" in row.keys() and row["payload_json"]:
                        try:
                            payload = json.loads(row["payload_json"])
                        except:
                            pass

                    event_data = {
                        "event_seq": row["event_seq"],
                        "event_type": event_type,
                        "ts": row["ts"] if "ts" in row.keys() else None,
                        "msg_id": row["msg_id"] if "msg_id" in row.keys() else None,
                        "invoke_id": row["invoke_id"] if "invoke_id" in row.keys() else None,
                        "payload": payload,
                    }

                    yield {
                        "event": event_type,
                        "data": json.dumps(event_data),
                    }
                else:
                    # Multiple events - send each but they're already batched by poll interval
                    for row in batch:
                        event_type = row["type"] if "type" in row.keys() else "unknown"
                        payload = {}
                        if "payload_json" in row.keys() and row["payload_json"]:
                            try:
                                payload = json.loads(row["payload_json"])
                            except:
                                pass

                        event_data = {
                            "event_seq": row["event_seq"],
                            "event_type": event_type,
                            "ts": row["ts"] if "ts" in row.keys() else None,
                            "msg_id": row["msg_id"] if "msg_id" in row.keys() else None,
                            "invoke_id": row["invoke_id"] if "invoke_id" in row.keys() else None,
                            "payload": payload,
                        }

                        yield {
                            "event": event_type,
                            "data": json.dumps(event_data),
                        }
        except asyncio.CancelledError:
            pass

    # Use ping to prevent buffering and keep connection alive
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
                    append_message(core.db, thread_id, role="user", content=content)
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
