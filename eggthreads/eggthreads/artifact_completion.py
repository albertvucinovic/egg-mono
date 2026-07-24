from __future__ import annotations

"""Shared completion helpers for user commands that reference artifacts."""

import os
import re
import shlex
from pathlib import Path
from typing import Any, Mapping


PROVIDER_ARTIFACT_COMMANDS = frozenset(
    {
        "/attachOutput",
        "/saveProviderArtifact",
        "/saveProviderOutput",
    }
)


def artifact_workspace_from_db(db: Any | None = None, *, fallback: str | Path | None = None) -> Path:
    """Return the workspace root used for Egg artifact storage."""

    try:
        db_path = Path(getattr(db, "path")).expanduser().resolve()
        if db_path.parent.name == ".egg":
            return db_path.parent.parent
    except Exception:
        pass
    if fallback is not None:
        return Path(fallback).expanduser().resolve()
    return Path.cwd().resolve()


def split_command_arg_tokens(arg: str) -> list[str]:
    try:
        return shlex.split(str(arg or ""))
    except Exception:
        return str(arg or "").split()


def is_provider_artifact_id_position(command: str, arg: str) -> bool:
    """Return True when ``arg`` is completing a provider artifact id."""

    if command not in PROVIDER_ARTIFACT_COMMANDS:
        return False
    text = str(arg or "")
    tokens = split_command_arg_tokens(text)
    if command == "/attachOutput":
        # /attachOutput takes exactly one artifact id.  After a completed first
        # arg, there is no second positional completion to offer.
        return len(tokens) == 0 or (len(tokens) == 1 and not text.endswith((" ", "\t")))
    if command in {"/saveProviderArtifact", "/saveProviderOutput"}:
        # /saveProviderArtifact <artifact_id> [path]
        # If the first argument is complete and the user is typing the export
        # path, callers should offer filesystem completions instead.
        return len(tokens) == 0 or (len(tokens) == 1 and not text.endswith((" ", "\t")))
    return False


def is_provider_artifact_export_path_position(command: str, arg: str) -> bool:
    if command not in {"/saveProviderArtifact", "/saveProviderOutput"}:
        return False
    text = str(arg or "")
    tokens = split_command_arg_tokens(text)
    return len(tokens) >= 2 or (len(tokens) == 1 and text.endswith((" ", "\t")))


def format_artifact_size(size_bytes: Any) -> str:
    try:
        size = int(size_bytes)
    except Exception:
        return "unknown size"
    if size < 0:
        return "unknown size"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} B"
    if value >= 100:
        rendered = f"{value:.0f}"
    elif value >= 10:
        rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered} {units[unit_index]}"


def format_provider_artifact_completion_display(metadata: Mapping[str, Any]) -> str:
    artifact_id = str(metadata.get("artifact_id") or "")
    filename = str(metadata.get("filename") or artifact_id or "artifact")
    presentation = str(metadata.get("presentation") or "file")
    mime_type = str(metadata.get("mime_type") or "application/octet-stream")
    created = str(metadata.get("created_at") or "")
    created_part = f" {created[:19]}" if created else ""
    return f"{artifact_id}  {presentation} {filename} ({mime_type}, {format_artifact_size(metadata.get('size_bytes'))}){created_part}"


def provider_artifact_completion_items(
    workspace: str | Path | None,
    db: Any,
    thread_id: str | None,
    fragment: str,
    *,
    limit: int = 50,
) -> list[dict[str, str]]:
    """Return provider-output artifact-id suggestions for autocomplete UIs."""

    tid = str(thread_id or "").strip()
    if not tid or db is None:
        return []
    try:
        from .provider_output_artifacts import list_provider_output_artifact_metadata

        records = list_provider_output_artifact_metadata(workspace, db, tid, limit=limit * 2)
    except Exception:
        return []

    frag = str(fragment or "")
    frag_l = frag.lower()
    out: list[dict[str, str]] = []
    for metadata in records:
        artifact_id = str(metadata.get("artifact_id") or "")
        if not artifact_id:
            continue
        hay = " ".join(
            str(metadata.get(key) or "")
            for key in ("artifact_id", "filename", "mime_type", "presentation", "created_at")
        ).lower()
        if frag_l and frag_l not in hay:
            continue
        item: dict[str, str] = {
            "display": format_provider_artifact_completion_display(metadata),
            "insert": artifact_id,
        }
        if frag:
            item["replace"] = str(len(frag))
        out.append(item)
        if len(out) >= limit:
            break
    return out


def current_completion_token(text: str) -> str:
    """Return the shell-like token at the cursor, retaining quotes and ``@``."""

    value = str(text or "")
    start = 0
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char.isspace():
            start = index + 1
    return value[start:]


def _completion_path_token(raw_token: str) -> tuple[str, str]:
    marker = "@" if raw_token.startswith("@") else ""
    token = raw_token[len(marker):]
    if token[:1] in {"'", '"'}:
        quote = token[0]
        token = token[1:]
        if token.endswith(quote):
            token = token[:-1]
    token = re.sub(r"\\([\\\s'\"])", r"\1", token)
    return marker, token


def _quote_completion_path(path: str) -> str:
    return shlex.quote(path) if any(char.isspace() or char in "'\"\\" for char in path) else path


def filesystem_completion_items(
    token: str,
    *,
    limit: int = 50,
    working_dir: str | Path | None = None,
) -> list[dict[str, str]]:
    """Return filesystem completion items relative to ``working_dir``.

    This is intentionally UI-neutral and shared by Egg/EggW. Relative inserts
    are explicit (``./``), whitespace is shell-quoted, a leading ``@`` is
    preserved, directories end in ``/``, and only the typed token is replaced.
    """

    raw_token = str(token or "")
    marker, path_token = _completion_path_token(raw_token)
    expanded = os.path.expanduser(path_token)
    root = Path.cwd() if working_dir is None else Path(working_dir).expanduser()
    lookup = Path(expanded) if os.path.isabs(expanded) else root / expanded
    base_dir = lookup
    needle = ""
    if not base_dir.is_dir():
        needle = base_dir.name
        base_dir = base_dir.parent
    try:
        if not base_dir.is_dir():
            return []
        entries = os.listdir(base_dir)
    except Exception:
        return []

    items: list[dict[str, str]] = []
    for name in sorted(entries):
        if needle and not name.startswith(needle):
            continue
        path = base_dir / name
        suffix = "/" if path.is_dir() else ""
        typed_parent = os.path.dirname(path_token)
        if typed_parent:
            inserted_path = os.path.join(typed_parent, name) + suffix
        elif os.path.isabs(expanded):
            inserted_path = str(path) + suffix
        else:
            inserted_path = "./" + name + suffix
        if path_token.startswith("./") and not inserted_path.startswith("./"):
            inserted_path = "./" + inserted_path
        item: dict[str, str] = {
            "display": name + suffix,
            "insert": marker + _quote_completion_path(inserted_path),
        }
        if raw_token:
            item["replace"] = str(len(raw_token))
        item["meta"] = "directory" if path.is_dir() else "file"
        items.append(item)
        if len(items) >= limit:
            break
    return items


__all__ = [
    "PROVIDER_ARTIFACT_COMMANDS",
    "artifact_workspace_from_db",
    "current_completion_token",
    "filesystem_completion_items",
    "format_artifact_size",
    "format_provider_artifact_completion_display",
    "is_provider_artifact_export_path_position",
    "is_provider_artifact_id_position",
    "provider_artifact_completion_items",
    "split_command_arg_tokens",
]
