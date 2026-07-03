from __future__ import annotations

"""Generic pure filters and helpers for Egg-native output optimization."""

from dataclasses import dataclass
import re
from typing import Any

from ..terminal_safety import sanitize_terminal_text
from .core import OptimizeDecision, OptimizeRequest, OutputFilter, OutputOptimizer, make_decision


def clean_ansi_controls(text: str) -> str:
    """Strip ANSI/terminal control sequences using Egg's shared sanitizer."""

    return sanitize_terminal_text(text)


_PROGRESS_SPINNER_CHARS = {"|", "/", "-", "\\", "◐", "◓", "◑", "◒"}
_PROGRESS_BAR_WITH_PERCENT_RE = re.compile(
    r"""
    ^\s*
    (?=.*(?:\d{1,3}%|\b\d+\s*/\s*\d+\b))
    (?:
        \d{1,3}%\s*[|[]
        |
        \[[#=\-><.\s█▓▒░]{3,}\]\s*\d{1,3}%
        |
        .*\|[#=\-><.\s█▓▒░]{3,}\|.*(?:it/s|B/s|/s|ETA|elapsed|remaining)
    )
    .*$
    """,
    re.VERBOSE,
)
_PROGRESS_BYTES_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*[KMGTP]?B\s*/\s*s|\d{1,3}%\s+of\s+\d+(?:\.\d+)?\s*[KMGTP]?B)\s*$",
    re.IGNORECASE,
)


def is_obvious_progress_noise_line(line: str) -> bool:
    """Return True for conservative progress-bar/spinner noise lines."""

    stripped = line.strip()
    if not stripped:
        return False
    if stripped in _PROGRESS_SPINNER_CHARS:
        return True
    if _PROGRESS_BAR_WITH_PERCENT_RE.match(stripped):
        return True
    if _PROGRESS_BYTES_RE.match(stripped):
        return True
    return False


def suppress_progress_noise(text: str) -> tuple[str, dict[str, Any]]:
    """Remove obvious standalone progress/spinner lines from *text*."""

    lines = text.splitlines()
    if not lines:
        return text, {"suppressed_progress_lines": 0}

    kept: list[str] = []
    suppressed = 0
    for line in lines:
        if is_obvious_progress_noise_line(line):
            suppressed += 1
        else:
            kept.append(line)

    if suppressed == 0:
        return text, {"suppressed_progress_lines": 0}

    optimized = "\n".join(kept)
    if text.endswith("\n") and optimized:
        optimized += "\n"
    return optimized, {"suppressed_progress_lines": suppressed}


def dedupe_repeated_lines(text: str, *, min_repeats: int = 3) -> tuple[str, dict[str, Any]]:
    """Collapse consecutive duplicate lines and report suppressed counts."""

    if min_repeats < 2:
        raise ValueError("min_repeats must be >= 2")

    lines = text.splitlines()
    if not lines:
        return text, {"dedupe_runs": 0, "dedupe_suppressed_lines": 0}

    out: list[str] = []
    dedupe_runs = 0
    suppressed_lines = 0

    def flush(line: str, count: int) -> None:
        nonlocal dedupe_runs, suppressed_lines
        if count >= min_repeats:
            repeated = count - 1
            out.append(line)
            out.append(f"[... repeated {repeated} more times ...]")
            dedupe_runs += 1
            suppressed_lines += repeated
            return
        out.extend(line for _ in range(count))

    current = lines[0]
    count = 1
    for line in lines[1:]:
        if line == current:
            count += 1
            continue
        flush(current, count)
        current = line
        count = 1
    flush(current, count)

    if dedupe_runs == 0:
        return text, {"dedupe_runs": 0, "dedupe_suppressed_lines": 0}

    optimized = "\n".join(out)
    if text.endswith("\n"):
        optimized += "\n"
    return optimized, {"dedupe_runs": dedupe_runs, "dedupe_suppressed_lines": suppressed_lines}


