from __future__ import annotations

"""Small UI/API helpers for output optimizer observability.

The optimizer's authoritative audit data remains on
``tool_call.output_approval.channels["optimizer"]``.  Helpers here derive a
compact public metadata shape for final tool messages and UI rendering; they do
not affect raw output capture or optimizer decisions.
"""

import math
import json
from pathlib import Path
from typing import Any, Mapping


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_savings_pct(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return ""
    if abs(number - round(number)) < 0.05:
        return f"{round(number):.0f}% saved"
    return f"{number:.1f}% saved"


def _artifact_id_from_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return Path(text).name
    except Exception:
        return text.rsplit("/", 1)[-1]


def _successful_optimizer_parts(
    payload: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]] | None:
    approval = _as_mapping(payload)
    channels = _as_mapping(approval.get("channels"))
    optimizer = _as_mapping(channels.get("optimizer"))
    if not optimizer:
        optimizer = _as_mapping(approval.get("optimizer"))
    if not optimizer:
        return None
    if not bool(optimizer.get("optimized")) or bool(optimizer.get("fallback")):
        return None
    if optimizer.get("published") is False:
        return None
    return approval, channels, optimizer


def optimizer_public_metadata_from_output_approval(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return compact public optimizer metadata for a tool message.

    ``None`` means no successful optimizer publication should be displayed.
    The input is a ``tool_call.output_approval`` payload.  Only successful,
    published optimizer use is surfaced; fallbacks/default output approvals stay
    quiet to avoid UI clutter.
    """

    parts = _successful_optimizer_parts(payload)
    if parts is None:
        return None
    approval, channels, optimizer = parts

    artifact_path = str(approval.get("artifact_path") or channels.get("artifact") or "").strip()
    artifact_id = _artifact_id_from_path(artifact_path)
    raw_channel = _as_mapping(channels.get("raw"))
    raw_available = bool(artifact_id or raw_channel.get("stored_in_finished_event"))

    savings_pct = _as_float(optimizer.get("published_savings_pct"))
    if savings_pct is None:
        savings_pct = _as_float(optimizer.get("savings_pct"))
    savings_text = _format_savings_pct(savings_pct)

    parts = ["Egg optimized"]
    if savings_text:
        parts.append(savings_text)
    if raw_available:
        parts.append("raw available")
    summary = " · ".join(parts)

    out: dict[str, Any] = {
        "optimized": True,
        "summary": summary,
        "raw_available": raw_available,
        "artifact_available": bool(artifact_id),
    }
    if savings_pct is not None:
        out["savings_pct"] = savings_pct
    for key in ("raw_chars", "optimized_chars", "published_chars", "confidence"):
        value = _as_float(optimizer.get(key)) if key == "confidence" else _as_int(optimizer.get(key))
        if value is not None:
            out[key] = value
    for key in ("filter_name", "reason", "name"):
        value = optimizer.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if artifact_id:
        out["artifact_id"] = artifact_id
        out["raw_hint"] = f"read_long_tool_output('{artifact_id}', chunk_number=1)"
        out["summary_with_artifact"] = summary.replace("raw available", f"raw artifact {artifact_id}")
    else:
        out["summary_with_artifact"] = summary
    return out


def format_output_optimizer_summary(metadata: Mapping[str, Any] | None, *, include_artifact_id: bool = False) -> str:
    """Return a concise human-readable optimizer summary, or ``""``."""

    data = _as_mapping(metadata)
    if not data or not bool(data.get("optimized")):
        return ""
    if include_artifact_id:
        text = str(data.get("summary_with_artifact") or "").strip()
        if text:
            return text
    return str(data.get("summary") or "").strip()


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except Exception:
        try:
            return row[index]
        except Exception:
            return None


def _parse_event_payload(payload_json: Any) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _approx_tokens_from_chars(chars: Any) -> int:
    value = _as_int(chars)
    if value is None or value <= 0:
        return 0
    return max(1, round(value / 4))


def _count_tokens_best_effort(text: Any, fallback_chars: Any) -> int:
    if isinstance(text, str):
        try:
            from ..token_count import count_text_tokens

            value = int(count_text_tokens(text))
            if value >= 0:
                return value
        except Exception:
            pass
    return _approx_tokens_from_chars(fallback_chars)


def collect_output_optimizer_savings(db: Any, thread_id: str) -> dict[str, Any]:
    """Aggregate local optimizer publication savings for one thread.

    This intentionally reports *publication/context* savings from optimized
    tool-output previews, not guaranteed bill reductions.  Provider billing can
    differ because of compaction, caching, model pricing, and whether a given
    optimized preview was actually sent in a later API request.
    """

    try:
        rows = db.conn.execute(
            """
            SELECT type, payload_json
              FROM events
             WHERE thread_id=?
               AND type IN ('tool_call.finished', 'tool_call.output_approval', 'msg.create')
             ORDER BY event_seq ASC
            """,
            (thread_id,),
        ).fetchall()
    except Exception:
        return {}

    finished_outputs: dict[str, str] = {}
    approvals_by_tool_call_id: dict[str, dict[str, Any]] = {}
    tool_messages_by_tool_call_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        ev_type = str(_row_value(row, "type", 0) or "")
        payload = _parse_event_payload(_row_value(row, "payload_json", 1))
        tool_call_id = str(payload.get("tool_call_id") or "")
        if ev_type == "msg.create":
            if payload.get("role") == "tool" and tool_call_id:
                tool_messages_by_tool_call_id[tool_call_id] = payload
            continue
        if not tool_call_id:
            continue
        if ev_type == "tool_call.finished" and "output" in payload:
            finished_outputs[tool_call_id] = str(payload.get("output") or "")
        elif ev_type == "tool_call.output_approval":
            approvals_by_tool_call_id[tool_call_id] = payload

    all_tool_call_ids = set(finished_outputs) | set(approvals_by_tool_call_id) | set(tool_messages_by_tool_call_id)
    total_tool_calls = len(all_tool_call_ids)
    optimized_tool_calls = sum(1 for approval in approvals_by_tool_call_id.values() if _successful_optimizer_parts(approval) is not None)
    optimized_tool_call_pct = (optimized_tool_calls / total_tool_calls * 100.0) if total_tool_calls else 0.0

    context_tool_call_ids = [
        tool_call_id
        for tool_call_id, message in tool_messages_by_tool_call_id.items()
        if tool_call_id in approvals_by_tool_call_id and not bool(message.get("no_api"))
    ]
    context_tool_calls = len(context_tool_call_ids)
    if total_tool_calls <= 0:
        return {}

    count = 0
    optimized_raw_chars = 0
    optimized_published_chars = 0
    optimized_saved_chars = 0
    optimized_raw_tokens = 0
    optimized_published_tokens = 0
    optimized_saved_tokens = 0
    total_context_chars = 0
    published_context_chars = 0
    context_saved_chars = 0
    total_context_tokens = 0
    published_context_tokens = 0
    context_saved_tokens = 0
    by_filter: dict[str, dict[str, Any]] = {}

    for tool_call_id in sorted(context_tool_call_ids):
        approval = approvals_by_tool_call_id[tool_call_id]
        raw_output = finished_outputs.get(tool_call_id)
        preview = approval.get("preview") if isinstance(approval.get("preview"), str) else ""
        actual_published_chars = len(preview)

        baseline_chars = actual_published_chars
        published_chars = actual_published_chars
        saved_chars = 0
        baseline_tokens = _count_tokens_best_effort(preview, baseline_chars)
        published_tokens = baseline_tokens

        parts = _successful_optimizer_parts(approval)
        if parts is not None:
            _approval, _channels, optimizer = parts
            count += 1

            opt_raw_chars = _as_int(optimizer.get("raw_chars"))
            if opt_raw_chars is None and raw_output is not None:
                opt_raw_chars = len(raw_output)
            opt_published_chars = _as_int(optimizer.get("published_chars"))
            if opt_published_chars is None:
                opt_published_chars = actual_published_chars
            opt_raw_chars = max(int(opt_raw_chars or 0), 0)
            opt_published_chars = max(int(opt_published_chars or 0), 0)
            opt_saved_chars = max(opt_raw_chars - opt_published_chars, 0)

            baseline_value = _as_int(optimizer.get("baseline_chars"))
            if baseline_value is None:
                baseline_value = opt_raw_chars
            baseline_chars = max(int(baseline_value or 0), opt_published_chars)
            published_chars = opt_published_chars
            saved_chars = max(baseline_chars - published_chars, 0)

            opt_raw_tokens = _count_tokens_best_effort(raw_output, opt_raw_chars)
            opt_published_tokens = _count_tokens_best_effort(preview, opt_published_chars)
            opt_saved_tokens = max(opt_raw_tokens - opt_published_tokens, 0)
            optimized_raw_chars += opt_raw_chars
            optimized_published_chars += opt_published_chars
            optimized_saved_chars += opt_saved_chars
            optimized_raw_tokens += opt_raw_tokens
            optimized_published_tokens += opt_published_tokens
            optimized_saved_tokens += opt_saved_tokens

            if raw_output is not None and len(raw_output) == baseline_chars:
                baseline_tokens = _count_tokens_best_effort(raw_output, baseline_chars)
            else:
                baseline_tokens = _approx_tokens_from_chars(baseline_chars)
            published_tokens = _count_tokens_best_effort(preview, published_chars)

            filter_name = str(optimizer.get("filter_name") or optimizer.get("name") or "unknown").strip() or "unknown"
            entry = by_filter.setdefault(
                filter_name,
                {"count": 0, "context_chars": 0, "published_chars": 0, "saved_chars": 0, "saved_tokens": 0},
            )
            entry["count"] += 1
            entry["context_chars"] += baseline_chars
            entry["published_chars"] += published_chars
            entry["saved_chars"] += saved_chars
            entry["saved_tokens"] += max(baseline_tokens - published_tokens, 0)

        saved_tokens = max(baseline_tokens - published_tokens, 0)
        total_context_chars += baseline_chars
        published_context_chars += published_chars
        context_saved_chars += saved_chars
        total_context_tokens += baseline_tokens
        published_context_tokens += published_tokens
        context_saved_tokens += saved_tokens

    context_optimized_pct = (count / context_tool_calls * 100.0) if context_tool_calls else 0.0

    return {
        "total_tool_calls": total_tool_calls,
        "context_tool_calls": context_tool_calls,
        "total_tool_outputs": context_tool_calls,
        "total_published_tool_outputs": len(approvals_by_tool_call_id),
        "optimized_tool_calls": optimized_tool_calls,
        "context_optimized_tool_calls": count,
        "optimized_tool_outputs": count,
        "optimized_tool_call_pct": optimized_tool_call_pct,
        "context_optimized_tool_call_pct": context_optimized_pct,
        "optimized_tool_output_pct": context_optimized_pct,
        "total_context_chars": total_context_chars,
        "published_context_chars": published_context_chars,
        "context_saved_chars": context_saved_chars,
        "context_savings_pct": (context_saved_chars / total_context_chars * 100.0) if total_context_chars else 0.0,
        "total_context_tokens": total_context_tokens,
        "published_context_tokens": published_context_tokens,
        "context_saved_tokens": context_saved_tokens,
        "context_token_savings_pct": (context_saved_tokens / total_context_tokens * 100.0) if total_context_tokens else 0.0,
        "raw_chars": optimized_raw_chars,
        "published_chars": optimized_published_chars,
        "saved_chars": optimized_saved_chars,
        "savings_pct": (optimized_saved_chars / optimized_raw_chars * 100.0) if optimized_raw_chars else 0.0,
        "raw_tokens": optimized_raw_tokens,
        "published_tokens": optimized_published_tokens,
        "saved_tokens": optimized_saved_tokens,
        "token_savings_pct": (optimized_saved_tokens / optimized_raw_tokens * 100.0) if optimized_raw_tokens else 0.0,
        "by_filter": by_filter,
    }


__all__ = [
    "collect_output_optimizer_savings",
    "format_output_optimizer_summary",
    "optimizer_public_metadata_from_output_approval",
]
