"""Durable, thread-scoped history of inputs submitted by human clients."""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping, Optional, Sequence

from .content_parts import TEXT_PART_TYPE
from .db import ThreadsDB

INPUT_SUBMITTED_EVENT_TYPE = "input.submitted"
DEFAULT_INPUT_HISTORY_LIMIT = 200
MAX_INPUT_HISTORY_LIMIT = 1000


def input_text_from_message_content(content: Any) -> str:
    """Return only user-authored text from message content.

    Attachment and artifact placeholders are transcript presentation, not text
    the operator typed, so recalling an attachment-bearing message never stages
    or inserts those placeholders into the composer.
    """

    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    text_parts = [
        str(part["text"])
        for part in content
        if (
            isinstance(part, Mapping)
            and part.get("type") == TEXT_PART_TYPE
            and isinstance(part.get("text"), str)
        )
    ]
    return "\n".join(text_parts).strip()


def _append_message_input_submission(
    db: ThreadsDB,
    thread_id: str,
    msg_id: str,
    *,
    source: Optional[str] = None,
) -> int:
    """Link input history to its message without duplicating message content."""

    payload = {"kind": "message"}
    if source:
        payload["source"] = str(source)
    return db.append_event(
        event_id=os.urandom(16).hex(),
        thread_id=thread_id,
        type_=INPUT_SUBMITTED_EVENT_TYPE,
        payload=payload,
        msg_id=msg_id,
    )


def record_submitted_command(
    db: ThreadsDB,
    thread_id: str,
    command: str,
    *,
    source: Optional[str] = None,
) -> Optional[int]:
    """Record one human command submission, including deliberate repeats."""

    normalized = str(command or "").strip()
    if not normalized:
        return None
    payload = {"text": normalized, "kind": "command"}
    if source:
        payload["source"] = str(source)
    return db.append_event(
        event_id=os.urandom(16).hex(),
        thread_id=thread_id,
        type_=INPUT_SUBMITTED_EVENT_TYPE,
        payload=payload,
    )


