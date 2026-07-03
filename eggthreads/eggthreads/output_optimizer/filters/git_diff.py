from __future__ import annotations

"""Conservative git-diff compact preview filter."""

from dataclasses import dataclass
import re
from typing import Any

from ..classify import git_request_subcommand_words, normalize_command_name
from ..core import OptimizeDecision, OptimizeRequest, make_decision


GIT_DIFF_TOOL_NAMES = frozenset({"git_diff", "git-diff"})
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?$")


@dataclass(frozen=True)
class GitDiffHunk:
    header: str
    changed_lines: tuple[str, ...]
    body_line_count: int


@dataclass(frozen=True)
class GitDiffFile:
    old_path: str
    new_path: str
    display_path: str
    hunks: tuple[GitDiffHunk, ...]


def is_git_diff_request(request: OptimizeRequest) -> bool:
    """Return True for high-confidence git-diff requests."""

    tool_name = normalize_command_name(request.tool_name)
    if tool_name in GIT_DIFF_TOOL_NAMES:
        return True
    if tool_name in {"git", "bash"}:
        subcommand_words = git_request_subcommand_words(request)
        return bool(subcommand_words and subcommand_words[0] == "diff")
    return False


def _extract_git_diff_lines(output: str) -> tuple[str, ...] | None:
    if not isinstance(output, str) or not output.strip():
        return None
    lines = output.splitlines()
    if lines and lines[0] == "--- STDOUT ---":
        lines = lines[1:]
    if not lines or any(line in {"--- STDOUT ---", "--- STDERR ---"} for line in lines):
        return None
    return tuple(lines)


def parse_git_diff(output: str) -> tuple[GitDiffFile, ...] | None:
    """Parse conservative unified ``git diff`` output."""

    lines = _extract_git_diff_lines(output)
    if lines is None or not lines or not lines[0].startswith("diff --git "):
        return None

    files: list[GitDiffFile] = []
    index = 0
    while index < len(lines):
        header_match = _DIFF_HEADER_RE.match(lines[index])
        if header_match is None:
            return None
        old_path, new_path = header_match.group(1), header_match.group(2)
        index += 1
        section: list[str] = []
        while index < len(lines) and not lines[index].startswith("diff --git "):
            section.append(lines[index])
            index += 1
        parsed = _parse_diff_section(old_path, new_path, tuple(section))
        if parsed is None:
            return None
        files.append(parsed)

    if not files or not any(file.hunks for file in files):
        return None
    return tuple(files)


def _parse_diff_section(old_path: str, new_path: str, section: tuple[str, ...]) -> GitDiffFile | None:
    hunk_indices = [idx for idx, line in enumerate(section) if _HUNK_HEADER_RE.match(line)]
    if not hunk_indices:
        return None
    if not any(line.startswith("--- ") for line in section[: hunk_indices[0]]):
        return None
    if not any(line.startswith("+++ ") for line in section[: hunk_indices[0]]):
        return None

    hunks: list[GitDiffHunk] = []
    for pos, start in enumerate(hunk_indices):
        end = hunk_indices[pos + 1] if pos + 1 < len(hunk_indices) else len(section)
        body = section[start + 1 : end]
        changed_lines: list[str] = []
        for line in body:
            if line.startswith((" ", "\\")):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                changed_lines.append(line)
                continue
            if line.startswith("-") and not line.startswith("---"):
                changed_lines.append(line)
                continue
            return None
        if not changed_lines:
            return None
        hunks.append(GitDiffHunk(header=section[start], changed_lines=tuple(changed_lines), body_line_count=len(body)))

    display_path = old_path if old_path == new_path else f"{old_path} -> {new_path}"
    return GitDiffFile(old_path=old_path, new_path=new_path, display_path=display_path, hunks=tuple(hunks))


