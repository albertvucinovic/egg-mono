"""Conservative syntax hints for terminal tool presentation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Mapping
from xml.etree import ElementTree

from pygments.token import Token
from rich.style import Style
from rich.syntax import ANSISyntaxTheme, Syntax
from rich.text import Text

from eggthreads.output_optimizer.classify import (
    normalize_command_name,
    simple_bash_command_invocation,
)
from eggthreads.output_optimizer.filters.python_traceback import parse_python_traceback


_PYTHON_TOOLS = frozenset({"python", "python_exec", "python_repl"})
_BASH_TOOLS = frozenset({"bash", "bash_repl"})
_FILE_OUTPUT_COMMANDS = frozenset({"cat", "head", "tail", "sed"})
_FILENAME_LEXERS = {
    ".bash": "bash",
    ".c": "c",
    ".cfg": "ini",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".ini": "ini",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_DIFF_OLD_HEADER_RE = re.compile(r"^---\s+\S+")
_DIFF_NEW_HEADER_RE = re.compile(r"^\+\+\+\s+\S+")
_DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


@dataclass(frozen=True)
class SyntaxHint:
    """A high-confidence Pygments lexer decision and its evidence source."""

    lexer: str
    source: str


def semantic_syntax_theme(
    *,
    foreground: Style,
    muted: Style,
    accent: Style,
    string: Style,
    name: Style,
    number: Style,
    error: Style,
) -> ANSISyntaxTheme:
    """Build a transparent syntax palette from the active Egg theme."""

    return ANSISyntaxTheme({
        Token.Text: foreground,
        Token.Whitespace: foreground,
        Token.Comment: muted + Style(italic=True),
        Token.Keyword: accent + Style(bold=True),
        Token.Name: foreground,
        Token.Name.Builtin: accent,
        Token.Name.Function: name + Style(bold=True),
        Token.Name.Class: name + Style(bold=True),
        Token.Name.Tag: string,
        Token.Name.Attribute: accent,
        Token.Literal.String: string,
        Token.Literal.String.Double: string,
        Token.Literal.String.Single: string,
        Token.Literal.String.Doc: string,
        Token.Literal.String.Symbol: string,
        Token.Literal.Number: number,
        Token.Operator: accent,
        Token.Punctuation: muted,
        Token.Generic.Heading: accent + Style(bold=True),
        Token.Generic.Subheading: accent,
        Token.Generic.Inserted: name,
        Token.Generic.Deleted: error,
        Token.Generic.Error: error + Style(bold=True),
        Token.Error: error + Style(bold=True),
    })


def tool_argument_syntax_lexer(tool_name: Any, argument_name: Any) -> str | None:
    """Return the known language for an executable tool argument."""

    name = normalize_command_name(str(tool_name or ""))
    argument = str(argument_name or "").strip().lower()
    if name in _PYTHON_TOOLS and argument in {"code", "script"}:
        return "python"
    if name in _BASH_TOOLS and argument == "script":
        return "bash"
    return None


def decode_tool_arguments(arguments: Any) -> Mapping[str, Any]:
    """Return structured tool arguments without guessing malformed JSON."""

    if isinstance(arguments, Mapping):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        decoded = json.loads(arguments)
    except Exception:
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def infer_tool_output_syntax(
    tool_name: Any,
    tool_arguments: Any,
    output: Any,
    *,
    channel: str = "",
) -> SyntaxHint | None:
    """Infer a Bash result's syntax only from high-confidence evidence.

    Content signatures take precedence over the command. Command-derived hints
    are restricted to stdout/unsectioned output; stderr is too often diagnostic
    text unrelated to the command's normal output format.
    """

    name = normalize_command_name(str(tool_name or ""))
    if name not in _BASH_TOOLS:
        return None
    text = str(output or "").strip()
    if not text:
        return None

    signature = _content_syntax_hint(text)
    if signature is not None:
        return signature
    if str(channel or "").strip().lower() == "stderr":
        return None

    arguments = decode_tool_arguments(tool_arguments)
    script = arguments.get("script")
    words = simple_bash_command_invocation(script)
    if not words:
        return None
    command = normalize_command_name(words[0])

    if command == "jq" and _looks_like_json_stream(text):
        return SyntaxHint("json", "bash-command")
    if command in {"python", "python3"} and _invokes_json_tool(words) and _looks_like_json_stream(text):
        return SyntaxHint("json", "bash-command")

    if command in _FILE_OUTPUT_COMMANDS:
        lexer = _single_filename_lexer(words)
        if lexer is not None:
            return SyntaxHint(lexer, "filename")
    return None


def syntax_highlight_text(code: str, lexer: str, theme: Any) -> Text:
    """Return exact literal text decorated with Pygments/Rich token spans."""

    source = str(code or "")
    if not source or not lexer or theme is None:
        return Text(source)
    try:
        highlighted = Syntax(
            source,
            lexer,
            theme=theme,
            dedent=False,
            line_numbers=False,
            word_wrap=False,
            background_color=None,
            padding=0,
        ).highlight(source)
        # Pygments lexers conventionally emit one terminal newline when the
        # source does not already end in one. Remove exactly that synthetic
        # newline; never strip source whitespace.
        if not source.endswith("\n") and highlighted.plain == source + "\n":
            highlighted.remove_suffix("\n")
        if highlighted.plain != source:
            return Text(source)
        return highlighted
    except Exception:
        return Text(source)


def _content_syntax_hint(text: str) -> SyntaxHint | None:
    if parse_python_traceback(text) is not None:
        return SyntaxHint("pytb", "traceback-signature")
    if _looks_like_unified_diff(text):
        return SyntaxHint("diff", "diff-signature")
    if _looks_like_json_document(text):
        return SyntaxHint("json", "json-signature")
    if _looks_like_xml_document(text):
        return SyntaxHint("xml", "xml-signature")
    return None


def _looks_like_json_document(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        decoded = json.loads(stripped)
    except Exception:
        return False
    return isinstance(decoded, (dict, list))


def _looks_like_json_stream(text: str) -> bool:
    if _looks_like_json_document(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    try:
        return all(isinstance(json.loads(line), (dict, list, str, int, float, bool, type(None))) for line in lines)
    except Exception:
        return False


def _looks_like_xml_document(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("<") or not stripped.endswith(">"):
        return False
    # ElementTree resolves entities and accepts declarations Egg does not need
    # merely to color terminal text. Abstain on document types/entities rather
    # than turning syntax detection into an XML-processing surface.
    if re.search(r"<!DOCTYPE|<!ENTITY", stripped, flags=re.IGNORECASE):
        return False
    try:
        parser = ElementTree.XMLParser()
        ElementTree.fromstring(stripped, parser=parser)
    except (ElementTree.ParseError, ValueError):
        return False
    return True


def _looks_like_unified_diff(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    has_hunk = any(_DIFF_HUNK_RE.match(line) for line in lines)
    if not has_hunk:
        return False
    if any(line.startswith("diff --git ") for line in lines):
        return True
    old_indices = [index for index, line in enumerate(lines) if _DIFF_OLD_HEADER_RE.match(line)]
    return any(
        index + 1 < len(lines) and _DIFF_NEW_HEADER_RE.match(lines[index + 1])
        for index in old_indices
    )


def _invokes_json_tool(words: tuple[str, ...]) -> bool:
    return len(words) >= 3 and words[1:3] == ("-m", "json.tool")


def _single_filename_lexer(words: tuple[str, ...]) -> str | None:
    if not words:
        return None
    command = normalize_command_name(words[0])
    # ``sed`` expressions frequently end in source-looking fragments. Only
    # consider its final operand, which is the conventional input-file slot.
    tokens = words[-1:] if command == "sed" else words
    candidates: list[str] = []
    for word in tokens:
        token = str(word or "")
        if not token or token == "--" or token.startswith("-"):
            continue
        suffix = PurePath(token).suffix.lower()
        if suffix in _FILENAME_LEXERS:
            candidates.append(_FILENAME_LEXERS[suffix])
    return candidates[0] if len(candidates) == 1 else None


__all__ = [
    "SyntaxHint",
    "decode_tool_arguments",
    "infer_tool_output_syntax",
    "semantic_syntax_theme",
    "syntax_highlight_text",
    "tool_argument_syntax_lexer",
]