def append_submitted_user_message(
    db: ThreadsDB,
    thread_id: str,
    content: str | list[dict[str, Any]],
    *,
    input_text: Optional[str] = None,
    source: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """Atomically record human input and append its normal user message."""

    from .api import append_normal_user_message

    reusable_text = (
        input_text_from_message_content(content)
        if input_text is None
        else str(input_text or "").strip()
    )
    savepoint = f"append_submitted_input_{os.urandom(8).hex()}"
    db.conn.execute(f"SAVEPOINT {savepoint}")
    try:
        message_extra = dict(extra or {})
        if reusable_text:
            message_extra["input_history_recorded"] = True
        msg_id = append_normal_user_message(
            db,
            thread_id,
            content,
            extra=message_extra or None,
        )
        if reusable_text:
            _append_message_input_submission(db, thread_id, msg_id, source=source)
        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return msg_id
    except Exception:
        db.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _decode_payload(raw: Any) -> dict[str, Any]:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _legacy_user_input(payload: Mapping[str, Any]) -> tuple[str, bool] | None:
    """Return ``(text, command_like)`` for a best-effort legacy input."""

    if payload.get("role") != "user":
        return None
    if payload.get("input_history_recorded"):
        return None
    if (
        payload.get("compaction_summary_request")
        or payload.get("auto_compaction_request")
        or payload.get("synthetic_user_tool_request")
        or payload.get("from_thread_id")
        or payload.get("origin") == "manager_message"
    ):
        return None

    text = input_text_from_message_content(payload.get("content"))
    if not text:
        return None

    # /btw historically stored its generated provider instruction as a user
    # message without provenance. Do not expose that implementation prompt as
    # if the operator had typed it.
    if text.startswith(
        "Please answer the following user message using the "
        "answer_user_while_preserving_llm_turn tool"
    ):
        return None

    command_like = bool(
        payload.get("user_command_type")
        or (
            text.startswith(("/", "$"))
            and (payload.get("no_api") or payload.get("keep_user_turn"))
            and not payload.get("consumed_by_tool_call_id")
        )
    )
    if payload.get("tool_calls") and not command_like:
        return None
    if payload.get("keep_user_turn") and not command_like:
        return None
    if payload.get("no_api") and not command_like:
        return None
    return text, command_like



def _rows_for_type(
    db: ThreadsDB,
    thread_id: str,
    event_type: str,
    *,
    before_seq: int,
    limit: int,
) -> Sequence[Any]:
    return db.conn.execute(
        "SELECT event_seq, msg_id, payload_json FROM events INDEXED BY events_thread_type "
        "WHERE thread_id=? AND type=? AND event_seq<? "
        "ORDER BY event_seq DESC LIMIT ?",
        (thread_id, event_type, int(before_seq), int(limit)),
    ).fetchall()


def _effective_message_content(db: ThreadsDB, msg_ids: Sequence[str]) -> dict[str, Any]:
    """Return latest non-deleted content for selected messages."""

    if not msg_ids:
        return {}
    placeholders = ",".join("?" for _ in msg_ids)
    rows = db.conn.execute(
        f"SELECT event_seq, type, msg_id, payload_json FROM events INDEXED BY events_msg_seq "
        f"WHERE msg_id IN ({placeholders}) AND type IN ('msg.create', 'msg.edit', 'msg.delete') "
        f"ORDER BY event_seq ASC",
        list(msg_ids),
    ).fetchall()
    content: dict[str, Any] = {}
    for row in rows:
        msg_id = str(row["msg_id"] or "")
        if row["type"] == "msg.delete":
            content.pop(msg_id, None)
            continue
        payload = _decode_payload(row["payload_json"])
        if "content" in payload:
            content[msg_id] = payload.get("content")
    return content


def _legacy_input_history(
    db: ThreadsDB,
    thread_id: str,
    *,
    before_seq: int,
    limit: int,
) -> list[str]:
    """Return a bounded best-effort history for pre-adoption threads."""

    scan_limit = min(MAX_INPUT_HISTORY_LIMIT * 8, max(limit * 8, 512))
    candidates: list[tuple[int, str, str, bool]] = []
    for event_type, source in (("user_command.started", "command"), ("msg.create", "message")):
        source_before_seq = before_seq
        source_scanned = 0
        while source_scanned < scan_limit:
            rows = _rows_for_type(
                db,
                thread_id,
                event_type,
                before_seq=source_before_seq,
                limit=min(64, scan_limit - source_scanned),
            )
            if not rows:
                break
            source_scanned += len(rows)
            source_before_seq = min(int(row["event_seq"]) for row in rows)
            for row in rows:
                payload = _decode_payload(row["payload_json"])
                if source == "command":
                    if payload.get("input_submission") is False:
                        continue
                    text = str(payload.get("command") or "").strip()
                    parsed = (text, True) if text else None
                else:
                    parsed = _legacy_user_input(payload)
                if parsed is not None:
                    text, command_like = parsed
                    candidates.append((int(row["event_seq"]), text, source, command_like))
            if len(rows) < 64:
                break

    # Each source query was bounded independently. Apply the same overall scan
    # budget after canonical ordering so a dense mix of both source types cannot
    # double the promised legacy work bound.
    candidates = sorted(candidates)[-scan_limit:]

    # EggW historically represented visible shell commands both as a lifecycle
    # event and a user tool-declaration message. Pair those cross-source copies
    # without collapsing deliberate repeats from the same source.
    merged: list[tuple[int, str, str, bool]] = []
    matched_command_indexes: set[int] = set()
    for candidate in candidates:
        _seq, text, source, command_like = candidate
        if source == "message" and command_like:
            match = next((
                index
                for index in range(len(merged) - 1, -1, -1)
                if index not in matched_command_indexes
                and merged[index][2] == "command"
                and merged[index][1] == text
            ), None)
            if match is not None:
                matched_command_indexes.add(match)
                continue
        merged.append(candidate)
    return [candidate[1] for candidate in merged[-limit:]]


def _canonical_input_history(
    db: ThreadsDB,
    thread_id: str,
    *,
    limit: int,
) -> list[str]:
    """Return the newest effective canonical inputs, oldest to newest.

    Message-linked submissions can later be deleted or edited to empty text.
    Read canonical rows in bounded pages so those entries do not consume the
    caller's result limit and hide older reusable input.
    """

    newest_first: list[str] = []
    before_seq = db.max_event_seq(thread_id) + 1
    page_size = max(64, min(limit, MAX_INPUT_HISTORY_LIMIT))
    # One request may skip only a bounded multiple of deleted/blank canonical
    # entries; history lookup must never turn an arbitrarily large event log
    # into unbounded synchronous work.
    scan_budget = min(MAX_INPUT_HISTORY_LIMIT * 8, max(limit * 8, 64))
    scanned = 0
    while len(newest_first) < limit and scanned < scan_budget:
        rows = _rows_for_type(
            db,
            thread_id,
            INPUT_SUBMITTED_EVENT_TYPE,
            before_seq=before_seq,
            limit=min(page_size, scan_budget - scanned),
        )
        if not rows:
            break
        scanned += len(rows)
        message_ids = [str(row["msg_id"]) for row in rows if row["msg_id"]]
        message_content = _effective_message_content(db, message_ids)
        for row in rows:
            payload = _decode_payload(row["payload_json"])
            text = str(payload.get("text") or "").strip()
            if not text and row["msg_id"]:
                text = input_text_from_message_content(message_content.get(str(row["msg_id"])))
            if text:
                newest_first.append(text)
                if len(newest_first) >= limit:
                    break
        before_seq = min(int(row["event_seq"]) for row in rows)
        if len(rows) < page_size:
            break
    return list(reversed(newest_first))


def list_input_history(
    db: ThreadsDB,
    thread_id: str,
    *,
    limit: int = DEFAULT_INPUT_HISTORY_LIMIT,
) -> list[str]:
    """Return reusable inputs oldest-to-newest for one thread.

    Canonical ``input.submitted`` events are the authority from their adoption
    point onward. Message submissions link to their existing ``msg.create``
    content instead of duplicating potentially sensitive prompt text in another
    event. Earlier records are reconstructed best-effort from command lifecycle
    and user-message events.
    """

    if db.get_thread_metadata(thread_id) is None:
        raise ValueError(f"Thread not found: {thread_id}")
    try:
        bounded_limit = max(1, min(int(limit), MAX_INPUT_HISTORY_LIMIT))
    except (TypeError, ValueError):
        bounded_limit = DEFAULT_INPUT_HISTORY_LIMIT
    adoption_row = db.conn.execute(
        "SELECT MIN(event_seq) FROM events INDEXED BY events_thread_type "
        "WHERE thread_id=? AND type=?",
        (thread_id, INPUT_SUBMITTED_EVENT_TYPE),
    ).fetchone()
    adoption_seq = int(adoption_row[0]) if adoption_row and adoption_row[0] is not None else None

    canonical: list[str] = []
    if adoption_seq is not None:
        canonical = _canonical_input_history(db, thread_id, limit=bounded_limit)

    if len(canonical) >= bounded_limit:
        return canonical[-bounded_limit:]

    legacy_limit = bounded_limit - len(canonical)
    legacy = _legacy_input_history(
        db,
        thread_id,
        before_seq=adoption_seq if adoption_seq is not None else db.max_event_seq(thread_id) + 1,
        limit=legacy_limit,
    )
    return [*legacy, *canonical][-bounded_limit:]


class InputHistoryNavigator:
    """State machine for draft-preserving older/newer input traversal."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self._entries: tuple[str, ...] = ()
        self._position: Optional[int] = None
        self._draft = ""
        self.replace_entries(entries)

    @property
    def active(self) -> bool:
        return self._position is not None

    def replace_entries(self, entries: Iterable[str]) -> None:
        self._entries = tuple(str(entry) for entry in entries if str(entry))
        self.reset()

    def reset(self) -> None:
        self._position = None
        self._draft = ""

    def older(self, current_draft: str) -> Optional[str]:
        if not self._entries:
            return None
        if self._position is None:
            self._draft = str(current_draft)
            self._position = len(self._entries) - 1
        elif self._position > 0:
            self._position -= 1
        return self._entries[self._position]

    def newer(self) -> Optional[str]:
        if self._position is None:
            return None
        if self._position < len(self._entries) - 1:
            self._position += 1
            return self._entries[self._position]
        draft = self._draft
        self.reset()
        return draft
