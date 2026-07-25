"""Helpers for min-verbosity hidden activity run summaries."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Set

from rich.text import Text

from eggthreads.inspection import shortest_unique_record_id_suffix
from eggthreads.tool_effects import ToolEffect, classify_tool_effect


_ARGUMENTS_UNSET = object()


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
class MinToolCallSummary:
    """One inspectable tool call retained by a min-verbosity run summary."""

    name: str
    tool_call_id: str
    effect: ToolEffect = ToolEffect.UNKNOWN
    group_id: str = ""


@dataclass(frozen=True)
class MinToolResultSummary:
    """One inspectable result paired to a compact tool-call identity."""

    name: str
    tool_call_id: str
    record_id: str
    effect: ToolEffect = ToolEffect.UNKNOWN


@dataclass(frozen=True)
class MinToolStyles:
    """Theme-resolved styles for compact tool identities."""

    summary: str
    separator: str
    read: str
    may_write: str
    unknown: str


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
    tool_calls: List[MinToolCallSummary] = field(default_factory=list)
    tool_results_list: List[MinToolResultSummary] = field(default_factory=list)
    record_ids: List[str] = field(default_factory=list)
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
        self.tool_calls.clear()
        self.tool_results_list.clear()
        self.record_ids.clear()
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

    def add_record_ids(self, record_ids: Any) -> None:
        """Merge the current thread's inspectable IDs for unambiguous hints."""

        seen = {record_id.casefold() for record_id in self.record_ids}
        for value in record_ids or ():
            record_id = str(value or "").strip()
            if record_id and record_id.casefold() not in seen:
                self.record_ids.append(record_id)
                seen.add(record_id.casefold())

    def add_tool_execution(
        self,
        *,
        name: Any = None,
        arguments: Any = _ARGUMENTS_UNSET,
        tokens: Any = 0,
        tool_call_id: Optional[str] = None,
        group_id: Any = None,
    ) -> None:
        """Count a tool execution/tool call, de-duping by call id when known."""
        call_id = str(tool_call_id or "").strip()
        if call_id:
            if call_id in self._seen_tool_call_ids:
                return
            self._seen_tool_call_ids.add(call_id)
        self.tool_executions += 1
        self._add_tool_name(name)
        normalized_name = self._normalize_tool_name(name) or "tool"
        if call_id and arguments is not _ARGUMENTS_UNSET:
            self.tool_calls.append(
                MinToolCallSummary(
                    name=normalized_name,
                    tool_call_id=call_id,
                    effect=classify_tool_effect(normalized_name, arguments).effect,
                    group_id=str(group_id or "").strip(),
                )
            )
        self.add_tokens(tokens)

    def add_tool_result(
        self,
        *,
        name: Any = None,
        tokens: Any = 0,
        record_id: Any = None,
        tool_call_id: Any = None,
        effect: ToolEffect = ToolEffect.UNKNOWN,
    ) -> None:
        self.tool_results += 1
        self._add_result_tool_name(name)
        if record_id:
            self.add_record_ids((record_id,))
        call_id = str(tool_call_id or "").strip()
        result_id = str(record_id or "").strip()
        if call_id or result_id:
            self.tool_results_list.append(
                MinToolResultSummary(
                    name=self._normalize_tool_name(name) or "tool",
                    tool_call_id=call_id,
                    record_id=result_id,
                    effect=effect,
                )
            )
        self.add_tokens(tokens)


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def _min_tool_entry(call: MinToolCallSummary, record_ids: List[str]) -> str:
    hint = shortest_unique_record_id_suffix(call.tool_call_id, record_ids)
    return f"{call.name}({hint})"


def _min_tool_result_entry(result: MinToolResultSummary, record_ids: List[str]) -> str:
    identity = result.record_id or result.tool_call_id
    hint = shortest_unique_record_id_suffix(identity, record_ids)
    return f"{result.name}({hint})"


