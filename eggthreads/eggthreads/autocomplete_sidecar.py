from __future__ import annotations

"""Disposable, versioned full-history autocomplete projection sidecar.

The canonical ThreadsDB remains authoritative.  This module stores only derived
completion metadata and publishes complete per-thread generations atomically.
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Mapping, Optional, Sequence

from .content_parts import content_to_plain_text
from .inspection import (
    SHOW_PREVIEW_CHARS,
    ShowRecordCandidate,
    _message_kind,
    _message_label,
    _message_preview,
    _tool_call_id,
    _tool_call_parts,
    _tool_call_preview,
)
from .projection import load_thread_projection

AUTOCOMPLETE_SIDECAR_VERSION = 1
AUTOCOMPLETE_SIDECAR_FILENAME = f"autocomplete-v{AUTOCOMPLETE_SIDECAR_VERSION}.sqlite"
AUTOCOMPLETE_SIDECAR_BATCH_SIZE = 500
AUTOCOMPLETE_BUILD_LEASE_SECONDS = 120
AutocompleteOrder = Literal["newest", "oldest"]
AutocompleteCatalogState = Literal["ready", "preparing", "missing", "stale", "error"]


@dataclass(frozen=True)
class AutocompleteRecord:
    record_id: str
    kind: str
    message_id: str
    tool_call_id: str
    event_seq: int
    item_order: int
    label: str
    preview: str
    paired_message_ids: tuple[str, ...]


@dataclass(frozen=True)
class AutocompletePage:
    state: AutocompleteCatalogState
    thread_id: str
    through_event_seq: int = -1
    total: int = 0
    records: tuple[AutocompleteRecord, ...] = ()
    next_cursor: Optional[str] = None
    sidecar_path: Optional[Path] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class AutocompleteBuildResult:
    state: Literal["ready", "busy", "error"]
    thread_id: str
    through_event_seq: int = -1
    total: int = 0
    sidecar_path: Optional[Path] = None
    error: Optional[str] = None


def autocomplete_sidecar_path(db_path: Path | str) -> Path:
    """Return the v1 sidecar path uniquely derived from a canonical DB path."""

    canonical = Path(db_path).expanduser().resolve()
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
    return canonical.parent / "cache" / f"{canonical.stem}-{digest}-{AUTOCOMPLETE_SIDECAR_FILENAME}"


def _open_sidecar(path: Path, *, create: bool) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if create:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_schema(conn)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    else:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cache_manifest(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          semantic_version INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS thread_authority(
          thread_id TEXT PRIMARY KEY,
          active_generation TEXT,
          through_event_seq INTEGER NOT NULL DEFAULT -1,
          through_event_id TEXT,
          state TEXT NOT NULL DEFAULT 'missing',
          last_error TEXT,
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS build_leases(
          thread_id TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          target_event_seq INTEGER NOT NULL,
          lease_until REAL NOT NULL,
          started_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS completion_records(
          thread_id TEXT NOT NULL,
          generation TEXT NOT NULL,
          record_id TEXT NOT NULL,
          normalized_id TEXT NOT NULL,
          reversed_normalized_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          message_id TEXT NOT NULL,
          tool_call_id TEXT NOT NULL,
          event_seq INTEGER NOT NULL,
          item_order INTEGER NOT NULL,
          label TEXT NOT NULL,
          preview TEXT NOT NULL,
          search_text TEXT NOT NULL,
          paired_message_ids_json TEXT NOT NULL,
          PRIMARY KEY(thread_id, generation, record_id)
        );
        CREATE INDEX IF NOT EXISTS completion_records_identity
          ON completion_records(thread_id, generation, normalized_id);
        CREATE INDEX IF NOT EXISTS completion_records_suffix
          ON completion_records(thread_id, generation, reversed_normalized_id);
        CREATE INDEX IF NOT EXISTS completion_records_newest
          ON completion_records(thread_id, generation, event_seq DESC, item_order DESC, record_id);
        CREATE INDEX IF NOT EXISTS completion_records_oldest
          ON completion_records(thread_id, generation, event_seq ASC, item_order ASC, record_id);
        """
    )
    row = conn.execute("SELECT semantic_version FROM cache_manifest WHERE singleton=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO cache_manifest(singleton, semantic_version) VALUES (1, ?)",
            (AUTOCOMPLETE_SIDECAR_VERSION,),
        )
    elif int(row[0]) != AUTOCOMPLETE_SIDECAR_VERSION:
        raise RuntimeError(
            f"unsupported autocomplete sidecar version {row[0]} (expected {AUTOCOMPLETE_SIDECAR_VERSION})"
        )


