from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


class SnapshotBuilder:
    """Builds a human-readable snapshot from events for caching in threads.snapshot_json.

    Minimal pass that reconstructs message list with roles/content and tool calls if present.
    """

    def __init__(self):
        pass

    def build(self, events: Iterable[dict]) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        # Very lightweight: just fold msg.create and stream content deltas into assistant messages.
        # In a fuller implementation you'd also fold tool_calls and reasoning parts.
        assistant_buf: Dict[str, List[str]] = {}
        last_assistant: Optional[Dict[str, Any]] = None
        for e in events:
            # Support both sqlite3.Row and plain dict; attribute access by key
            t = e["type"] if isinstance(e, dict) else e["type"]
            pj = e["payload_json"] if isinstance(e, dict) else e["payload_json"]
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
            if t == 'msg.create':
                msg = {
                    "msg_id": (e.get("msg_id") if isinstance(e, dict) else e["msg_id"]),
                    "role": payload.get("role"),
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
                messages.append(msg)
            elif t == 'stream.open':
                inv = (e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]) 
                assistant_buf[inv] = []
            elif t == 'stream.delta':
                inv = (e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]) 
                if inv in assistant_buf:
                    # Support text-only delta payloads for now
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
                inv = (e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]) 
                parts = assistant_buf.pop(inv, None)
                if parts is not None:
                    last_assistant = {
                        "role": "assistant",
                        "content": "".join(parts),
                        "invoke_id": inv,
                    }
                    # Attach reasoning if any (from streaming)
                    rparts = assistant_buf.pop(inv + "::reason", None)
                    if rparts:
                        last_assistant["reasoning"] = "".join(rparts)
                    # Attach tool-call args stream (optional metadata)
                    tc_keys = [k for k in list(assistant_buf.keys()) if isinstance(k, str) and k.startswith(inv + "::toolcall::")]
                    if tc_keys:
                        last_assistant.setdefault("tool_calls_stream", {})
                        for tk in tc_keys:
                            name = tk.split("::toolcall::",1)[1]
                            chunks = assistant_buf.pop(tk, [])
                            last_assistant["tool_calls_stream"][name] = "".join(chunks)
                    # Attach tool output streaming if any (metadata)
                    tool_keys = [k for k in list(assistant_buf.keys()) if isinstance(k, str) and k.startswith(inv + "::tool::")]
                    if tool_keys:
                        last_assistant.setdefault("tool_stream", {})
                        for tk in tool_keys:
                            name = tk.split("::tool::",1)[1]
                            chunks = assistant_buf.pop(tk, [])
                            last_assistant["tool_stream"][name] = "".join(chunks)
                    messages.append(last_assistant)
        return {"messages": messages}