@dataclass(frozen=True)
class GitDiffCompactFilter:
    """Compact unified git diffs by file/hunk changed lines."""

    name: str = "git_diff_compact"
    max_files: int = 20
    max_hunks_per_file: int = 12
    max_hunks_total: int = 80
    max_changed_lines_per_hunk: int = 30
    max_changed_lines_total: int = 800
    confidence: float = 0.9

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not is_git_diff_request(request):
            return None
        files = parse_git_diff(request.output)
        if not files:
            return None
        rendered, metadata = self._render_files(files)
        if rendered == request.output:
            return None
        metadata["original_had_stdout_header"] = request.output.splitlines()[:1] == ["--- STDOUT ---"]
        return make_decision(
            request,
            rendered,
            filter_name=self.name,
            reason="git_diff_compacted",
            confidence=self.confidence,
            metadata=metadata,
        )

    def _render_files(self, files: tuple[GitDiffFile, ...]) -> tuple[str, dict[str, Any]]:
        max_files = max(1, int(self.max_files))
        max_hunks_per_file = max(1, int(self.max_hunks_per_file))
        max_hunks_total = max(1, int(self.max_hunks_total))
        max_changed_lines_per_hunk = max(1, int(self.max_changed_lines_per_hunk))
        max_changed_lines_total = max(1, int(self.max_changed_lines_total))

        output_lines: list[str] = []
        emitted_files = 0
        emitted_hunks = 0
        emitted_changed_lines = 0
        omitted_files = 0
        omitted_hunks = 0
        omitted_changed_lines = 0
        omitted_remaining_hunks = 0
        omitted_remaining_changed_lines = 0
        remaining_hunks = max_hunks_total
        remaining_changed_lines = max_changed_lines_total

        for file_index, diff_file in enumerate(files):
            if emitted_files >= max_files or remaining_hunks <= 0 or remaining_changed_lines <= 0:
                omitted_files = len(files) - file_index
                omitted_remaining_hunks = sum(len(file.hunks) for file in files[file_index:])
                omitted_remaining_changed_lines = sum(_changed_line_count(file) for file in files[file_index:])
                omitted_hunks += omitted_remaining_hunks
                omitted_changed_lines += omitted_remaining_changed_lines
                break

            if output_lines:
                output_lines.append("")
            output_lines.append(f"diff --git {diff_file.display_path}")
            emitted_files += 1

            file_hunks_emitted = 0
            for hunk_index, hunk in enumerate(diff_file.hunks):
                if file_hunks_emitted >= max_hunks_per_file or remaining_hunks <= 0 or remaining_changed_lines <= 0:
                    remaining_file_hunks = diff_file.hunks[hunk_index:]
                    remaining_file_hunk_count = len(remaining_file_hunks)
                    remaining_file_changed_lines = sum(len(item.changed_lines) for item in remaining_file_hunks)
                    output_lines.append(
                        f"  [... omitted {remaining_file_hunk_count} more hunks / "
                        f"{remaining_file_changed_lines} changed lines in this file ...]"
                    )
                    omitted_hunks += remaining_file_hunk_count
                    omitted_changed_lines += remaining_file_changed_lines
                    break

                output_lines.append(f"  {hunk.header}")
                include_count = min(len(hunk.changed_lines), max_changed_lines_per_hunk, remaining_changed_lines)
                for line in hunk.changed_lines[:include_count]:
                    output_lines.append(f"    {line}")
                emitted_hunks += 1
                file_hunks_emitted += 1
                emitted_changed_lines += include_count
                remaining_hunks -= 1
                remaining_changed_lines -= include_count

                hunk_omitted = len(hunk.changed_lines) - include_count
                if hunk_omitted > 0:
                    output_lines.append(f"    [... omitted {hunk_omitted} more changed lines in this hunk ...]")
                    omitted_changed_lines += hunk_omitted

        if omitted_files > 0:
            if output_lines:
                output_lines.append("")
            output_lines.append(
                f"[... omitted {omitted_files} more files / {omitted_remaining_hunks} hunks / "
                f"{omitted_remaining_changed_lines} changed lines due to cap ...]"
            )

        metadata = {
            "file_count": len(files),
            "hunk_count": sum(len(file.hunks) for file in files),
            "changed_line_count": sum(_changed_line_count(file) for file in files),
            "emitted_files": emitted_files,
            "emitted_hunks": emitted_hunks,
            "emitted_changed_lines": emitted_changed_lines,
            "omitted_files": omitted_files,
            "omitted_hunks": omitted_hunks,
            "omitted_changed_lines": omitted_changed_lines,
            "omitted_remaining_hunks": omitted_remaining_hunks,
            "omitted_remaining_changed_lines": omitted_remaining_changed_lines,
            "max_files": max_files,
            "max_hunks_per_file": max_hunks_per_file,
            "max_hunks_total": max_hunks_total,
            "max_changed_lines_per_hunk": max_changed_lines_per_hunk,
            "max_changed_lines_total": max_changed_lines_total,
        }
        return "\n".join(output_lines), metadata


def _changed_line_count(diff_file: GitDiffFile) -> int:
    return sum(len(hunk.changed_lines) for hunk in diff_file.hunks)


__all__ = [
    "GitDiffCompactFilter",
    "GitDiffFile",
    "GitDiffHunk",
    "is_git_diff_request",
    "parse_git_diff",
]
