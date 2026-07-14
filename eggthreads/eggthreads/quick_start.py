from __future__ import annotations

"""Shared interpretation of launcher arguments as an unsent composer draft."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


@dataclass(frozen=True)
class QuickStartRequest:
    """A launcher request to prefill text or stage one existing local file."""

    kind: Literal["draft", "attachment"]
    draft: str = ""
    source_path: Path | None = None


def quick_start_args_from_json(raw: str | None) -> list[str]:
    """Decode the launcher's JSON argv without accepting coerced values."""

    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return list(value)


def parse_quick_start_args(
    args: Iterable[str],
    *,
    cwd: str | Path | None = None,
) -> QuickStartRequest | None:
    """Interpret positional launcher arguments without sending a message.

    Multiple shell arguments are joined with one space, matching their normal
    command-line word boundaries while retaining whitespace inside each quoted
    argument. A sole existing regular file is represented as an attachment
    request so clients can route it through their canonical staging machinery
    instead of reading it into the text draft.
    """

    values = [str(value) for value in args]
    if not values:
        return None

    if len(values) == 1 and values[0]:
        base = Path.cwd() if cwd is None else Path(cwd).expanduser()
        raw = Path(values[0]).expanduser()
        candidate = raw if raw.is_absolute() else base / raw
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved = None
        if resolved is not None and resolved.is_file():
            return QuickStartRequest(kind="attachment", source_path=resolved)

    return QuickStartRequest(kind="draft", draft=" ".join(values))


__all__ = ["QuickStartRequest", "parse_quick_start_args", "quick_start_args_from_json"]
