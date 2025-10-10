from __future__ import annotations

import json
from typing import Any, Dict, Optional


def ev_stream_open(event_id: str, thread_id: str, msg_id: str, invoke_id: str) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "type": "stream.open",
        "msg_id": msg_id,
        "invoke_id": invoke_id,
        "payload_json": json.dumps({})
    }


def ev_stream_delta(event_id: str, thread_id: str, invoke_id: str, chunk_seq: int, delta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "type": "stream.delta",
        "invoke_id": invoke_id,
        "chunk_seq": chunk_seq,
        "payload_json": json.dumps(delta),
    }


def ev_stream_close(event_id: str, thread_id: str, invoke_id: str, summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "type": "stream.close",
        "invoke_id": invoke_id,
        "payload_json": json.dumps(summary or {})
    }


def ev_msg_create(event_id: str, thread_id: str, msg_id: str, role: str, content: str, extra: Optional[Dict[str, Any]] = None):
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "type": "msg.create",
        "msg_id": msg_id,
        "payload_json": json.dumps({"role": role, "content": content, **(extra or {})})
    }


def ev_msg_edit(event_id: str, thread_id: str, msg_id: str, new_content: str, extra: Optional[Dict[str, Any]] = None):
    return {
        "event_id": event_id,
        "thread_id": thread_id,
        "type": "msg.edit",
        "msg_id": msg_id,
        "payload_json": json.dumps({"content": new_content, **(extra or {})})
    }
