from __future__ import annotations

"""Conservative grep/rg output grouping filter."""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ..classify import PathLineContent, is_plausible_path_list_line, parse_path_line_content_lines, request_command_name
from ..core import OptimizeDecision, OptimizeRequest, make_decision


GREP_LIKE_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "ripgrep"})


def is_grep_like_request(request: OptimizeRequest) -> bool:
    """Return True when request metadata identifies grep/ripgrep conservatively."""

    command_name = request_command_name(request)
    return command_name in GREP_LIKE_COMMANDS


def _extract_grep_stdout_lines(output: str) -> tuple[str, ...] | None:
    """Return candidate grep stdout lines, or ``None`` for mixed/ambiguous output."""

    if not isinstance(output, str) or not output.strip():
        return None
    lines = output.splitlines()
    if lines and lines[0] == "--- STDOUT ---":
        lines = lines[1:]
    if not lines:
        return None
    # If stderr or another tool-header block is present, keep the raw output.
    if any(line.startswith("--- ") for line in lines):
        return None
    return tuple(lines)


def parse_grep_rg_matches(output: str) -> tuple[PathLineContent, ...] | None:
    """Parse grep/rg ``path:line:content`` output, allowing a bash stdout header."""

    lines = _extract_grep_stdout_lines(output)
    if lines is None:
        return None
    return parse_path_line_content_lines(lines)


@dataclass(frozen=True)
class GrepRgGroupByFileFilter:
    """Group conservative ``grep``/``rg`` line matches by file."""

    name: str = "grep_rg_group_by_file"
    max_files: int = 40
    max_matches_per_file: int = 20
    max_matches_total: int = 400
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_grep_like_request(request):
            return None
        matches = parse_grep_rg_matches(request.output)
        if not matches:
            return None

        grouped = _group_matches_by_file(matches)
        rendered, metadata = self._render_grouped(grouped)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="grep_rg_grouped_by_file",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render_grouped(self, grouped: "OrderedDict[str, list[PathLineContent]]") -> tuple[str, dict[str, Any]]:
        max_files = max(1, int(self.max_files))
        max_matches_per_file = max(1, int(self.max_matches_per_file))
        max_matches_total = max(1, int(self.max_matches_total))

        output_lines: list[str] = []
        emitted_files = 0
        emitted_matches = 0
        omitted_files = 0
        omitted_matches = 0
        omitted_remaining_matches = 0
        remaining_total = max_matches_total
        items = list(grouped.items())

        for index, (path, matches) in enumerate(items):
            if emitted_files >= max_files or remaining_total <= 0:
                omitted_files = len(items) - index
                omitted_remaining_matches = sum(len(remaining) for _path, remaining in items[index:])
                omitted_matches += omitted_remaining_matches
                break

            include_count = min(len(matches), max_matches_per_file, remaining_total)
            if include_count <= 0:
                omitted_files = len(items) - index
                omitted_remaining_matches = sum(len(remaining) for _path, remaining in items[index:])
                omitted_matches += omitted_remaining_matches
                break

            if output_lines:
                output_lines.append("")
            output_lines.append(f"{path}:")
            for match in matches[:include_count]:
                output_lines.append(f"  {match.line_number}: {match.content}")
            emitted_files += 1
            emitted_matches += include_count
            remaining_total -= include_count

            file_omitted = len(matches) - include_count
            if file_omitted > 0:
                output_lines.append(f"  [... omitted {file_omitted} more matches in this file ...]")
                omitted_matches += file_omitted

        if omitted_files > 0:
            if output_lines:
                output_lines.append("")
            output_lines.append(f"[... omitted {omitted_files} more files / {omitted_remaining_matches} matches due to cap ...]")

        metadata = {
            "file_count": len(grouped),
            "match_count": sum(len(matches) for matches in grouped.values()),
            "emitted_files": emitted_files,
            "emitted_matches": emitted_matches,
            "omitted_files": omitted_files,
            "omitted_matches": omitted_matches,
            "omitted_remaining_matches": omitted_remaining_matches,
            "max_files": max_files,
            "max_matches_per_file": max_matches_per_file,
            "max_matches_total": max_matches_total,
        }
        return "\n".join(output_lines), metadata


@dataclass(frozen=True)
class GrepRgOutputShapeFilter(GrepRgGroupByFileFilter):
    """Group grep-shaped output even when bash command parsing is ambiguous.

    Many high-volume bash calls are safe wrappers such as ``grep ... | head``
    or multi-line inspection scripts.  The simple command classifier rejects
    those by design, so this filter only trusts the output shape: every
    non-header line must parse as ``path:line:content``.
    """

    name: str = "grep_rg_output_shape_group_by_file"
    min_matches: int = 4
    confidence: float = 0.85

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if is_grep_like_request(request):
            return None
        matches = parse_grep_rg_matches(request.output)
        if not matches or len(matches) < max(1, int(self.min_matches)):
            return None
        if not all(is_plausible_path_list_line(match.path) for match in matches):
            return None

        grouped = _group_matches_by_file(matches)
        rendered, metadata = self._render_grouped(grouped)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        metadata["matched_by_output_shape"] = True
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="grep_rg_output_shape_grouped_by_file",
            confidence=self.confidence,
            metadata=metadata,
        )


def _group_matches_by_file(matches: tuple[PathLineContent, ...]) -> "OrderedDict[str, list[PathLineContent]]":
    grouped: "OrderedDict[str, list[PathLineContent]]" = OrderedDict()
    for match in matches:
        grouped.setdefault(match.path, []).append(match)
    return grouped


__all__ = [
    "GREP_LIKE_COMMANDS",
    "GrepRgGroupByFileFilter",
    "GrepRgOutputShapeFilter",
    "is_grep_like_request",
    "parse_grep_rg_matches",
]
