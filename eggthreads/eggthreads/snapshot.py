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
        """Builds a human-readable snapshot from msg.create events only.

        For snapshot purposes we want a stable, final message list that:
          - preserves system / user / assistant / tool messages,
          - carries model_key, tool_calls, reasoning, no_api, keep_user_turn,
          - ignores streaming events (stream.open/delta/close) and tool_call.*.

        Streaming is represented live via EventWatcher; snapshots should
        reflect only completed messages.
        """
        messages: List[Dict[str, Any]] = []

        def _get(row, key):
            if isinstance(row, dict):
                return row.get(key)
            return row[key]

        for e in events:
            # Support both sqlite3.Row and plain dict; attribute access by key
            t = _get(e, "type")
            if t != "msg.create":
                continue
            pj = _get(e, "payload_json")
            try:
                payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
            except Exception:
                payload = {}

            role = payload.get("role")
            msg: Dict[str, Any] = {
                "msg_id": _get(e, "msg_id"),
                "role": role,
            }
            # Preserve model_key if present so UIs can display the model for each message
            if isinstance(payload, dict) and payload.get("model_key"):
                msg["model_key"] = payload.get("model_key")
            # Copy content if present
            if "content" in payload:
                msg["content"] = payload.get("content")
            # Preserve special flags for API filtering and turn management
            if isinstance(payload, dict):
                if payload.get("no_api"):
                    msg["no_api"] = payload.get("no_api")
                if payload.get("keep_user_turn"):
                    msg["keep_user_turn"] = payload.get("keep_user_turn")
            # Tool messages
            if role == "tool":
                if payload.get("name"):
                    msg["name"] = payload.get("name")
                if payload.get("tool_call_id"):
                    msg["tool_call_id"] = payload.get("tool_call_id")
                # Preserve user_tool_call so that user-initiated
                # command outputs can be distinguished from genuine
                # assistant tool outputs when rebuilding API context.
                if payload.get("user_tool_call"):
                    msg["user_tool_call"] = payload.get("user_tool_call")
            # Assistant messages
            if role == "assistant":
                if payload.get("tool_calls"):
                    msg["tool_calls"] = payload.get("tool_calls")
                if payload.get("reasoning"):
                    msg["reasoning"] = payload.get("reasoning")

            messages.append(msg)

        return {"messages": messages}
