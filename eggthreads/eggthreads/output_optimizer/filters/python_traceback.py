from __future__ import annotations

"""Conservative Python traceback focusing filter."""

from dataclasses import dataclass
import re
from typing import Any

from ..core import OptimizeDecision, OptimizeRequest, make_decision


_TRACEBACK_HEADER = "Traceback (most recent call last):"
_FRAME_RE = re.compile(r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
_EXCEPTION_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*(?::\s?.*)?$")


@dataclass(frozen=True)
class ParsedTraceback:
    prefix_lines: tuple[str, ...]
    header: str
    frames: tuple[tuple[str, ...], ...]
    exception_tail: tuple[str, ...]
    exception_line: str
    header_index: int


def parse_python_traceback(output: str) -> ParsedTraceback | None:
    """Parse a single high-confidence Python traceback block."""

    if not isinstance(output, str) or _TRACEBACK_HEADER not in output:
        return None
    lines = tuple(output.splitlines())
    header_indices = [idx for idx, line in enumerate(lines) if line == _TRACEBACK_HEADER]
    if len(header_indices) != 1:
        return None
    header_index = header_indices[0]
    if header_index >= len(lines) - 2:
        return None

    block = lines[header_index:]
    frame_starts = [idx for idx, line in enumerate(block) if _FRAME_RE.match(line)]
    if not frame_starts:
        return None

    exception_idx = _last_exception_line_index(block)
    if exception_idx is None or exception_idx <= frame_starts[-1]:
        return None
    if any(idx >= exception_idx for idx in frame_starts):
        return None

    frames: list[tuple[str, ...]] = []
    for pos, start in enumerate(frame_starts):
        next_start = frame_starts[pos + 1] if pos + 1 < len(frame_starts) else exception_idx
        if next_start <= start:
            return None
        frames.append(tuple(block[start:next_start]))

    exception_tail = tuple(block[exception_idx:])
    exception_line = block[exception_idx]
    if not frames or not exception_tail:
        return None
    return ParsedTraceback(
        prefix_lines=tuple(lines[:header_index]),
        header=block[0],
        frames=tuple(frames),
        exception_tail=exception_tail,
        exception_line=exception_line,
        header_index=header_index,
    )


def _last_exception_line_index(block: tuple[str, ...]) -> int | None:
    for idx in range(len(block) - 1, 0, -1):
        line = block[idx]
        if not line.strip():
            continue
        if line == line.strip() and _EXCEPTION_RE.match(line):
            return idx
        return None
    return None


@dataclass(frozen=True)
class PythonTracebackFocusFilter:
    """Keep the top and bottom frames of long Python tracebacks."""

    name: str = "python_traceback_focus"
    max_frames: int = 6
    head_frames: int = 2
    tail_frames: int = 3
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        parsed = parse_python_traceback(request.output)
        if parsed is None:
            return None

        frame_count = len(parsed.frames)
        max_frames = max(1, int(self.max_frames))
        if frame_count <= max_frames:
            return None

        head_count = min(max(0, int(self.head_frames)), frame_count)
        tail_count = min(max(0, int(self.tail_frames)), max(0, frame_count - head_count))
        if head_count + tail_count <= 0:
            tail_count = min(1, frame_count)
        if head_count + tail_count > max_frames:
            overflow = head_count + tail_count - max_frames
            reduce_tail = min(tail_count, overflow)
            tail_count -= reduce_tail
            overflow -= reduce_tail
            if overflow > 0:
                head_count = max(0, head_count - overflow)
        omitted_frames = frame_count - head_count - tail_count
        if omitted_frames <= 0:
            return None

        rendered_lines: list[str] = list(parsed.prefix_lines)
        rendered_lines.append(parsed.header)
        for frame in parsed.frames[:head_count]:
            rendered_lines.extend(frame)
        rendered_lines.append(f"  [... omitted {omitted_frames} middle traceback frames ...]")
        for frame in parsed.frames[frame_count - tail_count :] if tail_count else ():
            rendered_lines.extend(frame)
        rendered_lines.extend(parsed.exception_tail)
        rendered = "\n".join(rendered_lines)
        if rendered == request.output:
            return None

        metadata: dict[str, Any] = {
            "frame_count": frame_count,
            "emitted_frames": head_count + tail_count,
            "omitted_frames": omitted_frames,
            "head_frames": head_count,
            "tail_frames": tail_count,
            "max_frames": max_frames,
            "exception": parsed.exception_line,
            "traceback_start_line": parsed.header_index + 1,
            "original_had_stdout_header": parsed.prefix_lines[:1] == ("--- STDOUT ---",),
            "original_had_stderr_header": parsed.prefix_lines[-1:] == ("--- STDERR ---",),
        }
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="python_traceback_focused",
            confidence=self.confidence,
            metadata=metadata,
        )


__all__ = ["ParsedTraceback", "PythonTracebackFocusFilter", "parse_python_traceback"]
