from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


class SnapshotBuilder:
    """Builds a human-readable snapshot from events for caching in threads.snapshot_json.

    Minimal pass that reconstructs message list with roles/content and tool calls if present.

    Additionally, avoids duplicating the streaming assistant message when a
    completed assistant message (msg.create) for the same turn exists.
    """

    def __init__(self):
        pass

    def build(self, events: Iterable[dict]) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        # Very lightweight: just fold msg.create and stream content deltas into assistant messages.
        assistant_buf: Dict[str, List[str]] = {}
        # Track event_seq for ordering and later de-duplication
        def _get(row, key):
            if isinstance(row, dict):
                return row.get(key)
            return row[key]

        pending_stream_assistant: Optional[Dict[str, Any]] = None
        pending_stream_seq: Optional[int] = None
        for e in events:
            # Support both sqlite3.Row and plain dict; attribute access by key
            t = _get(e, "type")
            pj = _get(e, "payload_json")
            ev_seq = _get(e, "event_seq")
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})

            if t == 'msg.create':
                msg = {
                    "msg_id": _get(e, "msg_id"),
                    "role": payload.get("role"),
                    "_event_seq": ev_seq,
                }
                # Preserve model_key if present so UIs can display the model for each message
                if isinstance(payload, dict) and payload.get("model_key"):
                    msg["model_key"] = payload.get("model_key")
                # Copy common fields if present
                if "content" in payload:
                    msg["content"] = payload.get("content")
                if payload.get("role") == 'tool':
                    if payload.get("name"):
                        msg["name"] = payload.get("name")
                    if payload.get("tool_call_id"):
                        msg["tool_call_id"] = payload.get("tool_call_id")
                if payload.get("role") == 'assistant':
                    if payload.get("tool_calls"):
                        msg["tool_calls"] = payload.get("tool_calls")
                    if payload.get("reasoning"):
                        msg["reasoning"] = payload.get("reasoning")
                    # If we have a pending streaming assistant from the previous invoke,
                    # and the completed assistant message has the same content/reasoning,
                    # drop the streaming one to avoid duplication.
                    if pending_stream_assistant is not None:
                        ps = pending_stream_assistant
                        def _norm(v: Optional[str]) -> str:
                            return (v or "").strip() if isinstance(v, str) else ""
                        same_content = _norm(ps.get("content")) == _norm(msg.get("content"))
                        same_reason = _norm(ps.get("reasoning")) == _norm(msg.get("reasoning"))
                        if same_content and same_reason:
                            # discard streaming version
                            pending_stream_assistant = None
                            pending_stream_seq = None
                        else:
                            # keep the streaming version before the completed assistant
                            ps["_event_seq"] = pending_stream_seq if pending_stream_seq is not None else -1
                            messages.append(ps)
                            pending_stream_assistant = None
                            pending_stream_seq = None
                messages.append(msg)

            elif t == 'stream.open':
                # Starting a new invoke. If there's a pending streaming assistant from
                # a previous invoke that wasn't followed by a completed assistant message,
                # flush it before moving on.
                if pending_stream_assistant is not None:
                    ps = pending_stream_assistant
                    ps["_event_seq"] = pending_stream_seq if pending_stream_seq is not None else -1
                    messages.append(ps)
                    pending_stream_assistant = None
                    pending_stream_seq = None
                inv = _get(e, "invoke_id")
                assistant_buf[inv] = []

            elif t == 'stream.delta':
                inv = _get(e, "invoke_id")
                if inv in assistant_buf:
                    # Accept both content/delta text and reasoning text under 'reason'
                    d = payload.get("text") or payload.get("content") or payload.get("delta") or ""
                    if isinstance(d, str):
                        assistant_buf[inv].append(d)
                    # Reasoning is kept separate; attach as metadata to the assistant message buffer
                    r = payload.get("reason")
                    if isinstance(r, str) and r:
                        assistant_buf.setdefault(inv + "::reason", []).append(r)
                    # Tool streaming payloads: {"tool": {"name":..., "text":...}}
                    tl = payload.get("tool")
                    if isinstance(tl, dict):
                        name = tl.get("name") or "tool"
                        txt = tl.get("text") or ""
                        assistant_buf.setdefault(inv + "::tool::" + name, [])
                        assistant_buf[inv + "::tool::" + name].append(str(txt))
                    # Tool-call arguments streaming: {"tool_call": {"name":..., "text":...}}
                    tcd = payload.get("tool_call")
                    if isinstance(tcd, dict):
                        name = tcd.get("name") or "tool"
                        txt = tcd.get("text") or ""
                        assistant_buf.setdefault(inv + "::toolcall::" + name, [])
                        assistant_buf[inv + "::toolcall::" + name].append(str(txt))

            elif t == 'stream.close':
                inv = _get(e, "invoke_id")
                parts = assistant_buf.pop(inv, None)
                if parts is not None:
                    stream_msg = {
                        "role": "assistant",
                        "content": "".join(parts),
                        "invoke_id": inv,
                        "_from_stream": True,
                    }
                    # Attach reasoning if any (from streaming)
                    rparts = assistant_buf.pop(inv + "::reason", None)
                    if rparts:
                        stream_msg["reasoning"] = "".join(rparts)
                    # Attach tool-call args stream (optional metadata)
                    tc_keys = [k for k in list(assistant_buf.keys()) if isinstance(k, str) and k.startswith(inv + "::toolcall::")]
                    if tc_keys:
                        stream_msg.setdefault("tool_calls_stream", {})
                        for tk in tc_keys:
                            name = tk.split("::toolcall::", 1)[1]
                            chunks = assistant_buf.pop(tk, [])
                            stream_msg["tool_calls_stream"][name] = "".join(chunks)
                    # Attach tool output streaming if any (metadata)
                    tool_keys = [k for k in list(assistant_buf.keys()) if isinstance(k, str) and k.startswith(inv + "::tool::")]
                    if tool_keys:
                        stream_msg.setdefault("tool_stream", {})
                        for tk in tool_keys:
                            name = tk.split("::tool::", 1)[1]
                            chunks = assistant_buf.pop(tk, [])
                            stream_msg["tool_stream"][name] = "".join(chunks)
                    # Do not append immediately; keep pending until we see whether a completed
                    # assistant msg occurs next. Record the close event seq for ordering if needed.
                    pending_stream_assistant = stream_msg
                    pending_stream_seq = ev_seq

        # If a streaming assistant is still pending by the end, flush it
        if pending_stream_assistant is not None:
            ps = pending_stream_assistant
            ps["_event_seq"] = pending_stream_seq if pending_stream_seq is not None else -1
            messages.append(ps)

        # Final pass: remove exact-duplicate streaming assistant messages that are immediately
        # followed by a completed assistant message with identical content/reasoning.
        filtered: List[Dict[str, Any]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("_from_stream"):
                # Look ahead for the next assistant (completed) message
                j = i + 1
                next_assistant = None
                while j < len(messages):
                    if messages[j].get("role") == "assistant" and not messages[j].get("_from_stream"):
                        next_assistant = messages[j]
                        break
                    # Stop if another stream.open/close derived assistant appears (different turn)
                    j += 1
                if next_assistant is not None:
                    def _norm(v: Optional[str]) -> str:
                        return (v or "").strip() if isinstance(v, str) else ""
                    same_content = _norm(m.get("content")) == _norm(next_assistant.get("content"))
                    same_reason = _norm(m.get("reasoning")) == _norm(next_assistant.get("reasoning"))
                    if same_content and same_reason:
                        # skip streaming duplicate
                        i += 1
                        continue
            filtered.append(m)
            i += 1

        # Clean helper keys
        for msg in filtered:
            if "_from_stream" in msg:
                del msg["_from_stream"]
            if "_event_seq" in msg:
                del msg["_event_seq"]
        return {"messages": filtered}
