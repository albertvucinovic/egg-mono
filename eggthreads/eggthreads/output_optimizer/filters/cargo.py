from __future__ import annotations

"""Conservative Cargo/Rust test failure-summary optimizer filter."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import shlex
from typing import Any

from ..classify import normalize_command_name, simple_bash_command_invocation
from ..core import OptimizeDecision, OptimizeRequest, make_decision


CARGO_TOOL_NAMES = frozenset({"cargo"})
_CARGO_OPTIONS_WITH_ARG = frozenset({"--manifest-path", "--config", "--target-dir", "--color"})
_FAILURE_HEADING_SUFFIXES = (" stdout", " stderr")
_OUTPUT_HEADERS = frozenset({"--- STDOUT ---", "--- STDERR ---"})


@dataclass(frozen=True)
class CargoFailureSection:
    name: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ParsedCargoTestFailureOutput:
    failure_names: tuple[str, ...]
    sections: tuple[CargoFailureSection, ...]
    final_summary: str
    trailing_error_lines: tuple[str, ...]
    output_header: str | None


def is_cargo_test_request(request: OptimizeRequest) -> bool:
    """Return True for high-confidence cargo-test requests."""

    tool_name = normalize_command_name(request.tool_name)
    if tool_name == "cargo":
        return _cargo_words_are_test(_cargo_tool_args_words(request.tool_args))
    if tool_name == "bash":
        try:
            script = request.tool_args.get("script")
        except Exception:
            script = None
        return _cargo_words_are_test(simple_bash_command_invocation(script))
    return False


def _cargo_tool_args_words(tool_args: Any) -> tuple[str, ...]:
    try:
        args_value = tool_args.get("args")
        if args_value is None:
            args_value = tool_args.get("argv")
        if args_value is None:
            args_value = tool_args.get("command")
        if args_value is None:
            args_value = tool_args.get("subcommand")
    except Exception:
        args_value = None

    if args_value is None:
        return ()
    if isinstance(args_value, str):
        try:
            words = tuple(shlex.split(args_value, comments=False, posix=True))
        except ValueError:
            return ()
    elif isinstance(args_value, Mapping):
        return ()
    elif isinstance(args_value, Iterable):
        words = tuple(str(item) for item in args_value)
    else:
        return ()
    if words and normalize_command_name(words[0]) == "cargo":
        return words
    return ("cargo", *words)


def _cargo_words_are_test(words: tuple[str, ...]) -> bool:
    if not words or normalize_command_name(words[0]) != "cargo":
        return False
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return False
        if word == "test":
            return True
        if word.startswith("+"):
            index += 1
            continue
        if word in _CARGO_OPTIONS_WITH_ARG:
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in _CARGO_OPTIONS_WITH_ARG):
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return False
    return False


def _extract_cargo_lines(output: str) -> tuple[tuple[str, ...], str | None] | None:
    if not isinstance(output, str) or not output.strip():
        return None
    lines = tuple(output.splitlines())
    output_header = lines[0] if lines[:1] and lines[0] in _OUTPUT_HEADERS else None
    if output_header is not None:
        lines = lines[1:]
    if not lines or any(line in _OUTPUT_HEADERS for line in lines):
        return None
    return lines, output_header


def parse_cargo_test_failure_output(output: str) -> ParsedCargoTestFailureOutput | None:
    """Parse high-confidence Cargo/Rust test failure output."""

    extracted = _extract_cargo_lines(output)
    if extracted is None:
        return None
    lines, output_header = extracted
    final_summary_index = _find_final_summary_index(lines)
    if final_summary_index is None:
        return None
    final_summary = lines[final_summary_index]

    failure_marker_indices = [idx for idx, line in enumerate(lines[:final_summary_index]) if line.strip() == "failures:"]
    if not failure_marker_indices:
        return None
    failure_names = _parse_failure_names(lines, failure_marker_indices[-1], final_summary_index)
    if not failure_names:
        return None

    sections = _parse_failure_sections(lines[: failure_marker_indices[-1]])
    if not sections:
        return None
    if not any(_section_has_panic_or_error(section) for section in sections):
        return None

    trailing_error_lines = tuple(line for line in lines[final_summary_index + 1 :] if line.startswith("error:"))
    return ParsedCargoTestFailureOutput(
        failure_names=failure_names,
        sections=sections,
        final_summary=final_summary,
        trailing_error_lines=trailing_error_lines,
        output_header=output_header,
    )


def _find_final_summary_index(lines: tuple[str, ...]) -> int | None:
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if line.startswith("test result: FAILED."):
            return idx
    return None


def _parse_failure_names(lines: tuple[str, ...], marker_index: int, final_summary_index: int) -> tuple[str, ...]:
    names: list[str] = []
    for line in lines[marker_index + 1 : final_summary_index]:
        if not line.strip():
            if names:
                break
            continue
        if line.startswith("    ") or line.startswith("\t"):
            name = line.strip()
            if name:
                names.append(name)
            continue
        if names:
            break
    return tuple(names)


def _parse_failure_sections(lines: tuple[str, ...]) -> tuple[CargoFailureSection, ...]:
    sections: list[CargoFailureSection] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines:
        heading_name = _failure_heading_name(line)
        if heading_name is not None:
            if current_name is not None:
                sections.append(CargoFailureSection(current_name, tuple(current_lines)))
            current_name = heading_name
            current_lines = []
            continue
        if current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        sections.append(CargoFailureSection(current_name, tuple(current_lines)))
    return tuple(section for section in sections if section.name and any(line.strip() for line in section.lines))


def _failure_heading_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("---- ") or not stripped.endswith(" ----"):
        return None
    title = stripped[5:-5].strip()
    for suffix in _FAILURE_HEADING_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    return title or None


def _section_has_panic_or_error(section: CargoFailureSection) -> bool:
    for line in section.lines:
        stripped = line.strip()
        if "panicked at " in stripped or stripped.startswith(("Error:", "error:")):
            return True
    return False


@dataclass(frozen=True)
class CargoTestFailureSummaryFilter:
    """Summarize Cargo/Rust test failures while preserving panic excerpts."""

    name: str = "cargo_test_failure_summary"
    max_failure_names: int = 30
    max_sections: int = 6
    max_lines_per_section: int = 18
    section_head_lines: int = 10
    section_tail_lines: int = 6
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_cargo_test_request(request):
            return None
        parsed = parse_cargo_test_failure_output(request.output)
        if parsed is None:
            return None

        rendered, metadata = self._render(parsed)
        if rendered == request.output:
            return None
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="cargo_test_failures_summarized",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render(self, parsed: ParsedCargoTestFailureOutput) -> tuple[str, dict[str, Any]]:
        max_failure_names = max(1, int(self.max_failure_names))
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

        output_lines = ["Cargo test failure summary:"]
        emitted_names = min(len(parsed.failure_names), max_failure_names)
        for name in parsed.failure_names[:emitted_names]:
            output_lines.append(f"  FAILED {name}")
        omitted_names = len(parsed.failure_names) - emitted_names
        if omitted_names > 0:
            output_lines.append(f"  [... omitted {omitted_names} more failing test names ...]")
        output_lines.append(f"  Final: {parsed.final_summary}")
        for line in parsed.trailing_error_lines:
            output_lines.append(f"  {line}")

        emitted_sections = min(len(parsed.sections), max_sections)
        omitted_sections = len(parsed.sections) - emitted_sections
        omitted_section_lines = 0
        output_lines.append("")
        output_lines.append("Failure excerpts:")
        for section in parsed.sections[:emitted_sections]:
            output_lines.append(f"[FAILURE] {section.name}")
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
            title_preview = ", ".join(section.name for section in remaining_sections[:5])
            if len(remaining_sections) > 5:
                title_preview += ", ..."
            output_lines.append(
                f"[... omitted {omitted_sections} more cargo failure sections"
                f" ({title_preview}) / {remaining_lines} lines ...]"
            )
            omitted_section_lines += remaining_lines

        metadata: dict[str, Any] = {
            "failure_name_count": len(parsed.failure_names),
            "section_count": len(parsed.sections),
            "emitted_failure_names": emitted_names,
            "emitted_sections": emitted_sections,
            "omitted_failure_names": omitted_names,
            "omitted_sections": omitted_sections,
            "omitted_section_lines": omitted_section_lines,
            "final_summary": parsed.final_summary,
            "trailing_error_count": len(parsed.trailing_error_lines),
            "original_output_header": parsed.output_header or "",
            "max_failure_names": max_failure_names,
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
    return (*lines[:head_count], f"[... omitted {omitted} lines from this cargo failure ...]", *tail), omitted


__all__ = [
    "CargoFailureSection",
    "CargoTestFailureSummaryFilter",
    "ParsedCargoTestFailureOutput",
    "is_cargo_test_request",
    "parse_cargo_test_failure_output",
]
