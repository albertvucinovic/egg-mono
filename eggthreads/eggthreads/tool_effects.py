"""Conservative, presentation-only classification of tool-call side effects."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping


class ToolEffect(str, Enum):
    """Advisory side-effect class used only by transcript presentation."""

    READ = "read"
    MAY_WRITE = "may_write"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            ToolEffect.READ: "READ",
            ToolEffect.MAY_WRITE: "MAY WRITE",
            ToolEffect.UNKNOWN: "UNKNOWN",
        }[self]


@dataclass(frozen=True)
class ToolEffectClassification:
    effect: ToolEffect
    reason: str = ""


_READ_ONLY_TOOLS = frozenset({
    "fetch_url",
    "get_child_status",
    "read_long_tool_output",
    "skill",
    "threads",
    "tool_help",
    "web_search",
})
_MAY_WRITE_TOOLS = frozenset({
    "extract_tool_output",
    "generate_image",
    "save_provider_artifact_to_file",
})
_UNKNOWN_EXECUTION_TOOLS = frozenset({"bash_repl", "python_exec", "python_repl"})

# Commands enter the READ class only when their semantics are narrow and well
# understood. Unknown commands abstain rather than being optimistically marked.
_READ_COMMANDS = frozenset({
    "awk", "basename", "cat", "cksum", "cmp", "column", "comm", "cut",
    "date", "diff", "diffstat", "dirname", "du", "file", "find",
    "fmt", "fold", "grep", "head", "id", "jq", "ls", "md5sum", "nl",
    "echo", "od", "paste", "printf", "pwd", "readlink", "realpath", "rg", "sed",
    "sha1sum", "sha256sum", "sort", "stat", "strings", "tail", "test",
    "tr", "tree", "true", "type", "uname", "uniq", "wc", "which", "whoami",
    # xargs is intentionally absent: it can invoke arbitrary writers.
})
_WRITE_COMMANDS = frozenset({
    "apply_patch", "applypatch", "chmod", "chown", "chgrp", "cp", "dd",
    "install", "ln", "mkdir", "mkfifo", "mknod", "mv", "patch", "rm",
    "rmdir", "rsync", "tee", "touch", "truncate",
})
_WRAPPERS = frozenset({"command", "env", "sudo", "time"})
_SHELL_SEPARATORS = frozenset({"|", "||", "&&", ";", "&", "\n"})
_WRITE_REDIRECTIONS = frozenset({">", ">>", ">&", "&>", "<>"})
_READ_REDIRECTIONS = frozenset({"<", "<<", "<<<"})
_GIT_READ_SUBCOMMANDS = frozenset({
    "blame", "branch", "cat-file", "diff", "diff-tree", "for-each-ref",
    "grep", "help", "log", "ls-files", "ls-tree", "merge-base", "name-rev",
    "remote", "rev-list", "rev-parse", "shortlog", "show", "show-ref",
    "status", "tag", "version", "whatchanged",
})
_GIT_WRITE_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "bisect", "branch", "checkout", "cherry-pick",
    "clean", "clone", "commit", "config", "fetch", "gc", "init", "merge",
    "mv", "notes", "pull", "push", "rebase", "reflog", "remote", "reset",
    "restore", "revert", "rm", "stash", "submodule", "switch", "tag", "worktree",
})
_PACKAGE_WRITE_COMMANDS = frozenset({
    "apt", "apt-get", "brew", "cargo", "composer", "dnf", "gem", "go",
    "npm", "npx", "pip", "pip3", "pnpm", "poetry", "rpm", "uv", "yarn", "yum",
})
_PACKAGE_READ_ACTIONS = frozenset({
    "check", "diff", "help", "info", "list", "ls", "outdated", "search", "show",
    "tree", "version", "view", "why",
})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SED_WRITE_RE = re.compile(r"^-[A-Za-z]*i(?:[A-Za-z]*|$)")
_PERL_WRITE_RE = re.compile(r"^-[A-Za-z]*[ip][A-Za-z]*$")


def decode_tool_arguments(arguments: Any) -> Any:
    """Decode provider JSON arguments when possible without raising."""

    if not isinstance(arguments, str):
        return arguments
    stripped = arguments.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except Exception:
        return arguments


def _argument(arguments: Any, *names: str) -> Any:
    decoded = decode_tool_arguments(arguments)
    if not isinstance(decoded, Mapping):
        return None
    for name in names:
        if name in decoded:
            return decoded[name]
    # Older persisted bash calls used ``cmd``. It still carries the same
    # presentation semantics as the current ``script`` argument.
    if "script" in names and "cmd" in decoded:
        return decoded["cmd"]
    return None


def _basename(word: str) -> str:
    return str(word or "").rsplit("/", 1)[-1].casefold()


def _shell_tokens(script: str) -> tuple[str, ...] | None:
    """Tokenize enough shell syntax for a conservative display heuristic."""

    if not isinstance(script, str) or not script.strip():
        return ()
    # Heredoc bodies are shell programs' data, not commands. Rather than trying
    # to implement a shell parser, classify from the declaring command itself.
    # A known writer such as applypatch is still caught before this abstention.
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars="|&;()<>")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return tuple(lexer)
    except (TypeError, ValueError):
        return None


def _strip_command_prefix(words: list[str]) -> list[str]:
    while words and _ENV_ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    while words and _basename(words[0]) in _WRAPPERS:
        wrapper = _basename(words.pop(0))
        if wrapper == "env":
            while words and (words[0].startswith("-") or _ENV_ASSIGNMENT_RE.match(words[0])):
                words.pop(0)
        elif wrapper == "sudo":
            while words and words[0].startswith("-"):
                words.pop(0)
        elif wrapper == "time":
            while words and words[0].startswith("-"):
                words.pop(0)
        while words and _ENV_ASSIGNMENT_RE.match(words[0]):
            words.pop(0)
    return words


def _git_subcommand(words: list[str]) -> str:
    index = 1
    options_with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-"):
            break
        if word in options_with_value:
            index += 2
        else:
            index += 1
    return words[index].casefold() if index < len(words) else ""


def _classify_git(words: list[str]) -> ToolEffectClassification:
    subcommand = _git_subcommand(words)
    if not subcommand:
        return ToolEffectClassification(ToolEffect.READ, "git help")
    if subcommand == "branch":
        write_flags = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--edit-description", "--set-upstream-to", "--unset-upstream"}
        return ToolEffectClassification(
            ToolEffect.MAY_WRITE if any(word in write_flags for word in words[2:]) else ToolEffect.READ,
            f"git {subcommand}",
        )
    if subcommand == "tag":
        read_only = len(words) == 2 or any(word in {"-l", "--list", "--contains", "--points-at"} for word in words[2:])
        return ToolEffectClassification(ToolEffect.READ if read_only else ToolEffect.MAY_WRITE, f"git {subcommand}")
    if subcommand == "remote":
        action = next((word.casefold() for word in words[2:] if not word.startswith("-")), "")
        read_only = action in {"", "get-url", "show", "-v"}
        return ToolEffectClassification(ToolEffect.READ if read_only else ToolEffect.MAY_WRITE, f"git remote {action}".strip())
    if subcommand in _GIT_WRITE_SUBCOMMANDS:
        return ToolEffectClassification(ToolEffect.MAY_WRITE, f"git {subcommand}")
    if subcommand in _GIT_READ_SUBCOMMANDS:
        return ToolEffectClassification(ToolEffect.READ, f"git {subcommand}")
    return ToolEffectClassification(ToolEffect.UNKNOWN, f"unrecognized git {subcommand}")


def _classify_command(words: Iterable[str]) -> ToolEffectClassification:
    command_words = _strip_command_prefix(list(words))
    if not command_words:
        return ToolEffectClassification(ToolEffect.UNKNOWN, "missing command")
    command = _basename(command_words[0])

    if command in _WRITE_COMMANDS:
        return ToolEffectClassification(ToolEffect.MAY_WRITE, command)
    if command == "git":
        return _classify_git(command_words)
    if command == "find":
        write_actions = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        if any(word in write_actions for word in command_words[1:]):
            return ToolEffectClassification(ToolEffect.MAY_WRITE, "find write/execute action")
        return ToolEffectClassification(ToolEffect.READ, "find")
    if command == "awk":
        source = " ".join(command_words[1:]).casefold()
        if "system(" in source or "getline" in source or re.search(r">\s*[^=]", source):
            return ToolEffectClassification(ToolEffect.UNKNOWN, "awk can execute/write")
        return ToolEffectClassification(ToolEffect.READ, "awk")
    if command == "sed":
        if any(_SED_WRITE_RE.match(word) for word in command_words[1:]):
            return ToolEffectClassification(ToolEffect.MAY_WRITE, "sed in-place")
        return ToolEffectClassification(ToolEffect.READ, "sed without in-place editing")
    if command == "perl":
        if any(_PERL_WRITE_RE.match(word) for word in command_words[1:] if word.startswith("-")):
            return ToolEffectClassification(ToolEffect.MAY_WRITE, "perl in-place")
        return ToolEffectClassification(ToolEffect.UNKNOWN, "arbitrary perl")
    if command in _PACKAGE_WRITE_COMMANDS:
        action = next((word.casefold() for word in command_words[1:] if not word.startswith("-")), "")
        if action in _PACKAGE_READ_ACTIONS:
            return ToolEffectClassification(ToolEffect.READ, f"{command} {action}")
        return ToolEffectClassification(ToolEffect.MAY_WRITE, f"{command} {action}".strip())
    if command in _READ_COMMANDS:
        return ToolEffectClassification(ToolEffect.READ, command)
    return ToolEffectClassification(ToolEffect.UNKNOWN, f"unrecognized command {command}")


def classify_bash_script(script: Any) -> ToolEffectClassification:
    """Classify a shell script; mutation wins, uncertainty beats READ."""

    if not isinstance(script, str) or not script.strip():
        return ToolEffectClassification(ToolEffect.UNKNOWN, "missing script")
    tokens = _shell_tokens(script)
    if tokens is None:
        return ToolEffectClassification(ToolEffect.UNKNOWN, "shell parse failed")

    # Output redirects are writes. Input redirects and stderr-to-stdout are not.
    if any(token in _WRITE_REDIRECTIONS for token in tokens):
        # `2>&1` changes only file descriptors, while `> file` changes a file.
        for index, token in enumerate(tokens):
            if token not in _WRITE_REDIRECTIONS:
                continue
            before = tokens[index - 1] if index else ""
            after = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token == ">&" and before.isdigit() and after.isdigit():
                continue
            return ToolEffectClassification(ToolEffect.MAY_WRITE, "shell output redirection")

    commands: list[list[str]] = []
    current: list[str] = []
    heredoc = False
    for token in tokens:
        if token == "<<":
            heredoc = True
            break
        if token in _SHELL_SEPARATORS:
            if current:
                commands.append(current)
                current = []
            continue
        if token in _READ_REDIRECTIONS:
            continue
        current.append(token)
    if current:
        commands.append(current)
    if not commands:
        return ToolEffectClassification(ToolEffect.UNKNOWN, "no commands")

    outcomes = tuple(_classify_command(command) for command in commands)
    write = next((outcome for outcome in outcomes if outcome.effect is ToolEffect.MAY_WRITE), None)
    if write is not None:
        return write
    unknown = next((outcome for outcome in outcomes if outcome.effect is ToolEffect.UNKNOWN), None)
    if unknown is not None:
        return unknown
    reason = "read-only shell pipeline" if len(commands) > 1 else outcomes[0].reason
    if heredoc and outcomes[0].effect is ToolEffect.READ:
        return ToolEffectClassification(ToolEffect.UNKNOWN, "heredoc with non-writer")
    return ToolEffectClassification(ToolEffect.READ, reason)


def classify_tool_effect(tool_name: Any, arguments: Any = None) -> ToolEffectClassification:
    """Return a conservative, advisory side-effect class for one tool call."""

    name = str(tool_name or "").strip().casefold()
    if name == "bash":
        return classify_bash_script(_argument(arguments, "script", "command", "code"))
    if name in _READ_ONLY_TOOLS:
        return ToolEffectClassification(ToolEffect.READ, "known read-only tool")
    if name in _MAY_WRITE_TOOLS:
        return ToolEffectClassification(ToolEffect.MAY_WRITE, "known artifact/file writer")
    if name in _UNKNOWN_EXECUTION_TOOLS:
        return ToolEffectClassification(ToolEffect.UNKNOWN, "arbitrary code execution")
    return ToolEffectClassification(ToolEffect.UNKNOWN, "unclassified tool")


__all__ = [
    "ToolEffect",
    "ToolEffectClassification",
    "classify_bash_script",
    "classify_tool_effect",
    "decode_tool_arguments",
]
