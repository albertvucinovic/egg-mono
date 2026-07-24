from __future__ import annotations

"""Reusable, UI-neutral completion sources composed by Egg frontends."""

import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .api import get_thread_statuses_bulk, get_thread_working_directory, list_threads
from .artifact_completion import current_completion_token, filesystem_completion_items
from .inspection import list_show_record_candidates

GLOBAL_ID_MIN_CHARS = 3
_COMPLETION_CACHE_LIMIT = 8
_RECORD_CACHE_STABLE_AFTER_SEC = 0.25
_record_cache: "OrderedDict[tuple[str, str, int], tuple[dict[str, str], ...]]" = OrderedDict()
_record_cache_lock = threading.Lock()
_record_cache_pending: dict[tuple[str, str, int], float] = {}


def _match_count(value: str) -> int:
    return sum(character.isalnum() for character in value)


def clear_completion_cache() -> None:
    """Drop process-local completion acceleration state."""

    with _record_cache_lock:
        _record_cache.clear()
        _record_cache_pending.clear()


def _db_identity(db: Any) -> str:
    try:
        return str(Path(db.path).expanduser().resolve())
    except Exception:
        return f"memory:{id(db)}"


def _record_catalog(db: Any, thread_id: str) -> tuple[dict[str, str], ...]:
    get_metadata = getattr(db, "get_thread_metadata", None)
    thread = get_metadata(thread_id) if callable(get_metadata) else db.get_thread(thread_id)
    snapshot_seq = int(getattr(thread, "snapshot_last_event_seq", -1)) if thread is not None else -1
    semantic_seq = -1
    for event_type in (
        "msg.create",
        "msg.edit",
        "msg.delete",
        "control.interrupt",
        "tool_call.output_approval",
    ):
        row = db.conn.execute(
            "SELECT MAX(event_seq) FROM events INDEXED BY events_thread_type "
            "WHERE thread_id=? AND type=?",
            (thread_id, event_type),
        ).fetchone()
        if row and row[0] is not None:
            semantic_seq = max(semantic_seq, int(row[0]))
    key = (_db_identity(db), str(thread_id), max(snapshot_seq, semantic_seq))
    now = time.monotonic()
    with _record_cache_lock:
        cached = _record_cache.get(key)
        if cached is not None:
            _record_cache.move_to_end(key)
            return cached
        _record_cache_pending.setdefault(key, now)
    items = []
    for candidate in list_show_record_candidates(db, thread_id):
        short_id = candidate.record_id[-8:] if len(candidate.record_id) > 8 else candidate.record_id
        preview = f" · {candidate.preview}" if candidate.preview else ""
        items.append({
            "record_id": candidate.record_id,
            "display": f"[{short_id}] {candidate.label}{preview}",
            "meta": f"{candidate.kind} · {candidate.record_id}",
        })
    value = tuple(items)
    # A streaming provider can append the assistant declaration and its tool
    # result in separate commits. Cache only after this semantic watermark has
    # remained stable briefly so the first partial catalog cannot be reused as
    # if it represented the completed write burst.
    with _record_cache_lock:
        stable_since = _record_cache_pending.get(key)
        if stable_since is None or time.monotonic() - stable_since < _RECORD_CACHE_STABLE_AFTER_SEC:
            return value
        for pending_key in [
            pending_key
            for pending_key in _record_cache_pending
            if pending_key[:2] == key[:2] and pending_key != key
        ]:
            _record_cache_pending.pop(pending_key, None)
        for stale_key in [
            cached_key
            for cached_key in _record_cache
            if cached_key[:2] == key[:2] and cached_key != key
        ]:
            _record_cache.pop(stale_key, None)
            _record_cache_pending.pop(stale_key, None)
        _record_cache[key] = value
        _record_cache_pending.pop(key, None)
        _record_cache.move_to_end(key)
        while len(_record_cache) > _COMPLETION_CACHE_LIMIT:
            _record_cache.popitem(last=False)
    return value


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
    exact = []
    matches = []
    for item in _record_catalog(db, thread_id):
        record_id = item["record_id"]
        if record_id == wanted:
            exact.append(item)
        elif record_id.startswith(wanted) or record_id.endswith(wanted):
            matches.append(item)
    selected = exact or matches
    replace_count = matched_length if replace is None else max(0, int(replace))
    return [
        {
            "display": item["display"],
            "insert": item["record_id"],
            "replace": str(replace_count),
            "meta": item["meta"],
        }
        for item in selected[: max(0, int(limit))]
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
        rows = list_threads(db)
        rows.sort(key=lambda row: getattr(row, "created_at", "") or "", reverse=True)
    except Exception:
        rows = []
    try:
        statuses = get_thread_statuses_bulk(
            db,
            [str(getattr(row, "thread_id", "") or "") for row in rows],
            skip_runnability=not include_streaming,
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
