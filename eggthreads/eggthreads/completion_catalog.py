from __future__ import annotations

"""Reusable, UI-neutral completion sources composed by Egg frontends."""

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .api import get_thread_statuses_bulk, get_thread_working_directory, list_threads
from .artifact_completion import current_completion_token, filesystem_completion_items
from .autocomplete_sidecar import query_autocomplete_records

GLOBAL_ID_MIN_CHARS = 3
def _match_count(value: str) -> int:
    return sum(character.isalnum() for character in value)


def clear_completion_cache() -> None:
    """Compatibility no-op; the shared sidecar has no process-local catalog."""


def _identity_fragment(raw_token: str) -> tuple[str, int]:
    token = str(raw_token or "")
    if token.startswith("@"):
        return "", 0
    match = re.search(r"([A-Za-z0-9_-]+)$", token)
    return (match.group(1), len(match.group(1))) if match else ("", 0)


def record_id_completion_items(
    db: Any,
    thread_id: str,
    fragment: str,
    *,
    replace: int | None = None,
    limit: int = 20,
) -> list[dict[str, str]]:
    wanted, matched_length = _identity_fragment(fragment)
    if _match_count(wanted) < GLOBAL_ID_MIN_CHARS:
        return []
    page = query_autocomplete_records(db, thread_id, wanted, limit=limit)
    if page.state != "ready":
        return []
    replace_count = matched_length if replace is None else max(0, int(replace))
    return [
        {
            "display": (
                f"[{record.record_id[-8:] if len(record.record_id) > 8 else record.record_id}] "
                f"{record.label}{f' · {record.preview}' if record.preview else ''}"
            ),
            "insert": record.record_id,
            "replace": str(replace_count),
            "meta": f"{record.kind} · {record.record_id}",
        }
        for record in page.records
    ]


def thread_completion_items(
    db: Any,
    fragment: str,
    *,
    current_thread: str | None = None,
    replace: int | None = None,
    limit: int = 20,
    match_metadata: bool = False,
    include_empty: bool = False,
    include_streaming: bool = False,
    streaming_thread_ids: Iterable[str] = (),
) -> list[dict[str, str]]:
    wanted, matched_length = _identity_fragment(fragment)
    if not wanted and not include_empty:
        return []
    minimum = 1 if match_metadata else GLOBAL_ID_MIN_CHARS
    if wanted and _match_count(wanted) < minimum:
        return []
    wanted_l = wanted.lower()
    try:
        if wanted:
            pattern = f"%{wanted.lower()}%"
            if match_metadata:
                sql = (
                    "SELECT thread_id,name,short_recap,status,NULL AS snapshot_json,"
                    "snapshot_last_event_seq,initial_model_key,depth,created_at FROM threads "
                    "WHERE lower(thread_id) LIKE ? OR lower(coalesce(name,'')) LIKE ? "
                    "OR lower(coalesce(short_recap,'')) LIKE ? ORDER BY created_at DESC LIMIT ?"
                )
                params = (pattern, pattern, pattern, max(0, int(limit)))
            else:
                sql = (
                    "SELECT thread_id,name,short_recap,status,NULL AS snapshot_json,"
                    "snapshot_last_event_seq,initial_model_key,depth,created_at FROM threads "
                    "WHERE lower(thread_id) LIKE ? ORDER BY created_at DESC LIMIT ?"
                )
                params = (pattern, max(0, int(limit)))
            from .db import ThreadRow

            rows = [ThreadRow(**dict(row)) for row in db.conn.execute(sql, params).fetchall()]
        else:
            rows = list_threads(db)
            rows.sort(key=lambda row: getattr(row, "created_at", "") or "", reverse=True)
            rows = rows[: max(0, int(limit))]
    except Exception:
        rows = []
    # Autocomplete needs exact live lease state, not full runner-actionability
    # discovery for every thread in the database. The latter can project every
    # long thread before we have even filtered or limited candidates.
    try:
        statuses = get_thread_statuses_bulk(
            db,
            [str(getattr(row, "thread_id", "") or "") for row in rows],
            skip_runnability=True,
        )
    except Exception:
        statuses = {}
    replace_count = matched_length if replace is None else max(0, int(replace))
    live_threads = {str(thread_id) for thread_id in streaming_thread_ids}
    out = []
    for row in rows:
        thread_id = str(getattr(row, "thread_id", "") or "")
        name = str(getattr(row, "name", "") or "")
        recap = str(getattr(row, "short_recap", "") or "")
        hay = f"{thread_id} {name} {recap}".lower() if match_metadata else thread_id.lower()
        if wanted_l and wanted_l not in hay:
            continue
        status = str(statuses.get(thread_id) or getattr(row, "status", "") or "unknown")
        streaming = status == "streaming" or thread_id in live_threads
        tags = [thread_id[-8:], f"<{status}>"]
        if current_thread and thread_id == current_thread:
            tags.insert(0, "[CUR]")
        if include_streaming:
            if streaming:
                tags.insert(0, "[STREAMING]")
        if name:
            tags.append(f"({name})")
        if recap:
            tags.append(f"- {recap}")
        item = {
            "display": " ".join(tags),
            "insert": thread_id,
            "meta": f"thread · {thread_id}",
        }
        if replace_count:
            item["replace"] = str(replace_count)
        out.append(item)
        if len(out) >= max(0, int(limit)):
            break
    return out


