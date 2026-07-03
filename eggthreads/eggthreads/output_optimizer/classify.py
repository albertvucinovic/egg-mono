from __future__ import annotations

"""Conservative classifiers shared by semantic output optimizer filters."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from typing import Any, Iterable

from .core import OptimizeRequest


_SIMPLE_SHELL_OPERATOR_RE = re.compile(r"(?:\n|;|\|\||&&|\||&|>|<|`|\$\(|\(|\))")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def normalize_command_name(command: str) -> str:
    """Return a lowercase basename for a command token."""

    if not isinstance(command, str) or not command.strip():
        return ""
    return Path(command.strip()).name.lower()


def simple_bash_command_words(script: Any) -> tuple[str, ...]:
    """Parse a single simple shell command into words, or return ``()``.

    This intentionally rejects pipelines, redirects, command substitutions,
    multiple statements, and malformed quoting.  Semantic output filters use it
    only as a high-confidence hint; ambiguous shell snippets should abstain.
    """

    if not isinstance(script, str):
        return ()
    text = script.strip()
    if not text or _SIMPLE_SHELL_OPERATOR_RE.search(text):
        return ()
    try:
        words = tuple(shlex.split(text, comments=False, posix=True))
    except ValueError:
        return ()
    return words


def simple_bash_command_invocation(script: Any) -> tuple[str, ...]:
    """Return command words after simple env/``command`` wrappers."""

    words = list(simple_bash_command_words(script))
    while words and _ENV_ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    if words and words[0] == "command":
        words.pop(0)
    return tuple(words)


def simple_bash_command_name(script: Any) -> str | None:
    """Return the command name for a single simple shell command, if known."""

    words = list(simple_bash_command_invocation(script))
    if not words:
        return None
    name = normalize_command_name(words[0])
    return name or None


def request_command_name(request: OptimizeRequest) -> str | None:
    """Best-effort command name from tool metadata or a simple bash script."""

    tool_name = normalize_command_name(request.tool_name)
    if tool_name and tool_name != "bash":
        return tool_name
    if tool_name == "bash":
        try:
            script = request.tool_args.get("script")
        except Exception:
            script = None
        return simple_bash_command_name(script)
    return None


@dataclass(frozen=True)
class PathLineContent:
    path: str
    line_number: int
    content: str


def parse_path_line_content(line: str) -> PathLineContent | None:
    """Parse conservative ``path:line:content`` output lines."""

    if not isinstance(line, str) or not line:
        return None
    parts = line.split(":", 2)
    if len(parts) != 3:
        return None
    path, line_number_text, content = parts
    if not path or path != path.strip() or path.startswith("---") or path.startswith("["):
        return None
    if not line_number_text.isdigit():
        return None
    line_number = int(line_number_text)
    if line_number <= 0:
        return None
    return PathLineContent(path=path, line_number=line_number, content=content)


def parse_path_line_content_lines(lines: Iterable[str]) -> tuple[PathLineContent, ...] | None:
    """Parse all lines as ``path:line:content`` or return ``None``."""

    parsed: list[PathLineContent] = []
    for line in lines:
        item = parse_path_line_content(line)
        if item is None:
            return None
        parsed.append(item)
    return tuple(parsed)


def is_plausible_path_list_line(line: str) -> bool:
    """Return True for conservative one-path-per-line output entries."""

    if not isinstance(line, str) or not line:
        return False
    if line != line.strip():
        return False
    if "\x00" in line or "\r" in line:
        return False
    if line.startswith("---") or line.startswith("["):
        return False
    # Avoid colliding with grep-style ``path:line:content`` and other colon
    # diagnostics.  POSIX paths can contain colons, but this optimizer is
    # deliberately conservative.
    if ":" in line:
        return False

    basename = line.rstrip("/").rsplit("/", 1)[-1]
    if not basename:
        return line in {"/"}
    return (
        "/" in line
        or line.startswith((".", "~"))
        or ("." in basename and not basename.endswith("."))
    )


def parse_path_list_lines(lines: Iterable[str], *, min_paths: int = 8) -> tuple[str, ...] | None:
    """Parse conservative one-path-per-line output or return ``None``."""

    non_empty = tuple(line for line in lines if isinstance(line, str) and line.strip())
    if len(non_empty) < max(1, int(min_paths)):
        return None
    if parse_path_line_content_lines(non_empty) is not None:
        return None
    if not all(is_plausible_path_list_line(line) for line in non_empty):
        return None
    return non_empty


__all__ = [
    "PathLineContent",
    "is_plausible_path_list_line",
    "normalize_command_name",
    "parse_path_list_lines",
    "parse_path_line_content",
    "parse_path_line_content_lines",
    "request_command_name",
    "simple_bash_command_invocation",
    "simple_bash_command_name",
    "simple_bash_command_words",
]
