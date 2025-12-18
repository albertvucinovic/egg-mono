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

            # Preserve the full msg.create payload so provider-specific
            # fields (e.g. Gemini thought signatures) survive round-trips.
            #
            # Snapshot messages are still considered a UI-friendly cache,
            # but they must remain faithful enough to reconstruct provider
            # requests for advanced models.
            role = payload.get("role")
            msg: Dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
            msg["msg_id"] = _get(e, "msg_id")
            msg["role"] = role
            # Preserve the original event timestamp if available so UIs
            # can display when a message was created.
            ts_val = _get(e, "ts")
            if ts_val is not None:
                msg["ts"] = ts_val
            # The selective field copying above was replaced by payload
            # passthrough. We intentionally keep all payload keys.

            messages.append(msg)

        snap: Dict[str, Any] = {"messages": messages}

        # Best-effort: attach approximate token statistics so that UIs
        # and tools can display context length and per-message token
        # counts without having to re-scan the snapshot on every
        # request.  We deliberately ignore failures here so that token
        # counting never interferes with core snapshot building.
        try:
            from .token_count import snapshot_token_stats  # type: ignore

            snap["token_stats"] = snapshot_token_stats(snap)
        except Exception:
            pass

        return snap
