from __future__ import annotations

"""Generic extraction of approved canonical textual tool output."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

from ..attachment_staging import safe_display_filename
from ..plugins import PluginContext
from ..provider_output_artifacts import save_provider_output_bytes
from ..terminal_safety import sanitize_terminal_text
from ..tool_output_presentation import extract_text_line_range
from ..tools import ToolContext, ToolRegistry


EXTRACT_TOOL_OUTPUT_NAME = "extract_tool_output"


class ToolOutputExtractionError(ValueError):
    """A requested source or line interval is not extractable."""


def _error(message: str) -> str:
    safe = sanitize_terminal_text(str(message or "tool output extraction failed"))
    if len(safe) > 500:
        safe = safe[:497] + "..."
    return f"Error: {safe}"


def _receipt_field(value: Any, *, max_chars: int = 160) -> str:
    """Render one untrusted receipt field safely and within a fixed bound."""

    text = sanitize_terminal_text(str(value or "unknown"))
    text = " ".join(text.split()) or "unknown"
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _context_db(ctx: ToolContext):
    try:
        from .compaction import _context_db as context_db

        return context_db(ctx)
    except Exception:
        from ..db import ThreadsDB

        db_path = getattr(ctx.db, "path", None)
        return ThreadsDB(db_path) if db_path is not None else ThreadsDB()


def _workspace_for_db(db: Any) -> Path:
    from ..attachment_tools import artifact_workspace_from_db

    return artifact_workspace_from_db(db)


def _event_payload(raw: Any) -> dict[str, Any]:
    try:
        payload_json = raw["payload_json"]
    except Exception:
        payload_json = None
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _visible_published_tool_messages(db: Any, thread_id: str, *, before_event_seq: int) -> list[tuple[int, str]]:
    """Return effective prior tool publications in authoritative event order."""

    from ..projection import load_thread_projection

    projection = load_thread_projection(db, thread_id, db.max_event_seq(thread_id))
    visible: list[tuple[int, str]] = []
    for message in projection.message_states:
        if not message.is_effective or message.created_event_seq >= before_event_seq:
            continue
        payload = message.payload if isinstance(message.payload, Mapping) else {}
        tool_call_id = payload.get("tool_call_id")
        if (
            payload.get("role") == "tool"
            and not payload.get("no_api")
            and isinstance(tool_call_id, str)
            and tool_call_id
        ):
            visible.append((message.created_event_seq, tool_call_id))
    visible.sort(key=lambda item: item[0])
    return visible


def _canonical_finished_text(db: Any, thread_id: str, tool_call_id: str) -> str:
    row = db.conn.execute(
        """
        SELECT payload_json FROM events
        WHERE thread_id=?
          AND type='tool_call.finished'
          AND json_extract(payload_json, '$.tool_call_id')=?
        ORDER BY event_seq DESC
        LIMIT 1
        """,
        (thread_id, tool_call_id),
    ).fetchone()
    if row is None:
        raise ToolOutputExtractionError("source tool call has no completed textual output")
    payload = _event_payload(row)
    output = payload.get("output")
    if not isinstance(output, str):
        raise ToolOutputExtractionError("source tool output is non-text/binary and cannot be extracted")
    return sanitize_terminal_text(output)


def _optional_nonnegative_int(value: Any, *, name: str) -> int | None:
    """Normalize an optional zero-based selector without accepting booleans."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolOutputExtractionError(f"{name} must be a non-negative integer")
    return value


def _source_from_group_position(
    states: Mapping[str, Any],
    current: Any,
    *,
    group_offset: int,
    call_index: int,
):
    """Resolve an LLM-authored prior assistant call by group and list index."""

    groups: dict[int, list[Any]] = {}
    for candidate in states.values():
        if (
            candidate.parent_role == "assistant"
            and candidate.parent_event_seq < current.parent_event_seq
        ):
            groups.setdefault(candidate.parent_event_seq, []).append(candidate)

    ordered_group_seqs = sorted(groups, reverse=True)
    if group_offset >= len(ordered_group_seqs):
        if ordered_group_seqs:
            available = f"0–{len(ordered_group_seqs) - 1}"
        else:
            available = "none"
        raise ToolOutputExtractionError(
            f"source_tool_call_group_offset {group_offset} is out of range; "
            f"available prior assistant tool-call group offsets: {available}"
        )

    group = sorted(groups[ordered_group_seqs[group_offset]], key=lambda item: item.index)
    by_index = {candidate.index: candidate for candidate in group}
    source = by_index.get(call_index)
    if source is None:
        available = ", ".join(str(candidate.index) for candidate in group) or "none"
        raise ToolOutputExtractionError(
            f"source_tool_call_index {call_index} is out of range for prior group offset "
            f"{group_offset}; available declaration indices: {available}"
        )
    return source