def global_completion_items(
    db: Any,
    thread_id: str | None,
    line_before_cursor: str,
    *,
    include_filesystem: bool = True,
    limit: int = 50,
) -> list[dict[str, str]]:
    """Return filesystem plus record/thread-ID suggestions for any input context."""

    token = current_completion_token(line_before_cursor)
    out: list[dict[str, str]] = []
    if include_filesystem and token and not line_before_cursor.lstrip().startswith("$"):
        try:
            working_dir = get_thread_working_directory(db, thread_id) if db is not None and thread_id else Path.cwd()
        except Exception:
            working_dir = Path.cwd()
        marker = "@" if token.startswith("@") else ""
        path_token = token[len(marker):]
        explicit_path = marker == "@" or path_token.startswith(("./", "../", "~/", "/", "'", '"'))
        command_context = line_before_cursor.lstrip().startswith("/")
        if explicit_path or len(path_token) >= GLOBAL_ID_MIN_CHARS or (command_context and len(path_token) >= 2):
            out.extend(filesystem_completion_items(token, working_dir=working_dir, limit=limit))
    if db is not None and thread_id:
        out.extend(record_id_completion_items(db, thread_id, token, limit=limit))
    if db is not None:
        out.extend(thread_completion_items(db, token, current_thread=thread_id, limit=limit))
    return merge_completion_items(out, limit=limit)


def merge_completion_items(
    *groups: Iterable[str | Mapping[str, Any]],
    limit: int = 50,
) -> list[dict[str, str]]:
    """Stable-deduplicate completion groups while preserving source priority."""

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for raw in group or ():
            if isinstance(raw, str):
                item = {"display": raw, "insert": raw}
            else:
                item = {
                    key: (int(value) if key == "replace" else str(value))
                    for key, value in dict(raw).items()
                    if value is not None and key in {"display", "insert", "replace", "meta"}
                }
            insert = item.get("insert", "")
            if not insert:
                continue
            key = (insert, item.get("replace", ""))
            if key in seen:
                continue
            seen.add(key)
            item.setdefault("display", insert)
            out.append(item)
            if len(out) >= max(0, int(limit)):
                return out
    return out


__all__ = [
    "GLOBAL_ID_MIN_CHARS",
    "clear_completion_cache",
    "global_completion_items",
    "merge_completion_items",
    "record_id_completion_items",
    "thread_completion_items",
]
