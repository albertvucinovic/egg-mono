from __future__ import annotations

"""Shared source parsing and record preparation for the human ``/editor`` UI."""

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .content_parts import content_to_plain_text
from .inspection import ShowRecordCandidate, ShowRecordResolution, resolve_show_record

EditorSourceMode = Literal["draft", "record", "file", "file_draft", "ambiguous"]
EDITOR_DRAFT_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class EditorSource:
    mode: EditorSourceMode
    draft: str = ""
    path: Path | None = None
    record_id: str = ""
    source_label: str = "input message"
    source_suffix: str = ""
    resolution: ShowRecordResolution | None = None


def _parse_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid quoting: {exc}") from exc


def _explicit_path_token(raw: str, token: str) -> bool:
    quoted = raw[:1] in {"'", '"'}
    return (
        Path(token).expanduser().is_absolute()
        or raw.startswith(("./", "../", "~/"))
        or "/" in token
        or quoted
        or ("\\" in raw and any(char.isspace() for char in token))
    )


def _record_candidate_text(candidate: ShowRecordCandidate) -> str:
    """Return a neutral editable representation of one ``/show`` record."""

    if candidate.kind == "tool_declaration":
        return json.dumps(dict(candidate.tool_call or {}), ensure_ascii=False, indent=2)
    return content_to_plain_text(candidate.message.get("content"))


def _resolve_editor_path(path: str | Path, working_dir: str | Path) -> Path:
    """Resolve a human editor path relative to one thread working directory."""

    raw = Path(path).expanduser()
    base = Path(working_dir).expanduser().resolve()
    resolved = (raw if raw.is_absolute() else base / raw).resolve(strict=False)
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"editor target is not a regular file: {resolved}")
    return resolved


def _validate_editor_file_target(path: Path) -> None:
    if path.exists() and not path.is_file():
        raise ValueError(f"editor target is not a regular file: {path}")
    if not path.exists() and not path.parent.is_dir():
        raise ValueError(f"parent directory does not exist: {path.parent}")


def read_editor_draft_file(path: Path) -> str:
    """Read one bounded UTF-8 text file for ``@file`` draft mode."""

    before = path.stat()
    with path.open("rb") as handle:
        data = handle.read(EDITOR_DRAFT_MAX_BYTES + 1)
    if len(data) > EDITOR_DRAFT_MAX_BYTES:
        raise ValueError(
            f"source file is too large for an editor draft ({len(data)} bytes; limit {EDITOR_DRAFT_MAX_BYTES})"
        )
    if b"\x00" in data:
        raise ValueError("binary files cannot be copied into an editor draft")
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or after.st_size != len(data):
        raise ValueError("source file changed while it was being opened; try again")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source file is not valid UTF-8") from exc


def resolve_editor_source(db: Any, thread_id: str, arg: str, working_dir: str | Path) -> EditorSource:
    """Resolve ``/editor`` arguments without applying model-tool sandbox policy."""

    text = str(arg or "").strip()
    base = Path(working_dir).expanduser().resolve()
    if not text:
        return EditorSource("draft")
    if text == "--":
        return EditorSource("draft")
    if text.startswith("-- "):
        return EditorSource("draft", draft=text[3:])

    file_draft = text.startswith("@")
    parsed_text = text[1:] if file_draft else text
    if file_draft and not parsed_text.strip():
        raise ValueError("@file mode requires a path")
    quoted_path = parsed_text[:1] in {"'", '"'}
    tokens = _parse_tokens(parsed_text)
    if file_draft:
        if len(tokens) != 1 or not tokens[0]:
            raise ValueError("@file mode requires exactly one path; quote or escape spaces in filenames.")
        path = _resolve_editor_path(tokens[0], base)
        _validate_editor_file_target(path)
        if not path.is_file():
            raise ValueError(f"source path is not a regular file: {path}")
        return EditorSource(
            "file_draft",
            path=path,
            source_label="file",
            source_suffix=path.name,
        )

    if len(tokens) == 1:
        token = tokens[0]
        resolution = resolve_show_record(db, thread_id, token)
        if resolution.status == "selected" and resolution.selected is not None:
            selected = resolution.selected
            return EditorSource(
                "record",
                draft=_record_candidate_text(selected),
                record_id=selected.record_id,
                source_label=selected.label,
                source_suffix=selected.record_id[-8:],
                resolution=resolution,
            )
        if resolution.status == "ambiguous":
            return EditorSource("ambiguous", resolution=resolution)

        path = _resolve_editor_path(token, base)
        if path.exists() or quoted_path or _explicit_path_token(parsed_text, token):
            _validate_editor_file_target(path)
            return EditorSource("file", path=path, source_label="file", source_suffix=path.name)

    return EditorSource("draft", draft=text)


__all__ = [
    "EditorSource",
    "EditorSourceMode",
    "EDITOR_DRAFT_MAX_BYTES",
    "read_editor_draft_file",
    "resolve_editor_source",
]
