from __future__ import annotations

"""Disposable, versioned full-history autocomplete projection sidecar.

The canonical ThreadsDB remains authoritative.  This module stores only derived
completion metadata and publishes complete per-thread generations atomically.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional, Sequence

from .content_parts import content_to_plain_text
from .inspection import (
    _message_kind,
    _message_label,
    _message_preview,
    _tool_call_id,
    _tool_call_parts,
    _tool_call_preview,
)
from .projection import load_thread_projection

AUTOCOMPLETE_SIDECAR_VERSION = 4
AUTOCOMPLETE_SIDECAR_FILENAME = f"autocomplete-v{AUTOCOMPLETE_SIDECAR_VERSION}.sqlite"
AUTOCOMPLETE_SIDECAR_BATCH_SIZE = 500
AUTOCOMPLETE_BUILD_LEASE_SECONDS = 120
AUTOCOMPLETE_SEMANTIC_EVENT_TYPES = (
    "msg.create",
    "msg.edit",
    "msg.delete",
    "control.interrupt",
)
AutocompleteOrder = Literal["newest", "oldest"]
AutocompleteMatch = Literal["best", "all"]
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


@dataclass(frozen=True)
class AutocompleteCatalogStatus:
    state: AutocompleteCatalogState
    thread_id: str
    through_event_seq: int = -1
    target_event_seq: int = -1
    active_generation: Optional[str] = None
    owner: Optional[str] = None
    sidecar_path: Optional[Path] = None
    size_bytes: int = 0
    last_error: Optional[str] = None


def autocomplete_sidecar_path(db_path: Path | str) -> Path:
    """Return the versioned sidecar path uniquely derived from a canonical DB path."""

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
        CREATE TABLE IF NOT EXISTS projected_messages(
          thread_id TEXT NOT NULL,
          generation TEXT NOT NULL,
          msg_id TEXT NOT NULL,
          created_event_seq INTEGER NOT NULL,
          last_event_seq INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          deleted INTEGER NOT NULL,
          skipped_on_continue INTEGER NOT NULL,
          PRIMARY KEY(thread_id, generation, msg_id)
        );
        CREATE INDEX IF NOT EXISTS projected_messages_order
          ON projected_messages(thread_id, generation, created_event_seq, msg_id);
        CREATE TABLE IF NOT EXISTS completion_terms(
          thread_id TEXT NOT NULL,
          generation TEXT NOT NULL,
          normalized_term TEXT NOT NULL,
          display_term TEXT NOT NULL,
          latest_event_seq INTEGER NOT NULL,
          occurrence_count INTEGER NOT NULL,
          PRIMARY KEY(thread_id, generation, normalized_term)
        );
        CREATE INDEX IF NOT EXISTS completion_terms_prefix
          ON completion_terms(thread_id, generation, normalized_term);
        CREATE INDEX IF NOT EXISTS completion_terms_recent
          ON completion_terms(thread_id, generation, latest_event_seq DESC, occurrence_count DESC, normalized_term);
        CREATE VIRTUAL TABLE IF NOT EXISTS completion_search USING fts5(
          thread_id UNINDEXED,
          generation UNINDEXED,
          record_id UNINDEXED,
          search_text,
          tokenize='trigram'
        );
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


def autocomplete_semantic_event_seq(db: Any, thread_id: str) -> int:
    """Return the newest event that can change completion-visible history."""

    row = db.conn.execute(
        """
        SELECT MAX(event_seq)
          FROM events
         WHERE thread_id=?
           AND (
             type IN ('msg.create','msg.edit','msg.delete')
             OR (type='control.interrupt' AND json_extract(payload_json,'$.purpose')='continue')
           )
        """,
        (thread_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else -1


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
    if autocomplete_semantic_event_seq(db, thread_id) != int(row["through_event_seq"]):
        return "stale", row
    current_id = _canonical_anchor(db, thread_id, int(row["through_event_seq"]))
    if current_id != row["through_event_id"]:
        return "stale", row
    return "ready", row


def _claim_build_lease(
    conn: sqlite3.Connection,
    thread_id: str,
    target: int,
    owner: str,
    lease_seconds: int,
) -> bool:
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute(
        "SELECT owner,lease_until FROM build_leases WHERE thread_id=?", (thread_id,)
    ).fetchone()
    if current is not None and float(current["lease_until"]) > now:
        conn.execute("ROLLBACK")
        return False
    conn.execute(
        "INSERT INTO build_leases(thread_id,owner,target_event_seq,lease_until,started_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(thread_id) DO UPDATE SET owner=excluded.owner,target_event_seq=excluded.target_event_seq,lease_until=excluded.lease_until,started_at=excluded.started_at",
        (thread_id, owner, target, now + max(1, int(lease_seconds)), now),
    )
    conn.execute(
        "INSERT INTO thread_authority(thread_id,state,last_error) VALUES (?,'building',NULL) "
        "ON CONFLICT(thread_id) DO UPDATE SET state='building',last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (thread_id,),
    )
    conn.execute("COMMIT")
    return True


def _release_build_lease(conn: sqlite3.Connection, thread_id: str, owner: str) -> None:
    conn.execute("DELETE FROM build_leases WHERE thread_id=? AND owner=?", (thread_id, owner))


def _completion_terms(message: Mapping[str, Any]) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]{3,}", content_to_plain_text(message.get("content")))


def _candidate_rows(
    projection: Any,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    rows: list[dict[str, Any]] = []
    declarations: dict[str, list[str]] = {}
    results: dict[str, list[str]] = {}
    item_order = 0
    terms: dict[str, list[Any]] = {}
    for state in projection.messages:
        message = state.as_message_dict()
        msg_id = str(message.get("msg_id") or "")
        if not msg_id:
            continue
        kind = _message_kind(message)
        per_message_terms: dict[str, list[Any]] = {}
        for term in _completion_terms(message):
            normalized_term = term.casefold()
            local = per_message_terms.get(normalized_term)
            if local is None:
                per_message_terms[normalized_term] = [term, 1]
            else:
                local[0] = term
                local[1] += 1
            current = terms.get(normalized_term)
            if current is None:
                terms[normalized_term] = [term, int(state.created_event_seq), 1]
            else:
                current[2] += 1
                if int(state.created_event_seq) >= current[1]:
                    current[0] = term
                    current[1] = int(state.created_event_seq)
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
    term_rows = [
        (normalized, display, latest_seq, count)
        for normalized, (display, latest_seq, count) in terms.items()
    ]
    return out, term_rows


def _projection_state_rows(projection: Any) -> list[tuple[Any, ...]]:
    return [
        (
            state.msg_id,
            int(state.created_event_seq),
            int(state.last_event_seq),
            json.dumps(dict(state.payload), ensure_ascii=False),
            1 if state.deleted else 0,
            1 if state.skipped_on_continue else 0,
        )
        for state in projection.message_states
    ]


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(values), max(1, int(size))):
        yield values[index:index + max(1, int(size))]


def build_autocomplete_catalog(
    db: Any,
    thread_id: str,
    *,
    batch_size: int = AUTOCOMPLETE_SIDECAR_BATCH_SIZE,
    lease_seconds: int = AUTOCOMPLETE_BUILD_LEASE_SECONDS,
    _prepared_projection: Any = None,
    _target_event_seq: Optional[int] = None,
) -> AutocompleteBuildResult:
    """Build and atomically publish a complete generation for ``thread_id``."""

    thread_id = str(thread_id or "").strip()
    if not thread_id or db.get_thread_metadata(thread_id) is None:
        return AutocompleteBuildResult("error", thread_id, error="thread not found")
    path = autocomplete_sidecar_path(db.path)
    owner = uuid.uuid4().hex
    target = (
        int(_target_event_seq)
        if _target_event_seq is not None
        else autocomplete_semantic_event_seq(db, thread_id)
    )
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
        if _prepared_projection is not None:
            if (
                str(getattr(_prepared_projection, "thread_id", "")) != thread_id
                or int(getattr(_prepared_projection, "through_event_seq", -2)) != target
            ):
                raise ValueError("prepared autocomplete projection does not match build target")
            projection = _prepared_projection
        else:
            # Projection reads happen on a dedicated connection so callers may
            # request a background build without crossing sqlite thread affinity.
            from .db import ThreadsDB

            source_db = ThreadsDB(db.path)
            try:
                projection = load_thread_projection(source_db, thread_id, target)
            finally:
                source_db.close()
        rows, term_rows = _candidate_rows(projection)
        projection_rows = _projection_state_rows(projection)
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
            conn.executemany(
                "INSERT INTO completion_search(thread_id,generation,record_id,search_text) VALUES (?,?,?,?)",
                ((thread_id, generation, row[0], row[10]) for row in batch),
            )
            conn.execute(
                "UPDATE build_leases SET lease_until=? WHERE thread_id=? AND owner=?",
                (time.time() + max(1, int(lease_seconds)), thread_id, owner),
            )
            conn.execute("COMMIT")
        term_sql = (
            "INSERT INTO completion_terms(thread_id,generation,normalized_term,display_term,latest_event_seq,occurrence_count) "
            "VALUES (?,?,?,?,?,?)"
        )
        for batch in _chunks(term_rows, batch_size):
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT owner FROM build_leases WHERE thread_id=?", (thread_id,)).fetchone()
            if lease is None or lease["owner"] != owner:
                raise RuntimeError("autocomplete catalog build lease lost")
            conn.executemany(term_sql, ((thread_id, generation, *row) for row in batch))
            conn.execute(
                "UPDATE build_leases SET lease_until=? WHERE thread_id=? AND owner=?",
                (time.time() + max(1, int(lease_seconds)), thread_id, owner),
            )
            conn.execute("COMMIT")
        for batch in _chunks(projection_rows, batch_size):
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO projected_messages(thread_id,generation,msg_id,created_event_seq,last_event_seq,payload_json,deleted,skipped_on_continue) VALUES (?,?,?,?,?,?,?,?)",
                ((thread_id, generation, *row) for row in batch),
            )
            conn.execute("COMMIT")
        current_id = _canonical_anchor(db, thread_id, target)
        if db.get_thread_metadata(thread_id) is None or current_id != target_id:
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
            conn.execute(
                "DELETE FROM completion_search WHERE thread_id=? AND generation<>?",
                (thread_id, generation),
            )
            conn.execute(
                "DELETE FROM completion_terms WHERE thread_id=? AND generation<>?",
                (thread_id, generation),
            )
            conn.execute("DELETE FROM projected_messages WHERE thread_id=? AND generation<>?", (thread_id, generation))
        except sqlite3.Error:
            pass
        # A concurrent append after the captured watermark leaves this complete
        # generation safely published but immediately stale.  Callers can catch
        # it up or rebuild without discarding a valid historical generation.
        return AutocompleteBuildResult("ready", thread_id, target, len(rows), path)
    except Exception as exc:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM completion_records WHERE thread_id=? AND generation=?", (thread_id, generation))
            conn.execute("DELETE FROM completion_search WHERE thread_id=? AND generation=?", (thread_id, generation))
            conn.execute("DELETE FROM completion_terms WHERE thread_id=? AND generation=?", (thread_id, generation))
            conn.execute("DELETE FROM projected_messages WHERE thread_id=? AND generation=?", (thread_id, generation))
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


def _record_from_row(row: sqlite3.Row) -> AutocompleteRecord:
    return AutocompleteRecord(
        record_id=str(row["record_id"]), kind=str(row["kind"]),
        message_id=str(row["message_id"]), tool_call_id=str(row["tool_call_id"]),
        event_seq=int(row["event_seq"]), item_order=int(row["item_order"]),
        label=str(row["label"]), preview=str(row["preview"]),
        paired_message_ids=tuple(json.loads(row["paired_message_ids_json"])),
    )


def _prefix_bounds(value: str) -> tuple[str, str]:
    """Return SQLite B-tree bounds for a normalized literal prefix."""

    return value, value + "\U0010ffff"


def query_autocomplete_records(
    db: Any,
    thread_id: str,
    fragment: str = "",
    *,
    order: AutocompleteOrder = "newest",
    match: AutocompleteMatch = "best",
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
            if match == "best":
                exact = conn.execute(
                    "SELECT 1 FROM completion_records WHERE thread_id=? AND generation=? AND normalized_id=? LIMIT 1",
                    (thread_id, generation, wanted),
                ).fetchone()
                if exact is not None:
                    where.append("normalized_id=?")
                    params.append(wanted)
                else:
                    prefix_lo, prefix_hi = _prefix_bounds(wanted)
                    suffix_lo, suffix_hi = _prefix_bounds(wanted[::-1])
                    where.append(
                        "((normalized_id>=? AND normalized_id<?) OR "
                        "(reversed_normalized_id>=? AND reversed_normalized_id<?))"
                    )
                    params.extend((prefix_lo, prefix_hi, suffix_lo, suffix_hi))
            else:
                prefix_lo, prefix_hi = _prefix_bounds(wanted)
                suffix_lo, suffix_hi = _prefix_bounds(wanted[::-1])
                where.append(
                    "(normalized_id=? OR (normalized_id>=? AND normalized_id<?) OR "
                    "(reversed_normalized_id>=? AND reversed_normalized_id<?))"
                )
                params.extend((wanted, prefix_lo, prefix_hi, suffix_lo, suffix_hi))
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
        match_where = where[:3] if wanted else where[:2]
        match_param_count = len(params) - (6 if boundary is not None else 0)
        total = conn.execute(
            "SELECT COUNT(*) FROM completion_records WHERE " + " AND ".join(match_where),
            tuple(params[:match_param_count]),
        ).fetchone()[0]
        page_limit = max(0, int(limit))
        rows = conn.execute(
            "SELECT * FROM completion_records WHERE " + " AND ".join(where)
            + f" ORDER BY event_seq {direction},item_order {direction},record_id {direction} LIMIT ?",
            (*params, page_limit + 1),
        ).fetchall()
        has_more = len(rows) > page_limit
        visible = rows[:page_limit]
        records = tuple(_record_from_row(row) for row in visible)
        return AutocompletePage(
            "ready", thread_id, int(authority["through_event_seq"]), int(total), records,
            _encode_cursor(visible[-1]) if has_more and visible else None,
            path,
        )
    except Exception as exc:
        return AutocompletePage("error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def autocomplete_catalog_status(db: Any, thread_id: str) -> AutocompleteCatalogStatus:
    """Return inspectable state without creating or rebuilding the sidecar."""

    thread_id = str(thread_id or "").strip()
    path = autocomplete_sidecar_path(db.path)
    try:
        conn = _open_sidecar(path, create=False)
    except FileNotFoundError:
        return AutocompleteCatalogStatus("missing", thread_id, sidecar_path=path)
    except Exception as exc:
        return AutocompleteCatalogStatus(
            "error", thread_id, sidecar_path=path,
            last_error=f"{type(exc).__name__}: {exc}",
        )
    try:
        state, authority = _authority_state(db, conn, thread_id)
        lease = conn.execute(
            "SELECT owner,target_event_seq,lease_until FROM build_leases WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if lease is not None and float(lease["lease_until"]) > time.time():
            state = "preparing"
        return AutocompleteCatalogStatus(
            state=state,
            thread_id=thread_id,
            through_event_seq=(int(authority["through_event_seq"]) if authority is not None else -1),
            target_event_seq=(int(lease["target_event_seq"]) if lease is not None else -1),
            active_generation=(str(authority["active_generation"]) if authority is not None and authority["active_generation"] else None),
            owner=(str(lease["owner"]) if lease is not None else None),
            sidecar_path=path,
            size_bytes=path.stat().st_size if path.exists() else 0,
            last_error=(str(authority["last_error"]) if authority is not None and authority["last_error"] else None),
        )
    except Exception as exc:
        return AutocompleteCatalogStatus(
            "error", thread_id, sidecar_path=path,
            size_bytes=path.stat().st_size if path.exists() else 0,
            last_error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()


def clear_autocomplete_catalog(db: Any, thread_id: str) -> bool:
    """Delete one thread's disposable generations and authority."""

    thread_id = str(thread_id or "").strip()
    path = autocomplete_sidecar_path(db.path)
    if not path.exists():
        return False
    conn = _open_sidecar(path, create=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            "SELECT lease_until FROM build_leases WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if lease is not None and float(lease[0]) > time.time():
            conn.execute("ROLLBACK")
            raise RuntimeError("autocomplete catalog build is active")
        conn.execute("DELETE FROM completion_records WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM completion_search WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM completion_terms WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM projected_messages WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM build_leases WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM thread_authority WHERE thread_id=?", (thread_id,))
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def query_autocomplete_content_records(
    db: Any,
    thread_id: str,
    fragment: str,
    *,
    order: AutocompleteOrder = "newest",
    limit: int = 20,
    cursor: Optional[str] = None,
) -> AutocompletePage:
    """Search complete effective record text through the published trigram index."""

    thread_id = str(thread_id or "").strip()
    wanted = str(fragment or "").casefold()
    if len(wanted) < 3:
        return query_autocomplete_records(db, thread_id, order=order, limit=limit, cursor=cursor)
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
            return AutocompletePage(state, thread_id, sidecar_path=path)
        generation = str(authority["active_generation"])
        direction = "DESC" if order == "newest" else "ASC"
        boundary = _decode_cursor(cursor)
        params: list[Any] = [wanted, thread_id, generation]
        boundary_sql = ""
        if boundary is not None:
            event_seq, item_order, record_id = boundary
            if order == "newest":
                boundary_sql = (
                    " AND (r.event_seq<? OR (r.event_seq=? AND r.item_order<?) OR "
                    "(r.event_seq=? AND r.item_order=? AND r.record_id<?))"
                )
            else:
                boundary_sql = (
                    " AND (r.event_seq>? OR (r.event_seq=? AND r.item_order>?) OR "
                    "(r.event_seq=? AND r.item_order=? AND r.record_id>?))"
                )
            params.extend((event_seq, event_seq, item_order, event_seq, item_order, record_id))
        total = conn.execute(
            "SELECT COUNT(*) FROM completion_search s "
            "JOIN completion_records r ON r.thread_id=s.thread_id AND r.generation=s.generation AND r.record_id=s.record_id "
            "WHERE completion_search MATCH ? AND s.thread_id=? AND s.generation=?",
            (wanted, thread_id, generation),
        ).fetchone()[0]
        page_limit = max(0, int(limit))
        rows = conn.execute(
            "SELECT r.* FROM completion_search s "
            "JOIN completion_records r ON r.thread_id=s.thread_id AND r.generation=s.generation AND r.record_id=s.record_id "
            "WHERE completion_search MATCH ? AND s.thread_id=? AND s.generation=?"
            + boundary_sql
            + f" ORDER BY r.event_seq {direction},r.item_order {direction},r.record_id {direction} LIMIT ?",
            (*params, page_limit + 1),
        ).fetchall()
        visible = rows[:page_limit]
        records = tuple(_record_from_row(row) for row in visible)
        return AutocompletePage(
            "ready", thread_id, int(authority["through_event_seq"]), int(total), records,
            _encode_cursor(visible[-1]) if len(rows) > page_limit and visible else None,
            path,
        )
    except Exception as exc:
        return AutocompletePage("error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def query_autocomplete_terms(
    db: Any,
    thread_id: str,
    fragment: str,
    *,
    limit: int = 50,
) -> tuple[AutocompleteCatalogState, tuple[str, ...]]:
    """Return complete-history conversation words ranked by latest occurrence."""

    thread_id = str(thread_id or "").strip()
    wanted = str(fragment or "").casefold()
    path = autocomplete_sidecar_path(db.path)
    try:
        conn = _open_sidecar(path, create=False)
    except FileNotFoundError:
        return "missing", ()
    except Exception:
        return "error", ()
    try:
        state, authority = _authority_state(db, conn, thread_id)
        if state != "ready" or authority is None:
            return state, ()
        generation = str(authority["active_generation"])
        rows = conn.execute(
            "SELECT display_term FROM completion_terms "
            "WHERE thread_id=? AND generation=? AND normalized_term LIKE ? AND normalized_term<>? "
            "ORDER BY latest_event_seq DESC,occurrence_count DESC,normalized_term LIMIT ?",
            (thread_id, generation, wanted + "%", wanted, max(0, int(limit))),
        ).fetchall()
        return "ready", tuple(str(row[0]) for row in rows)
    except Exception:
        return "error", ()
    finally:
        conn.close()


def _projection_from_sidecar(
    conn: sqlite3.Connection,
    thread_id: str,
    generation: str,
    through_event_seq: int,
):
    from .projection import ProjectedMessage, ThreadProjection

    rows = conn.execute(
        "SELECT * FROM projected_messages WHERE thread_id=? AND generation=? ORDER BY created_event_seq,msg_id",
        (thread_id, generation),
    ).fetchall()
    states = tuple(
        ProjectedMessage(
            thread_id=thread_id,
            msg_id=str(row["msg_id"]),
            payload=json.loads(row["payload_json"]),
            created_event_seq=int(row["created_event_seq"]),
            created_event_id=None,
            created_at=None,
            last_event_seq=int(row["last_event_seq"]),
            last_event_id=None,
            updated_at=None,
            deleted=bool(row["deleted"]),
            skipped_on_continue=bool(row["skipped_on_continue"]),
        )
        for row in rows
    )
    return ThreadProjection(
        thread_id=thread_id,
        through_event_seq=int(through_event_seq),
        message_states=states,
        started_from_snapshot_event_seq=-1,
        tail_event_types=(),
    )


def _semantic_tail_rows(db: Any, thread_id: str, after: int, through: int):
    return db.conn.execute(
        """
        SELECT event_seq,event_id,ts,thread_id,type,msg_id,payload_json
          FROM events
         WHERE thread_id=? AND event_seq>? AND event_seq<=?
           AND (
             type IN ('msg.create','msg.edit','msg.delete')
             OR (type='control.interrupt' AND json_extract(payload_json,'$.purpose')='continue')
           )
         ORDER BY event_seq
        """,
        (thread_id, int(after), int(through)),
    ).fetchall()


def catch_up_autocomplete_catalog(
    db: Any,
    thread_id: str,
    *,
    batch_size: int = AUTOCOMPLETE_SIDECAR_BATCH_SIZE,
    max_tail_events: int = 1000,
) -> AutocompleteBuildResult:
    """Incrementally reduce semantic tail events and update the active catalog."""

    thread_id = str(thread_id or "").strip()
    path = autocomplete_sidecar_path(db.path)
    if not path.exists():
        return build_autocomplete_catalog(db, thread_id, batch_size=batch_size)
    conn = _open_sidecar(path, create=True)
    owner = uuid.uuid4().hex
    claimed = False
    try:
        _state, authority = _authority_state_without_freshness(conn, thread_id)
        if authority is None or not authority["active_generation"]:
            conn.close()
            return build_autocomplete_catalog(db, thread_id, batch_size=batch_size)
        generation = str(authority["active_generation"])
        old_seq = int(authority["through_event_seq"])
        target = autocomplete_semantic_event_seq(db, thread_id)
        if target == old_seq:
            total = conn.execute(
                "SELECT COUNT(*) FROM completion_records WHERE thread_id=? AND generation=?",
                (thread_id, generation),
            ).fetchone()[0]
            return AutocompleteBuildResult("ready", thread_id, target, int(total), path)
        if target < old_seq or _canonical_anchor(db, thread_id, old_seq) != authority["through_event_id"]:
            conn.close()
            return build_autocomplete_catalog(db, thread_id, batch_size=batch_size)
        if not _claim_build_lease(conn, thread_id, target, owner, AUTOCOMPLETE_BUILD_LEASE_SECONDS):
            return AutocompleteBuildResult("busy", thread_id, target, sidecar_path=path)
        claimed = True
        base = _projection_from_sidecar(conn, thread_id, generation, old_seq)
        tail = _semantic_tail_rows(db, thread_id, old_seq, target)
        if len(tail) > max(1, int(max_tail_events)):
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            _release_build_lease(conn, thread_id, owner)
            conn.close()
            return build_autocomplete_catalog(db, thread_id, batch_size=batch_size)
        from .projection import _apply_events, _decode_event_record

        events = tuple(_decode_event_record(row, default_thread_id=thread_id) for row in tail)
        projection = _apply_events(
            thread_id, base.message_states, events,
            through_event_seq=target, started_from_snapshot_event_seq=old_seq,
            base_snapshot=None,
        )
        result = _catch_up_generation_in_place(
            db, conn, thread_id, authority, target, projection, tail, owner
        )
        claimed = False
        return result
    except Exception as exc:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if claimed:
                _release_build_lease(conn, thread_id, owner)
            conn.execute(
                "UPDATE thread_authority SET state=CASE WHEN active_generation IS NULL THEN 'error' ELSE 'ready' END,last_error=? WHERE thread_id=?",
                (f"{type(exc).__name__}: {exc}", thread_id),
            )
        except Exception:
            pass
        return AutocompleteBuildResult(
            "error", thread_id, sidecar_path=path, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _authority_state_without_freshness(
    conn: sqlite3.Connection, thread_id: str
) -> tuple[str, Optional[sqlite3.Row]]:
    _validate_manifest(conn)
    row = conn.execute("SELECT * FROM thread_authority WHERE thread_id=?", (thread_id,)).fetchone()
    return (str(row["state"]) if row is not None else "missing"), row


def _publish_projection_generation(
    db: Any,
    thread_id: str,
    target: int,
    projection: Any,
    *,
    batch_size: int,
) -> AutocompleteBuildResult:
    """Publish an already-reduced projection through the shared builder."""

    return build_autocomplete_catalog(
        db,
        thread_id,
        batch_size=batch_size,
        _prepared_projection=projection,
        _target_event_seq=target,
    )


def _changed_message_ids(tail_rows: Sequence[Any], projection: Any) -> set[str]:
    changed: set[str] = set()
    states_by_id = {state.msg_id: state for state in projection.message_states}
    for row in tail_rows:
        event_type = str(row["type"])
        msg_id = str(row["msg_id"] or "")
        if msg_id:
            changed.add(msg_id)
        if event_type == "control.interrupt":
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
            start_id = payload.get("continue_from_msg_id") if isinstance(payload, dict) else None
            start = states_by_id.get(str(start_id)) if start_id else None
            if start is not None:
                boundary = int(row["event_seq"])
                changed.update(
                    state.msg_id
                    for state in projection.message_states
                    if start.created_event_seq < state.created_event_seq < boundary
                )
    related_tool_call_ids: set[str] = set()
    for state in projection.message_states:
        if state.msg_id not in changed:
            continue
        result_id = state.payload.get("tool_call_id")
        if isinstance(result_id, str) and result_id:
            related_tool_call_ids.add(result_id)
        calls = state.payload.get("tool_calls")
        if isinstance(calls, list):
            related_tool_call_ids.update(
                call_id
                for raw in calls
                if isinstance(raw, Mapping)
                and (call_id := _tool_call_id(raw))
            )
    if related_tool_call_ids:
        for state in projection.message_states:
            result_id = state.payload.get("tool_call_id")
            calls = state.payload.get("tool_calls")
            declares_related = isinstance(calls, list) and any(
                isinstance(raw, Mapping) and _tool_call_id(raw) in related_tool_call_ids
                for raw in calls
            )
            if result_id in related_tool_call_ids or declares_related:
                changed.add(state.msg_id)
    return changed


def _message_rows_for_catalog(projection: Any, message_ids: set[str]) -> list[tuple[Any, ...]]:
    selected = tuple(
        state for state in projection.message_states if state.msg_id in message_ids
    )
    from .projection import ThreadProjection

    partial = ThreadProjection(
        thread_id=projection.thread_id,
        through_event_seq=projection.through_event_seq,
        message_states=selected,
        started_from_snapshot_event_seq=-1,
        tail_event_types=(),
    )
    rows, _terms = _candidate_rows(partial)
    return rows


def _refresh_completion_terms(
    conn: sqlite3.Connection, thread_id: str, generation: str
) -> None:
    rows = conn.execute(
        "SELECT payload_json,created_event_seq FROM projected_messages "
        "WHERE thread_id=? AND generation=? AND deleted=0 AND skipped_on_continue=0",
        (thread_id, generation),
    ).fetchall()
    terms: dict[str, list[Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        for term in _completion_terms(payload if isinstance(payload, Mapping) else {}):
            normalized = term.casefold()
            current = terms.get(normalized)
            if current is None:
                terms[normalized] = [term, int(row["created_event_seq"]), 1]
            else:
                current[2] += 1
                if int(row["created_event_seq"]) >= current[1]:
                    current[0] = term
                    current[1] = int(row["created_event_seq"])
    conn.execute(
        "DELETE FROM completion_terms WHERE thread_id=? AND generation=?",
        (thread_id, generation),
    )
    conn.executemany(
        "INSERT INTO completion_terms(thread_id,generation,normalized_term,display_term,latest_event_seq,occurrence_count) VALUES (?,?,?,?,?,?)",
        (
            (thread_id, generation, normalized, display, latest, count)
            for normalized, (display, latest, count) in terms.items()
        ),
    )


def _catch_up_generation_in_place(
    db: Any,
    conn: sqlite3.Connection,
    thread_id: str,
    authority: sqlite3.Row,
    target: int,
    projection: Any,
    tail_rows: Sequence[Any],
    owner: str,
) -> AutocompleteBuildResult:
    generation = str(authority["active_generation"])
    changed = _changed_message_ids(tail_rows, projection)
    states = {state.msg_id: state for state in projection.message_states}
    record_rows = _message_rows_for_catalog(projection, changed)
    conn.execute("BEGIN IMMEDIATE")
    lease = conn.execute("SELECT owner FROM build_leases WHERE thread_id=?", (thread_id,)).fetchone()
    if lease is None or lease["owner"] != owner:
        raise RuntimeError("autocomplete catalog build lease lost")
    if changed:
        placeholders = ",".join("?" for _ in changed)
        args = (thread_id, generation, *sorted(changed))
        conn.execute(
            f"DELETE FROM completion_search WHERE thread_id=? AND generation=? AND record_id IN "
            f"(SELECT record_id FROM completion_records WHERE thread_id=? AND generation=? AND message_id IN ({placeholders}))",
            (thread_id, generation, thread_id, generation, *sorted(changed)),
        )
        conn.execute(
            f"DELETE FROM completion_records WHERE thread_id=? AND generation=? AND message_id IN ({placeholders})",
            args,
        )
        conn.execute(
            f"DELETE FROM projected_messages WHERE thread_id=? AND generation=? AND msg_id IN ({placeholders})",
            args,
        )
    for msg_id in sorted(changed):
        state = states.get(msg_id)
        if state is None:
            continue
        conn.execute(
            "INSERT INTO projected_messages(thread_id,generation,msg_id,created_event_seq,last_event_seq,payload_json,deleted,skipped_on_continue) VALUES (?,?,?,?,?,?,?,?)",
            (
                thread_id, generation, state.msg_id, int(state.created_event_seq),
                int(state.last_event_seq), json.dumps(dict(state.payload), ensure_ascii=False),
                1 if state.deleted else 0, 1 if state.skipped_on_continue else 0,
            ),
        )
    if record_rows:
        conn.executemany(
            "INSERT INTO completion_records(thread_id,generation,record_id,normalized_id,reversed_normalized_id,kind,message_id,tool_call_id,event_seq,item_order,label,preview,search_text,paired_message_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((thread_id, generation, *row) for row in record_rows),
        )
        conn.executemany(
            "INSERT INTO completion_search(thread_id,generation,record_id,search_text) VALUES (?,?,?,?)",
            ((thread_id, generation, row[0], row[10]) for row in record_rows),
        )
    _refresh_completion_terms(conn, thread_id, generation)
    target_id = _canonical_anchor(db, thread_id, target)
    conn.execute(
        "UPDATE thread_authority SET through_event_seq=?,through_event_id=?,state='ready',last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE thread_id=?",
        (target, target_id, thread_id),
    )
    _release_build_lease(conn, thread_id, owner)
    conn.execute("COMMIT")
    total = conn.execute(
        "SELECT COUNT(*) FROM completion_records WHERE thread_id=? AND generation=?",
        (thread_id, generation),
    ).fetchone()[0]
    return AutocompleteBuildResult("ready", thread_id, target, int(total), autocomplete_sidecar_path(db.path))
