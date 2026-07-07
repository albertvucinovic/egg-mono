from __future__ import annotations

"""Conservative git-status short/porcelain optimizer filter."""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ..classify import git_request_subcommand_words, normalize_command_name, request_command_name
from ..core import OptimizeDecision, OptimizeRequest, make_decision


GIT_STATUS_TOOL_NAMES = frozenset({"git_status", "git-status"})
_VALID_STATUS_CHARS = frozenset(" MADRCU?!")
_STATUS_LABELS = {
    "??": "Untracked",
    "!!": "Ignored",
}


@dataclass(frozen=True)
class GitStatusEntry:
    code: str
    path: str


def is_git_status_request(request: OptimizeRequest) -> bool:
    """Return True for high-confidence git-status requests."""

    tool_name = normalize_command_name(request.tool_name)
    if tool_name in GIT_STATUS_TOOL_NAMES:
        return True
    if tool_name in {"git", "bash"}:
        subcommand_words = git_request_subcommand_words(request)
        return bool(subcommand_words and subcommand_words[0] == "status")
    # Some future direct tools may expose the full command as their tool name.
    return request_command_name(request) in GIT_STATUS_TOOL_NAMES


def _extract_git_status_lines(output: str) -> tuple[str, ...] | None:
    if not isinstance(output, str) or not output.strip():
        return None
    lines = output.splitlines()
    if lines and lines[0] == "--- STDOUT ---":
        lines = lines[1:]
    if not lines or any(not line for line in lines):
        return None
    if any(line.startswith("--- ") for line in lines):
        return None
    return tuple(lines)


def parse_git_status_entries(output: str) -> tuple[GitStatusEntry, ...] | None:
    """Parse ``git status --short`` / porcelain-v1 path entries."""

    lines = _extract_git_status_lines(output)
    if lines is None:
        return None
    entries: list[GitStatusEntry] = []
    for line in lines:
        entry = parse_git_status_line(line)
        if entry is None:
            return None
        entries.append(entry)
    return tuple(entries)


def parse_git_status_line(line: str) -> GitStatusEntry | None:
    if not isinstance(line, str) or len(line) < 4:
        return None
    if line.startswith("##") or line[2] != " ":
        return None
    code = line[:2]
    if code == "  " or any(ch not in _VALID_STATUS_CHARS for ch in code):
        return None
    path = line[3:]
    if not path or path != path.strip() or path.startswith("[") or path.startswith("---"):
        return None
    if ("R" in code or "C" in code) and " -> " not in path:
        return None
    if " -> " in path and not ("R" in code or "C" in code):
        return None
    return GitStatusEntry(code=code, path=path)


@dataclass(frozen=True)
class GitStatusCompactFilter:
    """Compact conservative git short-status output by exact status code."""

    name: str = "git_status_compact"
    max_statuses: int = 12
    max_entries_per_status: int = 80
    max_entries_total: int = 800
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_git_status_request(request):
            return None
        entries = parse_git_status_entries(request.output)
        if not entries:
            return None

        grouped = _group_entries_by_code(entries)
        rendered, metadata = self._render_grouped(grouped)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="git_status_compacted",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render_grouped(self, grouped: "OrderedDict[str, list[GitStatusEntry]]") -> tuple[str, dict[str, Any]]:
        max_statuses = max(1, int(self.max_statuses))
        max_entries_per_status = max(1, int(self.max_entries_per_status))
        max_entries_total = max(1, int(self.max_entries_total))

        output_lines: list[str] = []
        emitted_statuses = 0
        emitted_entries = 0
        omitted_statuses = 0
        omitted_entries = 0
        omitted_remaining_entries = 0
        remaining_total = max_entries_total
        items = list(grouped.items())

        for index, (code, entries) in enumerate(items):
            if emitted_statuses >= max_statuses or remaining_total <= 0:
                omitted_statuses = len(items) - index
                omitted_remaining_entries = sum(len(remaining) for _code, remaining in items[index:])
                omitted_entries += omitted_remaining_entries
                break

            include_count = min(len(entries), max_entries_per_status, remaining_total)
            if include_count <= 0:
                omitted_statuses = len(items) - index
                omitted_remaining_entries = sum(len(remaining) for _code, remaining in items[index:])
                omitted_entries += omitted_remaining_entries
                break

            if output_lines:
                output_lines.append("")
            output_lines.append(f"[{code}] {_status_label(code)} ({len(entries)}):")
            for entry in entries[:include_count]:
                output_lines.append(f"  {entry.path}")
            emitted_statuses += 1
            emitted_entries += include_count
            remaining_total -= include_count

            status_omitted = len(entries) - include_count
            if status_omitted > 0:
                output_lines.append(f"  [... omitted {status_omitted} more entries with status {code!r} ...]")
                omitted_entries += status_omitted

        if omitted_statuses > 0:
            if output_lines:
                output_lines.append("")
            output_lines.append(f"[... omitted {omitted_statuses} more status groups / {omitted_remaining_entries} entries due to cap ...]")

        metadata = {
            "status_count": len(grouped),
            "entry_count": sum(len(entries) for entries in grouped.values()),
            "emitted_statuses": emitted_statuses,
            "emitted_entries": emitted_entries,
            "omitted_statuses": omitted_statuses,
            "omitted_entries": omitted_entries,
            "omitted_remaining_entries": omitted_remaining_entries,
            "max_statuses": max_statuses,
            "max_entries_per_status": max_entries_per_status,
            "max_entries_total": max_entries_total,
        }
        return "\n".join(output_lines), metadata


def _group_entries_by_code(entries: tuple[GitStatusEntry, ...]) -> "OrderedDict[str, list[GitStatusEntry]]":
    grouped: "OrderedDict[str, list[GitStatusEntry]]" = OrderedDict()
    for entry in entries:
        grouped.setdefault(entry.code, []).append(entry)
    return grouped


def _status_label(code: str) -> str:
    if code in _STATUS_LABELS:
        return _STATUS_LABELS[code]
    if "U" in code:
        return "Unmerged"
    if "R" in code:
        return "Renamed"
    if "C" in code:
        return "Copied"
    if "A" in code:
        return "Added"
    if "D" in code:
        return "Deleted"
    if "M" in code:
        return "Modified"
    return "Other"


__all__ = [
    "GitStatusCompactFilter",
    "GitStatusEntry",
    "is_git_status_request",
    "parse_git_status_entries",
    "parse_git_status_line",
]
