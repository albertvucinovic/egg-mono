from __future__ import annotations

"""Small UI/API helpers for output optimizer observability.

The optimizer's authoritative audit data remains on
``tool_call.output_approval.channels["optimizer"]``.  Helpers here derive a
compact public metadata shape for final tool messages and UI rendering; they do
not affect raw output capture or optimizer decisions.
"""

import math
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


def optimizer_public_metadata_from_output_approval(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return compact public optimizer metadata for a tool message.

    ``None`` means no successful optimizer publication should be displayed.
    The input is a ``tool_call.output_approval`` payload.  Only successful,
    published optimizer use is surfaced; fallbacks/default output approvals stay
    quiet to avoid UI clutter.
    """

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


__all__ = [
    "format_output_optimizer_summary",
    "optimizer_public_metadata_from_output_approval",
]