def _resolve_source_tool_call(
    db: Any,
    thread_id: str,
    current_tool_call_id: str,
    explicit_source_tool_call_id: Any,
    source_tool_call_index: Any,
    source_tool_call_group_offset: Any,
):
    from ..tool_state import build_tool_call_states

    states = build_tool_call_states(db, thread_id)
    current = states.get(current_tool_call_id)
    if current is None:
        raise ToolOutputExtractionError("current extraction tool call is not present in authoritative thread state")

    visible = _visible_published_tool_messages(
        db,
        thread_id,
        before_event_seq=current.parent_event_seq,
    )
    visible_ids = {tool_call_id for _seq, tool_call_id in visible}

    # Treat an empty ID exactly like omission. This is friendlier to generated
    # tool calls and prevents an opaque protocol ID from becoming mandatory.
    if explicit_source_tool_call_id is None:
        source_id = None
    elif isinstance(explicit_source_tool_call_id, str):
        source_id = explicit_source_tool_call_id.strip() or None
    else:
        raise ToolOutputExtractionError("source_tool_call_id must be a string when provided")
    call_index = _optional_nonnegative_int(
        source_tool_call_index,
        name="source_tool_call_index",
    )
    group_offset = _optional_nonnegative_int(
        source_tool_call_group_offset,
        name="source_tool_call_group_offset",
    )
    if source_id is not None and (call_index is not None or group_offset is not None):
        raise ToolOutputExtractionError(
            "source_tool_call_id cannot be combined with positional source selectors"
        )
    if group_offset is not None and call_index is None:
        raise ToolOutputExtractionError(
            "source_tool_call_group_offset requires source_tool_call_index"
        )

    if source_id is not None:
        if source_id == current_tool_call_id:
            raise ToolOutputExtractionError("the current extraction call cannot select itself")
        source = states.get(source_id)
        if source is not None and source.parent_event_seq == current.parent_event_seq:
            raise ToolOutputExtractionError(
                "source_tool_call_id is from the same parallel tool-call declaration; select a prior published call"
            )
        if source_id not in visible_ids:
            raise ToolOutputExtractionError(
                "source_tool_call_id must identify an authorized prior visible published tool call in the current thread"
            )
    elif call_index is not None:
        source = _source_from_group_position(
            states,
            current,
            group_offset=group_offset or 0,
            call_index=call_index,
        )
        source_id = source.tool_call_id
    else:
        eligible = [
            (event_seq, candidate_id)
            for event_seq, candidate_id in visible
            if candidate_id in states
            and states[candidate_id].approval_decision == "granted"
            and states[candidate_id].published
            and states[candidate_id].state == "TC6"
            and states[candidate_id].output_decision in {"whole", "partial"}
            and bool(states[candidate_id].finished_reason)
        ]
        if not eligible:
            raise ToolOutputExtractionError("no preceding completed visible published tool output is available")
        latest_event_seq = eligible[-1][0]
        latest_ids = [
            tool_call_id
            for event_seq, tool_call_id in eligible
            if event_seq == latest_event_seq
        ]
        if len(latest_ids) != 1:
            raise ToolOutputExtractionError(
                "multiple eligible prior outputs share the latest persisted publication boundary; pass source_tool_call_id"
            )
        source_id = latest_ids[0]

    source = states.get(source_id)
    if source is None:
        raise ToolOutputExtractionError("source tool call is not present in authoritative thread state")
    if source_id not in visible_ids:
        raise ToolOutputExtractionError(
            "selected source must be an authorized prior visible published tool call in the current thread"
        )
    if source.approval_decision != "granted":
        raise ToolOutputExtractionError("source tool call was not approved for execution")
    if not source.published or source.state != "TC6":
        raise ToolOutputExtractionError("source tool output is pending or not published")
    if source.output_decision not in {"whole", "partial"}:
        raise ToolOutputExtractionError("source tool output was omitted or not approved for publication")
    if not source.finished_reason:
        raise ToolOutputExtractionError("source tool call is not completed")
    return source


