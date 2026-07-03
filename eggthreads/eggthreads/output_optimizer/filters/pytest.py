from __future__ import annotations

"""Conservative pytest failure-summary optimizer filter."""

from dataclasses import dataclass
import re
from typing import Any

from ..classify import normalize_command_name, simple_bash_command_invocation
from ..core import OptimizeDecision, OptimizeRequest, make_decision


PYTEST_TOOL_NAMES = frozenset({"pytest", "py.test"})
_PYTHON_COMMAND_NAMES = frozenset({"python", "python3", "pypy", "pypy3"})
_SUMMARY_PREFIXES = ("FAILED ", "ERROR ")
_FINAL_OUTCOME_WORDS = (" failed", " error", " errors")


def is_pytest_request(request: OptimizeRequest) -> bool:
    """Return True for high-confidence pytest requests."""

    tool_name = normalize_command_name(request.tool_name)
    if tool_name in PYTEST_TOOL_NAMES:
        return True
    if tool_name == "bash":
        try:
            script = request.tool_args.get("script")
        except Exception:
            script = None
        return _words_invoke_pytest(simple_bash_command_invocation(script))
    return False


def _words_invoke_pytest(words: tuple[str, ...]) -> bool:
    if not words:
        return False
    command = normalize_command_name(words[0])
    if command in PYTEST_TOOL_NAMES:
        return True
    if command in _PYTHON_COMMAND_NAMES and len(words) >= 3 and words[1] == "-m":
        return normalize_command_name(words[2]) in PYTEST_TOOL_NAMES
    return False


def _section_title(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) < 6 or stripped[0] != "=" or stripped[-1] != "=":
        return None
    title = stripped.strip("= ").strip()
    return title or None