def bounded_head_tail(
    text: str,
    *,
    max_chars: int,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a bounded head/tail preview with an explicit omission note."""

    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    if head_chars is not None and head_chars < 0:
        raise ValueError("head_chars must be >= 0")
    if tail_chars is not None and tail_chars < 0:
        raise ValueError("tail_chars must be >= 0")

    raw_chars = len(text)
    if raw_chars <= max_chars:
        return text, {
            "bounded": False,
            "max_chars": max_chars,
            "omitted_chars": 0,
            "omitted_lines": 0,
            "head_chars": raw_chars,
            "tail_chars": 0,
        }

    if head_chars is None and tail_chars is None:
        head = max_chars // 2
        tail = max_chars - head
    elif head_chars is None:
        tail = min(int(tail_chars or 0), raw_chars)
        head = max_chars - tail
    elif tail_chars is None:
        head = min(int(head_chars), raw_chars)
        tail = max_chars - head
    else:
        head = min(int(head_chars), raw_chars)
        tail = min(int(tail_chars), max(0, raw_chars - head))

    head = max(0, head)
    tail = max(0, tail)

    def build_note(current_head: int, current_tail: int) -> tuple[str, int, int]:
        omit_start = min(current_head, raw_chars)
        omit_end = max(omit_start, raw_chars - current_tail)
        omitted_text = text[omit_start:omit_end]
        omitted_chars = len(omitted_text)
        omitted_lines = omitted_text.count("\n")
        note = f"\n[... omitted {omitted_chars} chars / {omitted_lines} lines from middle ...]\n"
        return note, omitted_chars, omitted_lines

    note, omitted_chars, omitted_lines = build_note(head, tail)
    while head + len(note) + tail > max_chars and (head > 0 or tail > 0):
        overflow = head + len(note) + tail - max_chars
        if head > tail and head > 0:
            reduction = min(head - tail if tail else head, overflow)
            head -= max(1, reduction)
        elif tail > head and tail > 0:
            reduction = min(tail - head if head else tail, overflow)
            tail -= max(1, reduction)
        elif head > 0:
            reduction = min(head, (overflow + 1) // 2)
            head -= max(1, reduction)
        elif tail > 0:
            reduction = min(tail, overflow)
            tail -= max(1, reduction)
        note, omitted_chars, omitted_lines = build_note(head, tail)

    if len(note) > max_chars:
        shortened = note[:max_chars]
        return shortened, {
            "bounded": True,
            "max_chars": max_chars,
            "omitted_chars": raw_chars,
            "omitted_lines": text.count("\n"),
            "head_chars": 0,
            "tail_chars": 0,
        }

    preview = text[:head] + note + (text[raw_chars - tail :] if tail else "")
    return preview, {
        "bounded": True,
        "max_chars": max_chars,
        "omitted_chars": omitted_chars,
        "omitted_lines": omitted_lines,
        "head_chars": head,
        "tail_chars": tail,
    }


@dataclass(frozen=True)
class AnsiControlCleanupFilter:
    name: str = "ansi_control_cleanup"
    confidence: float = 1.0

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        cleaned = clean_ansi_controls(request.output)
        if cleaned == request.output:
            return None
        return make_decision(
            request,
            cleaned,
            filter_name=self.name,
            reason="ansi_control_cleanup",
            confidence=self.confidence,
            metadata={"cleaned_controls": True},
        )


@dataclass(frozen=True)
class ProgressNoiseFilter:
    name: str = "progress_noise"
    confidence: float = 0.8

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        optimized, metadata = suppress_progress_noise(request.output)
        if optimized == request.output:
            return None
        return make_decision(
            request,
            optimized,
            filter_name=self.name,
            reason="progress_noise_suppressed",
            confidence=self.confidence,
            metadata=metadata,
        )


@dataclass(frozen=True)
class RepeatedLineDedupeFilter:
    name: str = "repeated_line_dedupe"
    min_repeats: int = 3
    confidence: float = 0.95

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        optimized, metadata = dedupe_repeated_lines(request.output, min_repeats=self.min_repeats)
        if optimized == request.output:
            return None
        return make_decision(
            request,
            optimized,
            filter_name=self.name,
            reason="repeated_lines_deduped",
            confidence=self.confidence,
            metadata=metadata,
        )


@dataclass(frozen=True)
class BoundedHeadTailFilter:
    name: str = "bounded_head_tail"
    max_chars: int = 20_000
    head_chars: int | None = None
    tail_chars: int | None = None
    confidence: float = 1.0

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        optimized, metadata = bounded_head_tail(
            request.output,
            max_chars=self.max_chars,
            head_chars=self.head_chars,
            tail_chars=self.tail_chars,
        )
        if optimized == request.output:
            return None
        return make_decision(
            request,
            optimized,
            filter_name=self.name,
            reason="bounded_head_tail_fallback",
            confidence=self.confidence,
            metadata=metadata,
        )


@dataclass(frozen=True)
class GenericOutputFilter:
    """Conservative helper filter that applies generic cleanup steps in order."""

    name: str = "generic"
    cleanup_controls: bool = True
    suppress_progress: bool = True
    dedupe_lines: bool = True
    dedupe_min_repeats: int = 3
    max_chars: int | None = None
    confidence: float = 0.8

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        current = request.output
        operations: list[str] = []
        metadata: dict[str, Any] = {}

        if self.cleanup_controls:
            cleaned = clean_ansi_controls(current)
            if cleaned != current:
                current = cleaned
                operations.append("ansi_control_cleanup")
                metadata["cleaned_controls"] = True

        if self.suppress_progress:
            current, progress_metadata = suppress_progress_noise(current)
            if progress_metadata.get("suppressed_progress_lines", 0):
                operations.append("progress_noise_suppressed")
                metadata.update(progress_metadata)

        if self.dedupe_lines:
            current, dedupe_metadata = dedupe_repeated_lines(current, min_repeats=self.dedupe_min_repeats)
            if dedupe_metadata.get("dedupe_runs", 0):
                operations.append("repeated_lines_deduped")
                metadata.update(dedupe_metadata)

        if self.max_chars is not None:
            current, bounded_metadata = bounded_head_tail(current, max_chars=self.max_chars)
            if bounded_metadata.get("bounded", False):
                operations.append("bounded_head_tail_fallback")
                metadata.update(bounded_metadata)

        if current == request.output:
            return None

        metadata["operations"] = tuple(operations)
        return make_decision(
            request,
            current,
            filter_name=self.name,
            reason="generic_output_optimized",
            confidence=self.confidence,
            metadata=metadata,
        )


def default_generic_filters(*, bounded_max_chars: int | None = None) -> list[OutputFilter]:
    """Return generic filters in conservative default order."""

    filters: list[OutputFilter] = [GenericOutputFilter(max_chars=bounded_max_chars)]
    return filters


def create_generic_output_optimizer(
    *,
    min_size_chars: int = 0,
    min_confidence: float = 0.5,
    bounded_max_chars: int | None = None,
) -> OutputOptimizer:
    """Create an optimizer containing only pure generic filters."""

    return OutputOptimizer(
        default_generic_filters(bounded_max_chars=bounded_max_chars),
        min_size_chars=min_size_chars,
        min_confidence=min_confidence,
    )


__all__ = [
    "AnsiControlCleanupFilter",
    "BoundedHeadTailFilter",
    "GenericOutputFilter",
    "ProgressNoiseFilter",
    "RepeatedLineDedupeFilter",
    "bounded_head_tail",
    "clean_ansi_controls",
    "create_generic_output_optimizer",
    "dedupe_repeated_lines",
    "default_generic_filters",
    "is_obvious_progress_noise_line",
    "suppress_progress_noise",
]