def _grouped_tool_calls(summary: MinHiddenActivitySummary) -> List[List[MinToolCallSummary]]:
    groups: List[List[MinToolCallSummary]] = []
    for call in summary.tool_calls:
        if groups and call.group_id and groups[-1][0].group_id == call.group_id:
            groups[-1].append(call)
        else:
            groups.append([call])
    return groups


def _grouped_tool_activity(
    summary: MinHiddenActivitySummary,
) -> List[tuple[List[MinToolCallSummary], List[MinToolResultSummary]]]:
    """Pair each ordered call group with its results."""

    groups = [(calls, []) for calls in _grouped_tool_calls(summary)]
    call_group_indexes = {
        call.tool_call_id: group_index
        for group_index, (calls, _results) in enumerate(groups)
        for call in calls
        if call.tool_call_id
    }
    unmatched_results: List[MinToolResultSummary] = []
    for result in summary.tool_results_list:
        group_index = call_group_indexes.get(result.tool_call_id)
        if group_index is None:
            unmatched_results.append(result)
        else:
            groups[group_index][1].append(result)
    if unmatched_results:
        groups.append(([], unmatched_results))
    return groups


def _min_tool_activity_group(
    calls: List[MinToolCallSummary],
    results: List[MinToolResultSummary],
    *,
    call_record_ids: List[str],
    result_record_ids: List[str],
) -> str:
    parts: List[str] = []
    if calls:
        parts.append(
            "calls [" + " ".join(_min_tool_entry(call, call_record_ids) for call in calls) + "]"
        )
    if results:
        parts.append(
            "results ["
            + " ".join(_min_tool_result_entry(result, result_record_ids) for result in results)
            + "]"
        )
    return " ".join(parts)


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
    activity_groups = _grouped_tool_activity(summary)
    if activity_groups:
        call_record_ids = [*summary.record_ids, *(call.tool_call_id for call in summary.tool_calls)]
        result_record_ids = [*summary.record_ids, *(result.record_id for result in summary.tool_results_list)]
        text += "\n" + " | ".join(
            _min_tool_activity_group(
                calls,
                results,
                call_record_ids=call_record_ids,
                result_record_ids=result_record_ids,
            )
            for calls, results in activity_groups
        )
    else:
        tool_names = summary.tool_names or summary._result_tool_names
        if tool_names:
            text += "\nTools: " + ", ".join(tool_names)
    return text


def min_hidden_activity_summary_text(
    summary: MinHiddenActivitySummary,
    *,
    styles: MinToolStyles,
) -> Text:
    """Render a min summary with each complete tool identity color-coded."""

    plain = format_min_hidden_activity_summary(summary)
    if not plain or (not summary.tool_calls and not summary.tool_results_list):
        return Text(plain, style=styles.summary)

    counts = plain.split("\n", 1)[0]
    text = Text(counts, style=styles.summary)
    record_ids = [*summary.record_ids, *(call.tool_call_id for call in summary.tool_calls)]
    effect_styles = {
        ToolEffect.READ: styles.read,
        ToolEffect.MAY_WRITE: styles.may_write,
        ToolEffect.UNKNOWN: styles.unknown,
    }
    result_record_ids = [*summary.record_ids, *(result.record_id for result in summary.tool_results_list)]
    text.append("\n")
    for group_index, (calls, results) in enumerate(_grouped_tool_activity(summary)):
        if group_index:
            text.append(" | ", style=styles.separator)
        if calls:
            text.append("calls [", style=styles.summary)
            for index, call in enumerate(calls):
                if index:
                    text.append(" ", style=styles.separator)
                text.append(_min_tool_entry(call, record_ids), style=effect_styles[call.effect])
            text.append("]", style=styles.summary)
        if calls and results:
            text.append(" ", style=styles.separator)
        if results:
            text.append("results [", style=styles.summary)
            for index, result in enumerate(results):
                if index:
                    text.append(" ", style=styles.separator)
                text.append(
                    _min_tool_result_entry(result, result_record_ids),
                    style=effect_styles[result.effect],
                )
            text.append("]", style=styles.summary)
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
