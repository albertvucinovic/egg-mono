from __future__ import annotations

"""Conservative ``ls -l`` / directory-listing optimizer filter."""

from collections import OrderedDict
from dataclasses import dataclass
import re
from typing import Any, Iterable

from ..classify import request_command_name, simple_bash_command_invocation
from ..core import OptimizeDecision, OptimizeRequest, make_decision


LS_COMMANDS = frozenset({"ls", "dir"})
_LS_DETAIL_LINE_RE = re.compile(
    r"^(?P<mode>[bcdlps-][rwxstST-]{9}[.+@]?)\s+"
    r"(?P<links>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<month>\S+)\s+"
    r"(?P<day>\S+)\s+"
    r"(?P<timeyear>\S+)\s+"
    r"(?P<name>.+)$"
)
_LS_ERROR_PREFIXES = ("ls:", "dir:")


def is_ls_like_request(request: OptimizeRequest) -> bool:
    """Return True for high-confidence direct/simple bash ``ls`` requests."""

    return request_command_name(request) in LS_COMMANDS


def _ls_invocation_words(request: OptimizeRequest) -> tuple[str, ...]:
    tool_name = request_command_name(request)
    if tool_name not in LS_COMMANDS:
        return ()
    if tool_name == request.tool_name:
        return (tool_name,)
    try:
        script = request.tool_args.get("script")
    except Exception:
        script = None
    words = simple_bash_command_invocation(script)
    if words and words[0].rsplit("/", 1)[-1].lower() in LS_COMMANDS:
        return words
    return (tool_name,)


def _ls_invocation_is_long_listing(words: Iterable[str]) -> bool:
    """Return whether words intentionally request long/detail output."""

    word_tuple = tuple(words or ())
    for word in word_tuple[1:]:
        if word == "--":
            break
        if not word.startswith("-") or word == "-":
            continue
        if word == "--long":
            return True
        if word.startswith("--"):
            continue
        if "l" in word[1:]:
            return True
    return False


def _strip_stdout_header(output: str) -> tuple[tuple[str, ...], bool] | None:
    if not isinstance(output, str) or not output.strip():
        return None
    lines = tuple(output.splitlines())
    had_stdout_header = bool(lines[:1] == ("--- STDOUT ---",))
    if had_stdout_header:
        lines = lines[1:]
    if not lines:
        return None
    if any(line == "--- STDERR ---" or line.startswith(_LS_ERROR_PREFIXES) for line in lines):
        return None
    if any(line.startswith("--- ") for line in lines):
        return None
    return lines, had_stdout_header


def _parse_ls_detail_line(line: str) -> dict[str, Any] | None:
    match = _LS_DETAIL_LINE_RE.match(line)
    if not match:
        return None
    name = match.group("name")
    if not name or name in {".", ".."}:
        return None
    try:
        size = int(match.group("size"))
    except Exception:
        size = 0
    mode = match.group("mode")
    return {
        "mode": mode,
        "kind": mode[0],
        "size": size,
        "name": name,
        "owner": match.group("owner"),
        "group": match.group("group"),
        "month": match.group("month"),
        "day": match.group("day"),
        "timeyear": match.group("timeyear"),
    }


def _is_dot_directory_entry(line: str) -> bool:
    match = _LS_DETAIL_LINE_RE.match(line)
    return bool(match and match.group("name") in {".", ".."})


def parse_ls_long_listing(output: str, *, min_entries: int = 8) -> tuple[dict[str, Any], ...] | None:
    """Parse conservative ``ls -l`` output, allowing Egg's stdout header."""

    stripped = _strip_stdout_header(output)
    if stripped is None:
        return None
    lines, _had_stdout_header = stripped
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line:
            return None
        if index == 0 and line.startswith("total "):
            continue
        if line.endswith(":"):
            # Recursive or multi-directory ls output is useful but broader than
            # this first conservative listing filter.
            return None
        if _is_dot_directory_entry(line):
            # ``ls -la`` commonly includes these two implementation details.
            # They add no useful context for the model and should not cause the
            # otherwise-normal long listing to abstain.
            continue
        parsed = _parse_ls_detail_line(line)
        if parsed is None:
            return None
        entries.append(parsed)
    if len(entries) < max(1, int(min_entries)):
        return None
    return tuple(entries)


def _kind_label(kind: str) -> str:
    return {
        "d": "directories",
        "-": "files",
        "l": "symlinks",
        "b": "block devices",
        "c": "char devices",
        "p": "fifos",
        "s": "sockets",
    }.get(kind, f"type {kind}")


@dataclass(frozen=True)
class LsLongListingFilter:
    """Summarize conservative long ``ls`` output without rewriting commands."""

    name: str = "ls_long_listing_summary"
    min_entries: int = 8
    max_entries_per_kind: int = 60
    max_entries_total: int = 400
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_ls_like_request(request):
            return None
        words = _ls_invocation_words(request)
        if words and not _ls_invocation_is_long_listing(words):
            # Avoid surprising users who asked for short plain ``ls`` output.
            return None
        parsed = parse_ls_long_listing(request.output, min_entries=self.min_entries)
        if not parsed:
            return None

        rendered, metadata = self._render(parsed)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        metadata["long_listing_requested"] = _ls_invocation_is_long_listing(words)
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="ls_long_listing_summarized",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render(self, entries: tuple[dict[str, Any], ...]) -> tuple[str, dict[str, Any]]:
        max_entries_per_kind = max(1, int(self.max_entries_per_kind))
        max_entries_total = max(1, int(self.max_entries_total))
        grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for entry in entries:
            grouped.setdefault(str(entry.get("kind") or "?"), []).append(entry)

        lines: list[str] = [f"ls -l summary: {len(entries)} entries"]
        total_size = sum(int(entry.get("size") or 0) for entry in entries if entry.get("kind") == "-")
        if total_size:
            lines.append(f"regular file bytes listed: {total_size}")

        emitted_total = 0
        omitted_total = 0
        for kind, items in grouped.items():
            if emitted_total >= max_entries_total:
                omitted_total += len(items)
                continue
            include_count = min(len(items), max_entries_per_kind, max_entries_total - emitted_total)
            lines.append("")
            lines.append(f"{_kind_label(kind)} ({len(items)}):")
            for entry in items[:include_count]:
                suffix = "/" if kind == "d" else "@" if kind == "l" else ""
                size_part = f" ({entry.get('size')} bytes)" if kind == "-" else ""
                lines.append(f"  {entry.get('name')}{suffix}{size_part}")
            emitted_total += include_count
            omitted_here = len(items) - include_count
            if omitted_here > 0:
                lines.append(f"  [... omitted {omitted_here} more {_kind_label(kind)} ...]")
                omitted_total += omitted_here

        metadata = {
            "entry_count": len(entries),
            "kind_counts": {kind: len(items) for kind, items in grouped.items()},
            "regular_file_bytes": total_size,
            "emitted_entries": emitted_total,
            "omitted_entries": omitted_total,
            "max_entries_per_kind": max_entries_per_kind,
            "max_entries_total": max_entries_total,
        }
        return "\n".join(lines), metadata


__all__ = [
    "LS_COMMANDS",
    "LsLongListingFilter",
    "is_ls_like_request",
    "parse_ls_long_listing",
]
