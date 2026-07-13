from __future__ import annotations

"""Exact line coordinates and presentation-only tool-output decoration."""

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class TextLineRange:
    start_line: int
    end_line: int
    total_lines: int
    text: str


def split_text_lines(text: str) -> list[str]:
    """Split text into logical lines while preserving every line ending.

    A trailing newline terminates the preceding line; it does not manufacture
    an additional empty line.  This matches the coordinate space users see in
    editors and keeps half-open extraction byte-exact after sanitization.
    """

    if not isinstance(text, str):
        raise TypeError("tool output must be text")
    return text.splitlines(keepends=True)


def extract_text_line_range(text: str, start_line: Any, end_line: Any) -> TextLineRange:
    """Return exact lines in the 1-based half-open range ``[start, end)``."""

    if isinstance(start_line, bool) or not isinstance(start_line, int):
        raise ValueError("start_line must be an integer starting at 1")
    if isinstance(end_line, bool) or not isinstance(end_line, int):
        raise ValueError("end_line must be an integer greater than start_line")
    if start_line < 1:
        raise ValueError("start_line must be an integer starting at 1")
    if end_line <= start_line:
        raise ValueError("end_line is exclusive and must be greater than start_line")

    lines = split_text_lines(text)
    total = len(lines)
    if start_line > total:
        raise ValueError(f"start_line {start_line} is out of range; source has {total} lines")
    if end_line > total + 1:
        raise ValueError(
            f"end_line {end_line} is out of range; maximum exclusive end is {total + 1}"
        )
    selected = "".join(lines[start_line - 1 : end_line - 1])
    if selected == "":
        raise ValueError("selected line range is empty")
    return TextLineRange(start_line, end_line, total, selected)


def number_text_lines(text: str, *, start_line: int = 1) -> str:
    """Prefix logical lines without changing their content or line endings."""

    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        raise ValueError("start_line must be an integer starting at 1")
    lines = split_text_lines(text)
    return "".join(f"{start_line + index}: {line}" for index, line in enumerate(lines))


def line_number_presentation(*, start_line: int = 1, body_offset: int = 0) -> dict[str, Any]:
    """Build small durable metadata for publication-time line decoration."""

    return {
        "kind": "line_numbers",
        "start_line": int(start_line),
        "body_offset": int(body_offset),
    }


def normalize_publication_presentation(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, Mapping) else {}
    if data.get("kind") != "line_numbers":
        return {}
    try:
        start_line = int(data.get("start_line", 1))
        body_offset = int(data.get("body_offset", 0))
    except (TypeError, ValueError):
        return {}
    if start_line < 1 or body_offset < 0:
        return {}
    return line_number_presentation(start_line=start_line, body_offset=body_offset)


def apply_output_presentation(text: str, presentation: Any) -> str:
    """Render presentation metadata over canonical text without mutating it."""

    normalized = normalize_publication_presentation(presentation)
    if not normalized:
        return text
    body_offset = min(normalized["body_offset"], len(text))
    return text[:body_offset] + number_text_lines(
        text[body_offset:], start_line=normalized["start_line"]
    )


def presentation_requires_exact_text(presentation: Any) -> bool:
    """Whether lossy semantic optimization would invalidate presentation."""

    return bool(normalize_publication_presentation(presentation))


__all__ = [
    "TextLineRange",
    "apply_output_presentation",
    "extract_text_line_range",
    "line_number_presentation",
    "normalize_publication_presentation",
    "number_text_lines",
    "presentation_requires_exact_text",
    "split_text_lines",
]