def _validate_manifest(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT semantic_version FROM cache_manifest WHERE singleton=1").fetchone()
    if row is None or int(row[0]) != AUTOCOMPLETE_SIDECAR_VERSION:
        raise RuntimeError("incompatible autocomplete sidecar manifest")


def _canonical_anchor(db: Any, thread_id: str, event_seq: int) -> Optional[str]:
    if event_seq < 0:
        return None
    row = db.conn.execute(
        "SELECT event_id FROM events WHERE thread_id=? AND event_seq=?",
        (thread_id, int(event_seq)),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _authority_state(db: Any, conn: sqlite3.Connection, thread_id: str) -> tuple[AutocompleteCatalogState, Optional[sqlite3.Row]]:
    _validate_manifest(conn)
    row = conn.execute(
        "SELECT * FROM thread_authority WHERE thread_id=?", (thread_id,)
    ).fetchone()
    if row is None:
        return "missing", None
    state = str(row["state"] or "missing")
    if state != "ready" or not row["active_generation"]:
        return ("preparing" if state == "building" else "error" if state == "error" else "missing"), row
    if db.get_thread_metadata(thread_id) is None:
        return "stale", row
    if int(db.max_event_seq(thread_id)) != int(row["through_event_seq"]):
        return "stale", row
    current_id = _canonical_anchor(db, thread_id, int(row["through_event_seq"]))
    if current_id != row["through_event_id"]:
        return "stale", row
    return "ready", row


def _candidate_rows(projection: Any) -> list[tuple[Any, ...]]:
    rows: list[dict[str, Any]] = []
    declarations: dict[str, list[str]] = {}
    results: dict[str, list[str]] = {}
    item_order = 0
    for state in projection.messages:
        message = state.as_message_dict()
        msg_id = str(message.get("msg_id") or "")
        if not msg_id:
            continue
        kind = _message_kind(message)
        tool_call_id = str(message.get("tool_call_id") or "") if kind == "tool_result" else ""
        rows.append({
            "record_id": msg_id,
            "kind": kind,
            "message_id": msg_id,
            "tool_call_id": tool_call_id,
            "event_seq": int(state.created_event_seq),
            "item_order": item_order,
            "label": _message_label(message, kind),
            "preview": _message_preview(message),
            "search_text": " ".join((msg_id, str(message.get("role") or ""), content_to_plain_text(message.get("content")))).casefold(),
        })
        if tool_call_id:
            results.setdefault(tool_call_id, []).append(msg_id)
        item_order += 1
        tool_calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(tool_calls, list):
            continue
        for raw in tool_calls:
            if not isinstance(raw, Mapping):
                continue
            call_id = _tool_call_id(raw)
            if not call_id:
                continue
            name, _arguments = _tool_call_parts(raw)
            rows.append({
                "record_id": call_id,
                "kind": "tool_declaration",
                "message_id": msg_id,
                "tool_call_id": call_id,
                "event_seq": int(state.created_event_seq),
                "item_order": item_order,
                "label": f"Tool declaration: {name}",
                "preview": _tool_call_preview(raw),
                "search_text": " ".join((call_id, name, _tool_call_preview(raw))).casefold(),
            })
            declarations.setdefault(call_id, []).append(msg_id)
            item_order += 1
    out = []
    for row in rows:
        paired: Sequence[str] = ()
        if row["kind"] == "tool_declaration":
            paired = results.get(row["tool_call_id"], ())
        elif row["kind"] == "tool_result":
            paired = declarations.get(row["tool_call_id"], ())
        normalized = row["record_id"].casefold()
        out.append((
            row["record_id"], normalized, normalized[::-1], row["kind"],
            row["message_id"], row["tool_call_id"], row["event_seq"],
            row["item_order"], row["label"], row["preview"], row["search_text"],
            json.dumps(list(paired), ensure_ascii=False),
        ))
    return out


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(values), max(1, int(size))):
        yield values[index:index + max(1, int(size))]


def build_autocomplete_catalog(
    db: Any,
    thread_id: str,
    *,
    batch_size: int = AUTOCOMPLETE_SIDECAR_BATCH_SIZE,
    lease_seconds: int = AUTOCOMPLETE_BUILD_LEASE_SECONDS,
) -> AutocompleteBuildResult:
    """Build and atomically publish a complete generation for ``thread_id``."""

    thread_id = str(thread_id or "").strip()
    if not thread_id or db.get_thread_metadata(thread_id) is None:
        return AutocompleteBuildResult("error", thread_id, error="thread not found")
    path = autocomplete_sidecar_path(db.path)
    owner = uuid.uuid4().hex
    target = int(db.max_event_seq(thread_id))
    target_id = _canonical_anchor(db, thread_id, target)
    generation = uuid.uuid4().hex
    try:
        conn = _open_sidecar(path, create=True)
    except Exception as exc:
        return AutocompleteBuildResult("error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    try:
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT owner, lease_until FROM build_leases WHERE thread_id=?", (thread_id,)).fetchone()
        if current is not None and float(current["lease_until"]) > now:
            conn.execute("ROLLBACK")
            return AutocompleteBuildResult("busy", thread_id, through_event_seq=target, sidecar_path=path)
        conn.execute(
            "INSERT INTO build_leases(thread_id, owner, target_event_seq, lease_until, started_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET owner=excluded.owner,target_event_seq=excluded.target_event_seq,lease_until=excluded.lease_until,started_at=excluded.started_at",
            (thread_id, owner, target, now + max(1, int(lease_seconds)), now),
        )
        conn.execute(
            "INSERT INTO thread_authority(thread_id,state,last_error) VALUES (?,'building',NULL) "
            "ON CONFLICT(thread_id) DO UPDATE SET state='building',last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (thread_id,),
        )
        conn.execute("COMMIT")
        projection = load_thread_projection(db, thread_id, target)
        rows = _candidate_rows(projection)
        sql = (
            "INSERT INTO completion_records(thread_id,generation,record_id,normalized_id,reversed_normalized_id,kind,message_id,tool_call_id,event_seq,item_order,label,preview,search_text,paired_message_ids_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        for batch in _chunks(rows, batch_size):
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT owner FROM build_leases WHERE thread_id=?", (thread_id,)).fetchone()
            if lease is None or lease["owner"] != owner:
                raise RuntimeError("autocomplete catalog build lease lost")
            conn.executemany(sql, ((thread_id, generation, *row) for row in batch))
            conn.execute(
                "UPDATE build_leases SET lease_until=? WHERE thread_id=? AND owner=?",
                (time.time() + max(1, int(lease_seconds)), thread_id, owner),
            )
            conn.execute("COMMIT")
        if (
            db.get_thread_metadata(thread_id) is None
            or int(db.max_event_seq(thread_id)) != target
            or _canonical_anchor(db, thread_id, target) != target_id
        ):
            raise RuntimeError("canonical source anchor changed during autocomplete build")
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute("SELECT owner FROM build_leases WHERE thread_id=?", (thread_id,)).fetchone()
        if lease is None or lease["owner"] != owner:
            raise RuntimeError("autocomplete catalog build lease lost before publication")
        conn.execute(
            "UPDATE thread_authority SET active_generation=?,through_event_seq=?,through_event_id=?,state='ready',last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE thread_id=?",
            (generation, target, target_id, thread_id),
        )
        conn.execute("DELETE FROM build_leases WHERE thread_id=? AND owner=?", (thread_id, owner))
        conn.execute("COMMIT")
        # Obsolete generations are invisible after publication. Reclaiming them
        # is best effort and must not affect the active catalog.
        try:
            conn.execute(
                "DELETE FROM completion_records WHERE thread_id=? AND generation<>?",
                (thread_id, generation),
            )
        except sqlite3.Error:
            pass
        return AutocompleteBuildResult("ready", thread_id, target, len(rows), path)
    except Exception as exc:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM completion_records WHERE thread_id=? AND generation=?", (thread_id, generation))
            conn.execute("DELETE FROM build_leases WHERE thread_id=? AND owner=?", (thread_id, owner))
            conn.execute(
                "UPDATE thread_authority SET state=CASE WHEN active_generation IS NULL THEN 'error' ELSE 'ready' END,last_error=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE thread_id=?",
                (f"{type(exc).__name__}: {exc}", thread_id),
            )
            conn.execute("COMMIT")
        except Exception:
            pass
        return AutocompleteBuildResult("error", thread_id, target, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def _decode_cursor(cursor: Optional[str]) -> Optional[tuple[int, int, str]]:
    if not cursor:
        return None
    try:
        event_seq, item_order, record_id = json.loads(cursor)
        return int(event_seq), int(item_order), str(record_id)
    except Exception as exc:
        raise ValueError("invalid autocomplete cursor") from exc


def _encode_cursor(row: sqlite3.Row) -> str:
    return json.dumps([int(row["event_seq"]), int(row["item_order"]), str(row["record_id"])], separators=(",", ":"))


def query_autocomplete_records(
    db: Any,
    thread_id: str,
    fragment: str = "",
    *,
    order: AutocompleteOrder = "newest",
    limit: int = 20,
    cursor: Optional[str] = None,
) -> AutocompletePage:
    """Query one complete published generation without building it inline."""

    thread_id = str(thread_id or "").strip()
    path = autocomplete_sidecar_path(db.path)
    try:
        conn = _open_sidecar(path, create=False)
    except FileNotFoundError:
        return AutocompletePage("missing", thread_id, sidecar_path=path)
    except Exception as exc:
        return AutocompletePage("error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    try:
        state, authority = _authority_state(db, conn, thread_id)
        if state != "ready" or authority is None:
            return AutocompletePage(
                state,
                thread_id,
                int(authority["through_event_seq"]) if authority is not None else -1,
                sidecar_path=path,
                error=str(authority["last_error"]) if authority is not None and authority["last_error"] else None,
            )
        generation = str(authority["active_generation"])
        wanted = str(fragment or "").casefold()
        params: list[Any] = [thread_id, generation]
        where = ["thread_id=?", "generation=?"]
        if wanted:
            where.append("(normalized_id=? OR normalized_id LIKE ? OR reversed_normalized_id LIKE ?)")
            params.extend((wanted, wanted + "%", wanted[::-1] + "%"))
        boundary = _decode_cursor(cursor)
        direction = "DESC" if order == "newest" else "ASC"
        if boundary is not None:
            event_seq, item_order, record_id = boundary
            if order == "newest":
                where.append(
                    "(event_seq<? OR (event_seq=? AND item_order<?) OR "
                    "(event_seq=? AND item_order=? AND record_id<?))"
                )
            else:
                where.append(
                    "(event_seq>? OR (event_seq=? AND item_order>?) OR "
                    "(event_seq=? AND item_order=? AND record_id>?))"
                )
            params.extend((event_seq, event_seq, item_order, event_seq, item_order, record_id))
        total = conn.execute(
            "SELECT COUNT(*) FROM completion_records WHERE " + " AND ".join(where[:3] if wanted else where[:2]),
            tuple(params[:5] if wanted else params[:2]),
        ).fetchone()[0]
        page_limit = max(0, int(limit))
        rows = conn.execute(
            "SELECT * FROM completion_records WHERE " + " AND ".join(where)
            + f" ORDER BY event_seq {direction},item_order {direction},record_id {direction} LIMIT ?",
            (*params, page_limit + 1),
        ).fetchall()
        has_more = len(rows) > page_limit
        visible = rows[:page_limit]
        records = tuple(
            AutocompleteRecord(
                record_id=str(row["record_id"]), kind=str(row["kind"]),
                message_id=str(row["message_id"]), tool_call_id=str(row["tool_call_id"]),
                event_seq=int(row["event_seq"]), item_order=int(row["item_order"]),
                label=str(row["label"]), preview=str(row["preview"]),
                paired_message_ids=tuple(json.loads(row["paired_message_ids_json"])),
            )
            for row in visible
        )
        return AutocompletePage(
            "ready", thread_id, int(authority["through_event_seq"]), int(total), records,
            _encode_cursor(visible[-1]) if has_more and visible else None,
            path,
        )
    except Exception as exc:
        return AutocompletePage("error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