def extract_tool_output_tool(args: Dict[str, Any], ctx: ToolContext) -> str:
    """Extract an exact half-open canonical line interval to a file artifact."""

    thread_id = str(ctx.thread_id or "").strip()
    current_tool_call_id = str(ctx.tool_call_id or "").strip()
    if not thread_id:
        return _error("extract_tool_output requires a calling thread")
    if not current_tool_call_id:
        return _error("extract_tool_output requires its runner-injected current tool_call_id")

    db = _context_db(ctx)
    try:
        source = _resolve_source_tool_call(
            db,
            thread_id,
            current_tool_call_id,
            args.get("source_tool_call_id"),
            args.get("source_tool_call_index"),
            args.get("source_tool_call_group_offset"),
        )
        canonical = _canonical_finished_text(db, thread_id, source.tool_call_id)
        selected = extract_text_line_range(canonical, args.get("start_line"), args.get("end_line"))

        filename_arg = args.get("filename")
        if filename_arg is None:
            filename = (
                f"{source.name or 'tool'}-{selected.start_line}-{selected.end_line - 1}.txt"
            )
        else:
            # Preserve an explicit safe basename exactly, including extensions
            # such as .py used by extract -> export -> inspect/run workflows.
            filename = safe_display_filename(filename_arg, default="tool-output.txt")

        data = selected.text.encode("utf-8")
        saved = save_provider_output_bytes(
            _workspace_for_db(db),
            thread_id,
            data,
            filename=filename,
            mime_type="text/plain; charset=utf-8",
            presentation="file",
            provenance={
                "kind": "tool_output_extraction",
                "source_tool_name": source.name,
                "source_tool_call_id": source.tool_call_id,
                "source_thread_id": thread_id,
                "owner_thread_id": thread_id,
                "start_line": selected.start_line,
                "end_line": selected.end_line,
                "line_range_semantics": "1-based half-open [start_line, end_line)",
            },
            derived={
                "source_total_lines": selected.total_lines,
                "selected_line_count": selected.end_line - selected.start_line,
                "text_encoding": "utf-8",
            },
        )
    except (ToolOutputExtractionError, TypeError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        # Do not reflect backend paths/provider details into a tool message.
        return _error(f"could not extract tool output ({type(exc).__name__})")

    source_name = _receipt_field(source.name)
    source_call_id = _receipt_field(source.tool_call_id)
    receipt_filename = _receipt_field(saved.metadata.get("filename"))
    return (
        f"Extracted {source_name} call {source_call_id} lines "
        f"{selected.start_line}–{selected.end_line - 1} "
        f"([{selected.start_line}, {selected.end_line})) to {receipt_filename} "
        f"as provider artifact {saved.artifact_id} ({saved.metadata['size_bytes']} bytes, "
        f"sha256 {saved.metadata['sha256']})."
    )


def register_tool_output_extraction_tool(registry: ToolRegistry) -> None:
    registry.register(
        name=EXTRACT_TOOL_OUTPUT_NAME,
        description=(
            "Extract exact canonical sanitized text lines from a prior approved, published tool call "
            "into a thread-owned file artifact. start_line is inclusive and end_line is exclusive. "
            "With no source selector (or a blank source_tool_call_id), use the latest eligible prior "
            "output. For recent non-last calls, select by zero-based prior group offset and declaration index."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Inclusive 1-based canonical source line.",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 2,
                    "description": "Exclusive 1-based canonical source line; must be greater than start_line.",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional safe display filename for the created provider-output artifact.",
                },
                "source_tool_call_id": {
                    "type": "string",
                    "description": "Advanced exact selector for an authorized prior visible tool call. Omit or pass blank for automatic latest-output selection; do not invent IDs.",
                },
                "source_tool_call_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based declaration index within the selected prior assistant tool-call group. Group offset defaults to 0.",
                },
                "source_tool_call_group_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Reverse-chronological zero-based prior assistant tool-call group offset: 0 is the immediately preceding group. Requires source_tool_call_index.",
                },
            },
            "required": ["start_line", "end_line"],
            "additionalProperties": False,
        },
        impl=extract_tool_output_tool,
        accepts_context=True,
    )


@dataclass(frozen=True)
class ToolOutputExtractionPlugin:
    name: str = "tool_output_extraction"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_tool_output_extraction_tool(context.tool_registry)


__all__ = [
    "EXTRACT_TOOL_OUTPUT_NAME",
    "ToolOutputExtractionError",
    "ToolOutputExtractionPlugin",
    "extract_tool_output_tool",
    "register_tool_output_extraction_tool",
]
