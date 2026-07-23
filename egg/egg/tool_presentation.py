"""Shared terminal presentation helpers for tool calls and results."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from rich.text import Text


MEDIUM_TOOL_PREVIEW_MAX_LINES = 20
MEDIUM_TOOL_PREVIEW_HEAD_LINES = 12
MEDIUM_TOOL_PREVIEW_TAIL_LINES = 8
MEDIUM_TOOL_PREVIEW_MAX_CHARS = 4096

_BLOCK_ARGUMENT_NAMES = frozenset({
    "code",
    "content",
    "message",
    "prompt",
    "query",
    "script",
})


@dataclass(frozen=True)
class ToolCallPresentation:
    """Canonical display fields extracted from one provider tool call."""

    name: str
    arguments: Any
    tool_call_id: str


@dataclass(frozen=True)
class MediumToolStyles:
    """Theme-resolved semantic styles for medium tool presentation."""

    call: str
    call_name: str
    argument_key: str
    argument_value: str
    result: str
    muted: str
    command: str


def tool_call_presentation(tool_call: Any) -> ToolCallPresentation:
    """Extract a tool name, arguments, and exact call identity."""

    data = tool_call if isinstance(tool_call, dict) else {}
    function = data.get("function") if isinstance(data.get("function"), dict) else {}
    name = function.get("name") or data.get("name") or "function"
    arguments = function.get("arguments") if "arguments" in function else data.get("arguments")
    tool_call_id = data.get("id") or data.get("tool_call_id") or ""
    return ToolCallPresentation(
        name=str(name or "function"),
        arguments=arguments,
        tool_call_id=str(tool_call_id or ""),
    )


def _json_scalar(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _indented(text: str, spaces: int) -> str:
    prefix = " " * max(0, int(spaces))
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.split("\n"))


def _decode_arguments(arguments: Any) -> tuple[Any, bool]:
    if not isinstance(arguments, str):
        return arguments, True
    stripped = arguments.strip()
    if not stripped:
        return None, True
    try:
        return json.loads(stripped), True
    except Exception:
        return arguments, False


def format_tool_arguments(arguments: Any) -> str:
    """Format tool arguments as readable fields rather than flattened JSON."""

    decoded, structured = _decode_arguments(arguments)
    if decoded is None or decoded == {} or decoded == []:
        return "(no arguments)"

    if structured and isinstance(decoded, dict):
        lines: list[str] = []
        for raw_key, value in decoded.items():
            key = str(raw_key)
            if isinstance(value, str):
                if key.lower() in _BLOCK_ARGUMENT_NAMES or "\n" in value or len(value) > 96:
                    lines.append(f"{key}:\n{_indented(value, 2)}")
                else:
                    lines.append(f"{key}: {value}")
            elif isinstance(value, (dict, list)):
                try:
                    nested = json.dumps(value, ensure_ascii=False, indent=2)
                except Exception:
                    nested = str(value)
                lines.append(f"{key}:\n{_indented(nested, 2)}")
            else:
                lines.append(f"{key}: {_json_scalar(value)}")
        return "\n".join(lines) if lines else "(no arguments)"

    if structured and isinstance(decoded, (list, tuple)):
        try:
            return json.dumps(decoded, ensure_ascii=False, indent=2)
        except Exception:
            return str(decoded)

    if structured and not isinstance(decoded, str):
        return _json_scalar(decoded)

    raw = str(decoded or "")
    return f"arguments:\n{_indented(raw, 2)}" if raw else "(no arguments)"


def _line_end_offsets(text: str) -> list[int]:
    """Return exclusive offsets after every logical line in *text*."""

    offsets: list[int] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        cursor += len(line)
        offsets.append(cursor)
    if not offsets or offsets[-1] < len(text):
        offsets.append(len(text))
    return offsets


def _line_start_offsets(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n" and index + 1 < len(text):
            starts.append(index + 1)
    return starts


def _omission_marker(lines: int, chars: int) -> str:
    line_label = "line" if int(lines) == 1 else "lines"
    return f"… omitted {int(lines)} {line_label} / {int(chars)} chars …"


def _truncate_preview_chars(
    text: str,
    *,
    max_chars: int,
    footer: str,
    line_count: int,
) -> str:
    """Fallback char bounding that always preserves the marker and footer."""

    content_budget = max(2, max_chars - len(footer) - 48)
    while True:
        head_size = max(1, int(content_budget * 0.6))
        tail_size = max(1, content_budget - head_size)
        if head_size + tail_size >= len(text):
            head_size = max(1, len(text) // 2)
            tail_size = max(1, len(text) - head_size - 1)
        prefix = text[:head_size].rstrip("\n")
        suffix = text[len(text) - tail_size:].lstrip("\n")
        omitted = text[len(prefix):len(text) - len(suffix)]
        omitted_lines = max(
            1,
            line_count - len(prefix.splitlines()) - len(suffix.splitlines()),
        )
        marker = _omission_marker(omitted_lines, len(omitted))
        preview = "\n".join(part for part in (prefix, marker, suffix) if part) + footer
        if len(preview) <= max_chars or content_budget <= 2:
            return preview
        content_budget = max(2, content_budget - (len(preview) - max_chars))


def bounded_medium_preview(
    value: Any,
    *,
    empty_text: str,
    inspect_message_id: str = "",
    max_lines: int = MEDIUM_TOOL_PREVIEW_MAX_LINES,
    head_lines: int = MEDIUM_TOOL_PREVIEW_HEAD_LINES,
    tail_lines: int = MEDIUM_TOOL_PREVIEW_TAIL_LINES,
    max_chars: int = MEDIUM_TOOL_PREVIEW_MAX_CHARS,
) -> str:
    """Return a bounded head-and-tail preview with truthful omission metadata."""

    max_lines = max(1, int(max_lines))
    # A truthful omission marker plus an optional /show footer has a real
    # minimum size. Clamp to that rather than silently cutting either one.
    min_chars = 64 + len(str(inspect_message_id or ""))
    max_chars = max(min_chars, int(max_chars))
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return empty_text

    line_count = len(text.splitlines()) or 1
    if line_count <= max_lines and len(text) <= max_chars:
        return text

    head_lines = max(1, min(int(head_lines), max(1, int(max_lines))))
    tail_lines = max(1, min(int(tail_lines), max(1, int(max_lines))))
    end_offsets = _line_end_offsets(text)
    start_offsets = _line_start_offsets(text)

    prefix_end = end_offsets[min(head_lines, len(end_offsets)) - 1]
    suffix_index = max(0, len(start_offsets) - tail_lines)
    suffix_start = start_offsets[suffix_index]

    # If only the character bound is exceeded, select character head/tail even
    # when the configured line windows overlap the entire value.
    reserve = min(256, max(32, max_chars // 8))
    content_budget = max(2, max_chars - reserve)
    head_budget = max(1, int(content_budget * 0.6))
    tail_budget = max(1, content_budget - head_budget)
    prefix_end = min(prefix_end, head_budget)
    suffix_start = max(suffix_start, len(text) - tail_budget)

    if prefix_end >= suffix_start:
        prefix_end = min(head_budget, max(1, len(text) // 2))
        suffix_start = max(prefix_end + 1, len(text) - tail_budget)
    suffix_start = min(len(text), suffix_start)

    prefix = text[:prefix_end].rstrip("\n")
    suffix = text[suffix_start:].lstrip("\n")
    message_id = str(inspect_message_id or "").strip()
    footer = f"\n\nInspect complete persisted record: /show {message_id}" if message_id else ""

    def build_preview(
        current_prefix: str,
        current_suffix: str,
        current_prefix_end: int,
        current_suffix_start: int,
    ) -> str:
        current_omitted = text[current_prefix_end:current_suffix_start]
        omitted_lines = max(
            1,
            line_count - len(current_prefix.splitlines()) - len(current_suffix.splitlines()),
        )
        marker = _omission_marker(omitted_lines, len(current_omitted))
        return "\n".join(part for part in (current_prefix, marker, current_suffix) if part) + footer

    preview = build_preview(prefix, suffix, prefix_end, suffix_start)
    if len(preview) > max_chars:
        preview = _truncate_preview_chars(
            text,
            max_chars=max_chars,
            footer=footer,
            line_count=line_count,
        )
    return preview


def format_medium_tool_arguments(arguments: Any, *, inspect_message_id: str = "") -> str:
    return bounded_medium_preview(
        format_tool_arguments(arguments),
        empty_text="(no arguments)",
        inspect_message_id=inspect_message_id,
    )


def format_medium_tool_calls(tool_calls: Iterable[Any], *, inspect_message_id: str = "") -> str:
    """Format one assistant tool-call group with stable exact identities."""

    blocks: list[str] = []
    for index, raw_call in enumerate(tool_calls, start=1):
        call = tool_call_presentation(raw_call)
        identity = f" · tool_call_id: {call.tool_call_id}" if call.tool_call_id else ""
        arguments = format_medium_tool_arguments(
            call.arguments,
        )
        blocks.append(f"{index}. {call.name}{identity}\n{_indented(arguments, 3)}")
    rendered = "\n\n".join(blocks)
    message_id = str(inspect_message_id or "").strip()
    if message_id and any("… omitted " in block for block in blocks):
        rendered += f"\n\nInspect complete persisted record: /show {message_id}"
    return rendered


def tool_result_recovery_hint(message: Any) -> str:
    """Return the persisted raw-output recovery command, when available."""

    data = message if isinstance(message, dict) else {}
    metadata = data.get("output_optimizer") if isinstance(data.get("output_optimizer"), dict) else {}
    hint = str(metadata.get("raw_hint") or "").strip()
    if hint:
        return hint
    artifact_id = str(metadata.get("artifact_id") or "").strip()
    if artifact_id:
        return f"read_long_tool_output('{artifact_id}', chunk_number=1)"
    return ""


def format_medium_tool_result(
    content: Any,
    *,
    inspect_message_id: str = "",
    recovery_hint: str = "",
) -> str:
    """Show short results completely and long results as bounded head/tail."""

    hint = str(recovery_hint or "").strip()
    footer_parts: list[str] = []
    if hint and hint not in str(content or ""):
        footer_parts.append(f"Raw output: {hint}")
    footer = "\n\n".join(footer_parts)

    # Keep the result body and recovery directions within the same bound. A raw
    # artifact command is more useful than an extra tail line, so reserve its
    # exact size before selecting the body preview.
    preview_budget = MEDIUM_TOOL_PREVIEW_MAX_CHARS
    if footer:
        preview_budget = max(256, preview_budget - len(footer) - 2)
    preview = bounded_medium_preview(
        content,
        empty_text="(no output)",
        inspect_message_id=str(inspect_message_id or "").strip(),
        max_chars=preview_budget,
    )
    if footer:
        preview += f"\n\n{footer}"
    return preview


def format_medium_streamed_tool_result(content: Any, *, inspect_message_id: str = "") -> str:
    """Format legacy assistant-attached stream previews without false empties."""

    return bounded_medium_preview(
        content,
        empty_text="(no streamed output)",
        inspect_message_id=inspect_message_id,
    )


def _append_line(text: Text, line: str, style: str) -> None:
    if text:
        text.append("\n")
    text.append(line, style=style)


def _styled_tool_arguments_rendered(rendered: str, styles: MediumToolStyles) -> Text:
    """Color semantic labels while preserving argument values literally."""

    text = Text()
    continuation_style = styles.argument_value
    for raw_line in rendered.splitlines():
        stripped = raw_line.lstrip(" ")
        indent = raw_line[:len(raw_line) - len(stripped)]
        if stripped in {"(no arguments)", "(no output)", "(no streamed output)"}:
            _append_line(text, raw_line, styles.muted)
            continuation_style = styles.muted
            continue
        if stripped.startswith("… omitted ") or stripped.startswith("Inspect complete persisted record:"):
            _append_line(text, raw_line, styles.muted)
            continuation_style = styles.muted
            continue
        key, separator, remainder = stripped.partition(":")
        is_labeled_argument = (
            len(indent) == 0
            and bool(separator and key)
            and not key.startswith(('{', '[', '"'))
            and not key.startswith(("http", "https", "/"))
            and " " not in key
            and len(key) <= 80
        )
        if is_labeled_argument:
            if text:
                text.append("\n")
            text.append(indent)
            text.append(key, style=styles.argument_key)
            text.append(":", style=styles.muted)
            if remainder:
                text.append(remainder, style=styles.argument_value)
            continuation_style = styles.argument_value
            continue
        _append_line(text, raw_line, continuation_style)
    return text


def _tool_argument_text(value: Any, styles: MediumToolStyles) -> Text:
    return _styled_tool_arguments_rendered(format_medium_tool_arguments(value), styles)


def medium_tool_calls_text(
    tool_calls: Iterable[Any],
    *,
    styles: MediumToolStyles,
    inspect_message_id: str = "",
) -> Text:
    """Return theme-aware Rich text for a grouped medium tool declaration."""

    raw_calls = list(tool_calls)
    text = Text()
    any_bounded = False
    for index, raw_call in enumerate(raw_calls, start=1):
        if text:
            text.append("\n\n")
        call = tool_call_presentation(raw_call)
        text.append(f"{index}.", style=styles.call)
        text.append(" ")
        text.append(call.name, style=styles.call_name)
        if call.tool_call_id:
            text.append(" · ", style=styles.muted)
            text.append("tool_call_id:", style=styles.muted)
            text.append(f" {call.tool_call_id}", style=styles.muted)
        arguments = _tool_argument_text(call.arguments, styles)
        if "… omitted " in arguments.plain:
            any_bounded = True
        text.append("\n")
        for line_index, line in enumerate(arguments.split("\n")):
            if line_index:
                text.append("\n")
            text.append("   ")
            text.append_text(line)
    message_id = str(inspect_message_id or "").strip()
    if message_id and any_bounded:
        text.append("\n\n")
        text.append("Inspect complete persisted record:", style=styles.muted)
        text.append(f" /show {message_id}", style=styles.command)
    return text


def medium_tool_arguments_text(
    arguments: Any,
    *,
    styles: MediumToolStyles,
    inspect_message_id: str = "",
) -> Text:
    rendered = format_medium_tool_arguments(
        arguments,
        inspect_message_id=inspect_message_id,
    )
    return _styled_tool_arguments_rendered(rendered, styles)


def medium_tool_result_text(
    content: Any,
    *,
    styles: MediumToolStyles,
    inspect_message_id: str = "",
    recovery_hint: str = "",
    streamed: bool = False,
) -> Text:
    """Return literal result content with semantic metadata styling only."""

    if streamed:
        rendered = format_medium_streamed_tool_result(
            content,
            inspect_message_id=inspect_message_id,
        )
    else:
        rendered = format_medium_tool_result(
            content,
            inspect_message_id=inspect_message_id,
            recovery_hint=recovery_hint,
        )
    text = Text()
    for raw_line in rendered.splitlines():
        if raw_line.startswith("… omitted "):
            _append_line(text, raw_line, styles.muted)
        elif raw_line.startswith("Inspect complete persisted record:"):
            prefix, _separator, command = raw_line.partition(":")
            if text:
                text.append("\n")
            text.append(f"{prefix}:", style=styles.muted)
            text.append(command, style=styles.command)
        elif raw_line.startswith("Raw output:"):
            command = raw_line[len("Raw output:"):]
            if text:
                text.append("\n")
            text.append("Raw output:", style=styles.muted)
            text.append(command, style=styles.command)
        elif raw_line in {"(no output)", "(no streamed output)"}:
            _append_line(text, raw_line, styles.muted)
        else:
            _append_line(text, raw_line, styles.result)
    return text


__all__ = [
    "MEDIUM_TOOL_PREVIEW_HEAD_LINES",
    "MEDIUM_TOOL_PREVIEW_MAX_CHARS",
    "MEDIUM_TOOL_PREVIEW_MAX_LINES",
    "MEDIUM_TOOL_PREVIEW_TAIL_LINES",
    "MediumToolStyles",
    "ToolCallPresentation",
    "bounded_medium_preview",
    "format_medium_streamed_tool_result",
    "format_medium_tool_arguments",
    "format_medium_tool_calls",
    "format_medium_tool_result",
    "format_tool_arguments",
    "medium_tool_arguments_text",
    "medium_tool_calls_text",
    "medium_tool_result_text",
    "tool_call_presentation",
    "tool_result_recovery_hint",
]
