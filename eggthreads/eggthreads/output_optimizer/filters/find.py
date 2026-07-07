from __future__ import annotations

"""Conservative find/fd path-list grouping filter."""

from collections import OrderedDict
from dataclasses import dataclass
import posixpath
from typing import Any

from ..classify import parse_path_list_lines, request_command_name
from ..core import OptimizeDecision, OptimizeRequest, make_decision


FIND_LIKE_COMMANDS = frozenset({"find", "fd", "fdfind"})


def is_find_like_request(request: OptimizeRequest) -> bool:
    """Return True when request metadata identifies find/fd conservatively."""

    command_name = request_command_name(request)
    return command_name in FIND_LIKE_COMMANDS


def _extract_find_stdout_lines(output: str) -> tuple[str, ...] | None:
    """Return candidate path-list stdout lines, or ``None`` for mixed output."""

    if not isinstance(output, str) or not output.strip():
        return None
    lines = output.splitlines()
    if lines and lines[0] == "--- STDOUT ---":
        lines = lines[1:]
    if not lines:
        return None
    if any(line.startswith("--- ") for line in lines):
        return None
    return tuple(lines)


def parse_find_fd_paths(output: str, *, min_paths: int = 8) -> tuple[str, ...] | None:
    """Parse find/fd one-path-per-line output, allowing a bash stdout header."""

    lines = _extract_find_stdout_lines(output)
    if lines is None:
        return None
    return parse_path_list_lines(lines, min_paths=min_paths)


@dataclass(frozen=True)
class FindPathGroupFilter:
    """Group conservative ``find``/``fd`` path-list output by directory."""

    name: str = "find_path_group_by_directory"
    min_paths: int = 8
    max_dirs: int = 40
    max_files_per_dir: int = 40
    max_paths_total: int = 800
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_find_like_request(request):
            return None
        paths = parse_find_fd_paths(request.output, min_paths=self.min_paths)
        if not paths:
            return None

        grouped = _group_paths_by_directory(paths)
        rendered, metadata = self._render_grouped(grouped)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="find_paths_grouped_by_directory",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render_grouped(self, grouped: "OrderedDict[str, list[str]]") -> tuple[str, dict[str, Any]]:
        max_dirs = max(1, int(self.max_dirs))
        max_files_per_dir = max(1, int(self.max_files_per_dir))
        max_paths_total = max(1, int(self.max_paths_total))

        output_lines: list[str] = []
        emitted_dirs = 0
        emitted_paths = 0
        omitted_dirs = 0
        omitted_paths = 0
        omitted_remaining_paths = 0
        remaining_total = max_paths_total
        items = list(grouped.items())

        for index, (directory, names) in enumerate(items):
            if emitted_dirs >= max_dirs or remaining_total <= 0:
                omitted_dirs = len(items) - index
                omitted_remaining_paths = sum(len(remaining) for _directory, remaining in items[index:])
                omitted_paths += omitted_remaining_paths
                break

            include_count = min(len(names), max_files_per_dir, remaining_total)
            if include_count <= 0:
                omitted_dirs = len(items) - index
                omitted_remaining_paths = sum(len(remaining) for _directory, remaining in items[index:])
                omitted_paths += omitted_remaining_paths
                break

            if output_lines:
                output_lines.append("")
            output_lines.append(f"{directory}:")
            for name in names[:include_count]:
                output_lines.append(f"  {name}")
            emitted_dirs += 1
            emitted_paths += include_count
            remaining_total -= include_count

            dir_omitted = len(names) - include_count
            if dir_omitted > 0:
                output_lines.append(f"  [... omitted {dir_omitted} more paths in this directory ...]")
                omitted_paths += dir_omitted

        if omitted_dirs > 0:
            if output_lines:
                output_lines.append("")
            output_lines.append(f"[... omitted {omitted_dirs} more directories / {omitted_remaining_paths} paths due to cap ...]")

        metadata = {
            "directory_count": len(grouped),
            "path_count": sum(len(names) for names in grouped.values()),
            "emitted_dirs": emitted_dirs,
            "emitted_paths": emitted_paths,
            "omitted_dirs": omitted_dirs,
            "omitted_paths": omitted_paths,
            "omitted_remaining_paths": omitted_remaining_paths,
            "max_dirs": max_dirs,
            "max_files_per_dir": max_files_per_dir,
            "max_paths_total": max_paths_total,
        }
        return "\n".join(output_lines), metadata


@dataclass(frozen=True)
class PathListOutputShapeFilter(FindPathGroupFilter):
    """Group path-list-shaped output when the bash command is complex."""

    name: str = "path_list_output_shape_group_by_directory"
    confidence: float = 0.85

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if is_find_like_request(request):
            return None
        paths = parse_find_fd_paths(request.output, min_paths=self.min_paths)
        if not paths:
            return None

        grouped = _group_paths_by_directory(paths)
        rendered, metadata = self._render_grouped(grouped)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        metadata["matched_by_output_shape"] = True
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="path_list_output_shape_grouped_by_directory",
            confidence=self.confidence,
            metadata=metadata,
        )


def _group_paths_by_directory(paths: tuple[str, ...]) -> "OrderedDict[str, list[str]]":
    grouped: "OrderedDict[str, list[str]]" = OrderedDict()
    for path in paths:
        directory, name = _split_path(path)
        grouped.setdefault(directory, []).append(name)
    return grouped


def _split_path(path: str) -> tuple[str, str]:
    trimmed = path.rstrip("/") if path != "/" else path
    directory = posixpath.dirname(trimmed) or "."
    name = posixpath.basename(trimmed) or trimmed
    return directory, name


__all__ = [
    "FIND_LIKE_COMMANDS",
    "FindPathGroupFilter",
    "PathListOutputShapeFilter",
    "is_find_like_request",
    "parse_find_fd_paths",
]
