from __future__ import annotations

"""Shared backend helpers for preparing quoted assistant-answer drafts.

The functions in this module are intentionally terminal/UI independent so
terminal Egg, EggW, and future clients all use the same source selection and
markdown-quoting rules for `/editAnswer`-style flows.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .content_parts import content_to_plain_text


EditAnswerSourceKind = Literal["assistant_answer", "assistant_note"]


@dataclass(frozen=True)
class EditAnswerDraft:
    """A quoted draft prepared from one assistant message."""

    draft: str
    source_msg_id: str
    source_kind: EditAnswerSourceKind
    source_suffix: str = ""
    source_label: str = ""


def quote_markdown_blockquote(text: str) -> str:
    """Return ``text`` as a markdown blockquote, preserving blank lines.

    This is intentionally a mechanical source transform: the editable buffer is
    raw assistant markdown, not rendered markdown. Every physical source line is
    prefixed, and blank lines become ``>`` so the quote does not visually break
    when rendered.
    """

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if normalized == "":
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in normalized.split("\n"))


def message_raw_text(message: Mapping[str, Any]) -> str:
    """Return the raw editable text for a snapshot message.

    String content is returned byte-for-byte. Egg content-part arrays are
    rendered with the same tolerant plain-text conversion terminal Egg already
    used, so text parts remain editable text and non-text parts become stable
    placeholders.
    """

    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return content_to_plain_text(content)


def is_assistant_note(message: Mapping[str, Any]) -> bool:
    """Return True when an assistant message is a preserve-turn Assistant Note."""

    return bool(message.get("answer_user_preserve_turn"))


def assistant_message_candidates(messages: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], str]]:
    """Return textual assistant-message candidates in transcript order."""

    candidates: list[tuple[Mapping[str, Any], str]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = message_raw_text(message)
        if text.strip():
            candidates.append((message, text))
    return candidates


def message_id(message: Mapping[str, Any]) -> str:
    """Return the durable message id from either supported snapshot field."""

    return str(message.get("msg_id") or message.get("id") or "")


def _selector_matches_message(message: Mapping[str, Any], selector: str) -> bool:
    msg_id = message_id(message)
    return msg_id == selector or (msg_id and msg_id.endswith(selector))


def _assistant_selector_matches(
    messages: Sequence[Mapping[str, Any]],
    selector: str,
) -> list[tuple[Mapping[str, Any], str]]:
    matches: list[tuple[Mapping[str, Any], str]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if not _selector_matches_message(message, selector):
            continue
        matches.append((message, message_raw_text(message)))
    return matches


def select_assistant_message(
    messages: Sequence[Mapping[str, Any]],
    selector: str = "",
    *,
    preferred_msg_id: str = "",
    prefer_notes: bool = False,
) -> tuple[Mapping[str, Any], str]:
    """Select an assistant message by msg_id/suffix, or the latest by default.

    Explicit selectors always win. For default selection, callers may provide a
    preferred waiting-note msg_id and/or request Assistant Note preference while
    an active get-user-message tool is waiting.
    """

    wanted = (selector or "").strip()
    if wanted:
        matches = _assistant_selector_matches(messages, wanted)
        if not matches:
            raise ValueError(f"No assistant answer matched selector {wanted!r}.")
        if len(matches) > 1:
            raise ValueError(f"Selector {wanted!r} matched multiple assistant answers; use a longer msg_id.")
        message, text = matches[0]
        if not text.strip():
            raise ValueError("selected assistant answer is empty.")
        return message, text

    candidates = assistant_message_candidates(messages)
    if not candidates:
        raise ValueError("No assistant answer with textual content was found in this thread.")

    preferred = str(preferred_msg_id or "").strip()
    if preferred:
        for message, text in candidates:
            if message_id(message) == preferred:
                return message, text
    if prefer_notes:
        notes = [(message, text) for message, text in candidates if is_assistant_note(message)]
        if notes:
            return notes[-1]
    return candidates[-1]


def _snapshot_messages_from_db(db: Any, thread_id: str) -> list[Mapping[str, Any]]:
    """Read messages from the current persisted thread snapshot."""

    row = db.get_thread(thread_id)
    if not row or not getattr(row, "snapshot_json", None):
        return []
    try:
        snap = json.loads(row.snapshot_json)
    except Exception:
        return []
    messages = snap.get("messages", []) if isinstance(snap, Mapping) else []
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, Mapping)]


def _fresh_snapshot_messages(db: Any, thread_id: str) -> list[Mapping[str, Any]]:
    """Create/read a fresh snapshot and return its messages."""

    try:
        from .api import create_snapshot

        snap = create_snapshot(db, thread_id)
        messages = snap.get("messages", []) if isinstance(snap, Mapping) else []
        if isinstance(messages, list):
            return [message for message in messages if isinstance(message, Mapping)]
    except Exception:
        pass
    return _snapshot_messages_from_db(db, thread_id)


def _active_waiting_note(db: Any, thread_id: str) -> Mapping[str, Any] | None:
    try:
        from .api import get_active_get_user_message_waiting_note

        note = get_active_get_user_message_waiting_note(db, thread_id)
    except Exception:
        note = None
    if not isinstance(note, Mapping):
        return None
    return note


def prepare_edit_answer_draft(
    db: Any,
    thread_id: str,
    selector: str = "",
    *,
    prefer_waiting_note: bool = True,
) -> EditAnswerDraft:
    """Prepare a quoted edit-answer draft from a thread's assistant message.

    Args:
        db: ThreadsDB-like object for the thread store.
        thread_id: Thread whose assistant answer should be quoted.
        selector: Optional exact msg_id or unique msg_id suffix.
        prefer_waiting_note: When True and no selector is provided, an active
            get-user-message waiting Assistant Note is selected over ordinary
            assistant answers.

    Raises:
        ValueError: if the thread/selection has no usable textual assistant
            answer, no selector match, or an ambiguous suffix match.
    """

    normalized_thread_id = str(thread_id or "").strip()
    if db is None or not normalized_thread_id:
        raise ValueError("No current thread.")

    wanted = (selector or "").strip()
    messages = _fresh_snapshot_messages(db, normalized_thread_id)
    waiting_note: Mapping[str, Any] | None = None
    preferred_msg_id = ""
    if prefer_waiting_note and not wanted:
        waiting_note = _active_waiting_note(db, normalized_thread_id)
        preferred_msg_id = str((waiting_note or {}).get("msg_id") or "").strip()

    message, raw_text = select_assistant_message(
        messages,
        wanted,
        preferred_msg_id=preferred_msg_id,
        prefer_notes=waiting_note is not None,
    )
    draft = quote_markdown_blockquote(raw_text)
    if not draft.strip():
        raise ValueError("selected assistant answer is empty.")

    source_msg_id = message_id(message)
    source_kind: EditAnswerSourceKind = "assistant_note" if is_assistant_note(message) else "assistant_answer"
    source_label = "assistant note" if source_kind == "assistant_note" else "assistant answer"
    return EditAnswerDraft(
        draft=draft,
        source_msg_id=source_msg_id,
        source_kind=source_kind,
        source_suffix=source_msg_id[-8:] if source_msg_id else "",
        source_label=source_label,
    )


__all__ = [
    "EditAnswerDraft",
    "EditAnswerSourceKind",
    "assistant_message_candidates",
    "is_assistant_note",
    "message_id",
    "message_raw_text",
    "prepare_edit_answer_draft",
    "quote_markdown_blockquote",
    "select_assistant_message",
]
