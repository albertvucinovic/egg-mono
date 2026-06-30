from __future__ import annotations

import json
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.edit_answer import prepare_edit_answer_draft, quote_markdown_blockquote


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"
SHA = "0123456789abcdef" * 4


def _make_db(tmp_path: Path) -> tuple[ts.ThreadsDB, str]:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    return db, tid


def _append_event(
    db: ts.ThreadsDB,
    tid: str,
    type_: str,
    payload: dict,
    *,
    msg_id: str | None = None,
    invoke_id: str | None = None,
) -> None:
    db.append_event(
        event_id=f"{type_}-{db.max_event_seq(tid) + 1}",
        thread_id=tid,
        type_=type_,
        payload=payload,
        msg_id=msg_id,
        invoke_id=invoke_id,
    )


def _start_get_user_wait(db: ts.ThreadsDB, tid: str, *, note: str = "What title should I use?") -> str:
    invoke_id = "invoke-edit-answer-get-user"
    tool_call_id = "call-edit-answer-get-user"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": GET_USER_TOOL_NAME,
                        "arguments": json.dumps({"assistant_note": note}),
                    },
                }
            ]
        },
    )
    _append_event(
        db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=invoke_id,
    )
    note_msg_id = ts.append_message(
        db,
        tid,
        "assistant",
        note,
        extra={
            "answer_user_preserve_turn": True,
            "source_tool_name": GET_USER_TOOL_NAME,
            "tool_call_id": tool_call_id,
            "awaiting_user_message_tool_call_id": tool_call_id,
        },
    )
    ts.create_snapshot(db, tid)
    return note_msg_id


def _attachment_part() -> dict:
    return {
        "type": "attachment",
        "input_id": "a1b2c3d4",
        "owner_thread_id": "01KVOWNER",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "screenshot.png",
        "size_bytes": 1024,
        "sha256": SHA,
        "options": {},
    }


def test_quote_markdown_blockquote_preserves_blank_lines_and_source_markdown() -> None:
    text = "# Title\n\n```python\nprint('hi')\n```\n> old quote"

    assert quote_markdown_blockquote(text) == (
        "> # Title\n"
        ">\n"
        "> ```python\n"
        "> print('hi')\n"
        "> ```\n"
        "> > old quote"
    )


def test_prepare_edit_answer_draft_selects_latest_textual_assistant_by_default(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    ts.append_message(db, tid, "assistant", "first")
    ts.append_message(db, tid, "user", "middle")
    ts.append_message(db, tid, "assistant", "")
    latest_id = ts.append_message(db, tid, "assistant", "second")

    draft = prepare_edit_answer_draft(db, tid)

    assert draft.draft == "> second"
    assert draft.source_msg_id == latest_id
    assert draft.source_kind == "assistant_answer"
    assert draft.source_label == "assistant answer"
    assert draft.source_suffix == latest_id[-8:]


def test_prepare_edit_answer_draft_selects_active_waiting_assistant_note(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    ts.append_message(db, tid, "assistant", "Older final answer")
    note_id = _start_get_user_wait(db, tid, note="## Waiting note\n\nPlease edit me")
    ts.append_message(db, tid, "assistant", "Later assistant text")

    draft = prepare_edit_answer_draft(db, tid)

    assert draft.draft == "> ## Waiting note\n>\n> Please edit me"
    assert draft.source_msg_id == note_id
    assert draft.source_kind == "assistant_note"
    assert draft.source_label == "assistant note"


def test_prepare_edit_answer_draft_explicit_selector_overrides_waiting_note(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    selected_id = ts.append_message(db, tid, "assistant", "Explicit older final")
    note_id = _start_get_user_wait(db, tid, note="Waiting note")

    draft = prepare_edit_answer_draft(db, tid, selected_id[-8:])

    assert note_id != selected_id
    assert draft.draft == "> Explicit older final"
    assert draft.source_msg_id == selected_id
    assert draft.source_kind == "assistant_answer"


def test_prepare_edit_answer_draft_rejects_ambiguous_suffix(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    ts.append_message(db, tid, "assistant", "first")
    db.append_event(
        event_id="custom-1",
        thread_id=tid,
        type_="msg.create",
        payload={"role": "assistant", "content": "custom first"},
        msg_id="01AAAASAME",
    )
    db.append_event(
        event_id="custom-2",
        thread_id=tid,
        type_="msg.create",
        payload={"role": "assistant", "content": "custom second"},
        msg_id="01BBBBSAME",
    )

    with pytest.raises(ValueError, match="matched multiple assistant answers"):
        prepare_edit_answer_draft(db, tid, "SAME")


def test_prepare_edit_answer_draft_reports_selected_empty_answer(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    empty_id = ts.append_message(db, tid, "assistant", "")

    with pytest.raises(ValueError, match="selected assistant answer is empty"):
        prepare_edit_answer_draft(db, tid, empty_id)


def test_prepare_edit_answer_draft_converts_multipart_content_consistently(tmp_path: Path) -> None:
    db, tid = _make_db(tmp_path)
    msg_id = ts.append_message(
        db,
        tid,
        "assistant",
        [
            {"type": "text", "text": "See this"},
            _attachment_part(),
        ],
    )

    draft = prepare_edit_answer_draft(db, tid)

    assert draft.source_msg_id == msg_id
    assert draft.draft == (
        "> See this\n"
        "> [Attachment: image screenshot.png image/png 1 KB sha256:01234567]"
    )
