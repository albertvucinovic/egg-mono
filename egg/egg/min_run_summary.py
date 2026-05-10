"""Helpers for min-verbosity hidden activity run summaries."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Set


def _positive_int(value: Any) -> int:
    try:
        iv = int(value)
    except Exception:
        return 0
    return iv if iv > 0 else 0


def count_min_hidden_text_tokens(text: Any) -> int:
    """Best-effort token count for hidden min-verbosity summary details."""
    if not isinstance(text, str) or not text:
        return 0
    try:
        from eggthreads import count_text_tokens

        return _positive_int(count_text_tokens(text))
    except Exception:
        # Keep this helper safe for display paths.  The shared token helper
        # should normally be available, but display should not fail if token
        # accounting is temporarily unavailable.
        return 0


def serialize_min_tool_call_tokens(tool_call: Any) -> str:
    """Return a stable string representation for approximating tool-call tokens."""
    try:
        return json.dumps(tool_call, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(tool_call or "")


@dataclass
class MinHiddenActivitySummary:
    """Aggregate one consecutive run of hidden min-verbosity activity.

    A run is bounded by visible transcript items.  It counts hidden reasoning,
    tool executions/tool calls, and tool results while carrying a compact list
    of known tool names and a best-effort hidden-token total.
    """

    tool_executions: int = 0
    tool_results: int = 0
    reasoning_blocks: int = 0
    total_tokens: int = 0
    tool_names: List[str] = field(default_factory=list)
    _result_tool_names: List[str] = field(default_factory=list, repr=False)
    _seen_tool_call_ids: Set[str] = field(default_factory=set, repr=False)

    def has_activity(self) -> bool:
        return bool(self.tool_executions or self.tool_results or self.reasoning_blocks)

    def clear(self) -> None:
        self.tool_executions = 0
        self.tool_results = 0
        self.reasoning_blocks = 0
        self.total_tokens = 0
        self.tool_names.clear()
        self._result_tool_names.clear()
        self._seen_tool_call_ids.clear()

    def add_tokens(self, tokens: Any) -> None:
        self.total_tokens += _positive_int(tokens)

    @staticmethod
    def _normalize_tool_name(name: Any) -> str:
        return str(name or "").strip()

    def _add_tool_name(self, name: Any) -> None:
        text = self._normalize_tool_name(name)
        if text:
            self.tool_names.append(text)

    def _add_result_tool_name(self, name: Any) -> None:
        text = self._normalize_tool_name(name)
        if text:
            self._result_tool_names.append(text)

    def add_reasoning_block(self, *, tokens: Any = 0) -> None:
        self.reasoning_blocks += 1
        self.add_tokens(tokens)

    def add_tool_execution(
        self,
        *,
        name: Any = None,
        tokens: Any = 0,
        tool_call_id: Optional[str] = None,
    ) -> None:
        """Count a tool execution/tool call, de-duping by call id when known."""
        call_id = str(tool_call_id or "").strip()
        if call_id:
            if call_id in self._seen_tool_call_ids:
                return
            self._seen_tool_call_ids.add(call_id)
        self.tool_executions += 1
        self._add_tool_name(name)
        self.add_tokens(tokens)

    def add_tool_result(self, *, name: Any = None, tokens: Any = 0) -> None:
        self.tool_results += 1
        self._add_result_tool_name(name)
        self.add_tokens(tokens)


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def format_min_hidden_activity_summary(summary: MinHiddenActivitySummary) -> str:
    """Format one min-verbosity hidden activity summary item."""
    if not summary.has_activity():
        return ""

    parts: List[str] = []
    if summary.tool_executions:
        parts.append(f"Executed {_plural(summary.tool_executions, 'tool')}")
    if summary.tool_results:
        parts.append(f"got {_plural(summary.tool_results, 'tool result')}")
    if summary.reasoning_blocks:
        parts.append(_plural(summary.reasoning_blocks, 'reasoning block'))
    if summary.total_tokens > 0:
        parts.append(f"total tokens {summary.total_tokens}")

    text = ", ".join(parts)
    tool_names = summary.tool_names or summary._result_tool_names
    if tool_names:
        text += "\nTools: " + ", ".join(tool_names)
    return text


def snapshot_per_message_token_stats(db: Any, thread_id: str) -> Dict[str, Dict[str, Any]]:
    """Return cached per-message token stats from a thread snapshot, if present."""
    try:
        th = db.get_thread(thread_id)
        snap_raw = getattr(th, "snapshot_json", None) if th else None
        if not isinstance(snap_raw, str) or not snap_raw:
            return {}
        snap = json.loads(snap_raw)
        if not isinstance(snap, dict):
            return {}
        token_stats = snap.get("token_stats")
        if not isinstance(token_stats, dict):
            return {}
        per_message = token_stats.get("per_message")
        if not isinstance(per_message, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for msg_id, info in per_message.items():
            if isinstance(msg_id, str) and isinstance(info, dict):
                out[msg_id] = info
        return out
    except Exception:
        return {}


def min_message_token_count(
    per_message_tokens: Dict[str, Dict[str, Any]],
    msg_id: str,
    field: str,
    fallback_text: Any = "",
) -> int:
    """Return a per-message token count field or approximate fallback text."""
    key_by_field = {
        "content": "content_tokens",
        "reasoning": "reasoning_tokens",
        "tool_calls": "tool_calls_tokens",
        "total": "total_tokens",
    }
    token_key = key_by_field.get(field, field)
    if msg_id:
        info = per_message_tokens.get(msg_id)
        if isinstance(info, dict):
            tokens = _positive_int(info.get(token_key))
            if tokens:
                return tokens
    return count_min_hidden_text_tokens(fallback_text)