def _is_block_heading(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 8 or not stripped.startswith("_") or not stripped.endswith("_"):
        return False
    return bool(stripped.strip("_ "))


def _block_heading_title(line: str) -> str:
    return line.strip().strip("_ ").strip()


def _extract_pytest_lines(output: str) -> tuple[tuple[str, ...], bool] | None:
    if not isinstance(output, str) or not output.strip():
        return None
    lines = tuple(output.splitlines())
    had_stdout_header = lines[:1] == ("--- STDOUT ---",)
    if had_stdout_header:
        lines = lines[1:]
    if not lines or any(line in {"--- STDOUT ---", "--- STDERR ---"} for line in lines):
        return None
    return lines, had_stdout_header


@dataclass(frozen=True)
class PytestSection:
    title: str
    lines: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class ParsedPytestFailureOutput:
    summary_lines: tuple[str, ...]
    sections: tuple[PytestSection, ...]
    final_outcome: str
    had_stdout_header: bool


def parse_pytest_failure_output(output: str) -> ParsedPytestFailureOutput | None:
    """Parse high-confidence pytest failure/error output."""

    extracted = _extract_pytest_lines(output)
    if extracted is None:
        return None
    lines, had_stdout_header = extracted
    titled_indices = [(idx, title) for idx, line in enumerate(lines) if (title := _section_title(line))]
    if not titled_indices:
        return None

    title_to_index = {title.lower(): idx for idx, title in titled_indices}
    if "short test summary info" not in title_to_index:
        return None
    has_failure_section = "failures" in title_to_index
    has_error_section = "errors" in title_to_index
    if not has_failure_section and not has_error_section:
        return None

    summary_start = title_to_index["short test summary info"]
    summary_end = _next_title_index(titled_indices, summary_start, len(lines))
    raw_summary_lines = tuple(line for line in lines[summary_start + 1 : summary_end] if line.startswith(_SUMMARY_PREFIXES))
    if not raw_summary_lines:
        return None

    final_outcome = _find_final_outcome_line(lines, summary_end)
    if final_outcome is None:
        return None

    sections: list[PytestSection] = []
    if has_failure_section:
        failures_start = title_to_index["failures"]
        failures_end = _next_title_index(titled_indices, failures_start, len(lines))
        sections.extend(_split_pytest_sections(lines[failures_start + 1 : failures_end], kind="FAILURE"))
    if has_error_section:
        errors_start = title_to_index["errors"]
        errors_end = _next_title_index(titled_indices, errors_start, len(lines))
        sections.extend(_split_pytest_sections(lines[errors_start + 1 : errors_end], kind="ERROR"))

    if not sections:
        return None
    return ParsedPytestFailureOutput(
        summary_lines=raw_summary_lines,
        sections=tuple(sections),
        final_outcome=final_outcome,
        had_stdout_header=had_stdout_header,
    )


def _next_title_index(titled_indices: list[tuple[int, str]], current_index: int, default: int) -> int:
    later = [idx for idx, _title in titled_indices if idx > current_index]
    return min(later) if later else default


def _find_final_outcome_line(lines: tuple[str, ...], start: int) -> str | None:
    for line in lines[start:]:
        title = _section_title(line)
        if title and " in " in title and any(word in title for word in _FINAL_OUTCOME_WORDS):
            return title
    return None


def _split_pytest_sections(lines: tuple[str, ...], *, kind: str) -> tuple[PytestSection, ...]:
    sections: list[PytestSection] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in lines:
        if _is_block_heading(line):
            if current_title is not None:
                sections.append(PytestSection(current_title, tuple(current_lines), kind))
            current_title = _block_heading_title(line)
            current_lines = []
            continue
        if current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections.append(PytestSection(current_title, tuple(current_lines), kind))
    return tuple(section for section in sections if section.title and any(line.strip() for line in section.lines))


@dataclass(frozen=True)
class PytestFailureSummaryFilter:
    """Summarize pytest failure/error output while preserving useful excerpts."""

    name: str = "pytest_failure_summary"
    max_summary_entries: int = 20
    max_sections: int = 5
    max_lines_per_section: int = 16
    section_head_lines: int = 8
    section_tail_lines: int = 6
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_pytest_request(request):
            return None
        parsed = parse_pytest_failure_output(request.output)
        if parsed is None:
            return None

        rendered, metadata = self._render(parsed)
        if rendered == request.output:
            return None
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="pytest_failures_summarized",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render(self, parsed: ParsedPytestFailureOutput) -> tuple[str, dict[str, Any]]:
        max_summary_entries = max(1, int(self.max_summary_entries))
        max_sections = max(1, int(self.max_sections))
        max_lines_per_section = max(1, int(self.max_lines_per_section))
        section_head_lines = max(0, int(self.section_head_lines))
        section_tail_lines = max(0, int(self.section_tail_lines))
        if section_head_lines + section_tail_lines > max_lines_per_section:
            overflow = section_head_lines + section_tail_lines - max_lines_per_section
            reduce_tail = min(section_tail_lines, overflow)
            section_tail_lines -= reduce_tail
            overflow -= reduce_tail
            section_head_lines = max(0, section_head_lines - overflow)
        if section_head_lines + section_tail_lines <= 0:
            section_tail_lines = 1

        output_lines = ["Pytest failure summary:"]
        emitted_summary = min(len(parsed.summary_lines), max_summary_entries)
        for line in parsed.summary_lines[:emitted_summary]:
            output_lines.append(f"  {line}")
        omitted_summary = len(parsed.summary_lines) - emitted_summary
        if omitted_summary > 0:
            output_lines.append(f"  [... omitted {omitted_summary} more summary entries ...]")
        output_lines.append(f"  Final: {parsed.final_outcome}")

        emitted_sections = min(len(parsed.sections), max_sections)
        omitted_sections = len(parsed.sections) - emitted_sections
        omitted_section_lines = 0
        output_lines.append("")
        output_lines.append("Failure/error excerpts:")
        for section in parsed.sections[:emitted_sections]:
            output_lines.append(f"[{section.kind}] {section.title}")
            excerpt, omitted_lines = _section_excerpt(
                section.lines,
                max_lines=max_lines_per_section,
                head_lines=section_head_lines,
                tail_lines=section_tail_lines,
            )
            output_lines.extend(f"  {line}" if line else "" for line in excerpt)
            omitted_section_lines += omitted_lines
            output_lines.append("")
        if output_lines and output_lines[-1] == "":
            output_lines.pop()
        if omitted_sections > 0:
            remaining_sections = parsed.sections[emitted_sections:]
            remaining_lines = sum(len(section.lines) for section in remaining_sections)
            title_preview = ", ".join(section.title for section in remaining_sections[:5])
            if len(remaining_sections) > 5:
                title_preview += ", ..."
            output_lines.append(
                f"[... omitted {omitted_sections} more pytest failure/error sections"
                f" ({title_preview}) / {remaining_lines} lines ...]"
            )
            omitted_section_lines += remaining_lines

        metadata: dict[str, Any] = {
            "summary_count": len(parsed.summary_lines),
            "section_count": len(parsed.sections),
            "emitted_summary_entries": emitted_summary,
            "emitted_sections": emitted_sections,
            "omitted_summary_entries": omitted_summary,
            "omitted_sections": omitted_sections,
            "omitted_section_lines": omitted_section_lines,
            "final_outcome": parsed.final_outcome,
            "original_had_stdout_header": parsed.had_stdout_header,
            "max_summary_entries": max_summary_entries,
            "max_sections": max_sections,
            "max_lines_per_section": max_lines_per_section,
        }
        return "\n".join(output_lines), metadata


def _section_excerpt(lines: tuple[str, ...], *, max_lines: int, head_lines: int, tail_lines: int) -> tuple[tuple[str, ...], int]:
    if len(lines) <= max_lines:
        return lines, 0
    head_count = min(head_lines, len(lines))
    tail_count = min(tail_lines, max(0, len(lines) - head_count))
    if head_count + tail_count <= 0:
        tail_count = min(1, len(lines))
    omitted = len(lines) - head_count - tail_count
    tail = lines[len(lines) - tail_count :] if tail_count else ()
    return (*lines[:head_count], f"[... omitted {omitted} lines from this pytest section ...]", *tail), omitted


__all__ = [
    "ParsedPytestFailureOutput",
    "PytestFailureSummaryFilter",
    "PytestSection",
    "is_pytest_request",
    "parse_pytest_failure_output",
]
