from __future__ import annotations

import eggthreads as ts
from eggthreads.attachment_staging import build_message_content_with_attachments


def make_thread(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="history")
    ts.append_message(db, thread_id, "system", "system")
    return db, thread_id


def attachment_part():
    return {
        "type": "attachment",
        "input_id": "input001",
        "owner_thread_id": "owner",
        "presentation": "file",
        "mime_type": "text/plain",
        "filename": "notes.txt",
        "size_bytes": 5,
        "sha256": "a" * 64,
    }


def test_canonical_history_preserves_repeats_and_text_without_attachments(tmp_path):
    db, thread_id = make_thread(tmp_path)
    content = build_message_content_with_attachments("inspect this", [attachment_part()])
    ts.append_submitted_user_message(db, thread_id, content, source="test")
    ts.record_submitted_command(db, thread_id, "/help", source="test")
    ts.record_submitted_command(db, thread_id, "/help", source="test")

    assert ts.list_input_history(db, thread_id) == ["inspect this", "/help", "/help"]
    assert "Attachment:" not in "\n".join(ts.list_input_history(db, thread_id))
    linked = db.conn.execute(
        "SELECT msg_id, payload_json FROM events "
        "WHERE thread_id=? AND type='input.submitted' "
        "ORDER BY event_seq ASC LIMIT 1",
        (thread_id,),
    ).fetchone()
    assert linked["msg_id"]
    assert "inspect this" not in linked["payload_json"]


def test_history_preserves_all_user_authored_text_parts(tmp_path):
    db, thread_id = make_thread(tmp_path)
    content = [
        {"type": "text", "text": "first"},
        attachment_part(),
        {"type": "text", "text": "second"},
    ]

    ts.append_submitted_user_message(db, thread_id, content, source="test")

    assert ts.list_input_history(db, thread_id) == ["first\nsecond"]


def test_history_uses_legacy_records_before_canonical_adoption_and_filters_synthetic(tmp_path):
    db, thread_id = make_thread(tmp_path)
    ts.append_message(db, thread_id, "user", "legacy prompt")
    db.append_event(
        event_id="cmd-start",
        thread_id=thread_id,
        type_="user_command.started",
        payload={"command": "$ echo hello", "command_id": "cmd"},
    )
    db.append_event(
        event_id="cmd-finish",
        thread_id=thread_id,
        type_="user_command.finished",
        payload={"command_id": "cmd", "success": True},
    )
    ts.append_message(
        db,
        thread_id,
        "user",
        "$ echo hello",
        extra={"tool_calls": [], "keep_user_turn": True, "user_command_type": "$"},
    )
    ts.append_message(
        db,
        thread_id,
        "user",
        "manager text",
        extra={"origin": "manager_message", "from_thread_id": "parent"},
    )
    ts.append_message(
        db,
        thread_id,
        "user",
        "generated request",
        extra={"synthetic_user_tool_request": True, "tool_calls": []},
    )
    ts.append_message(
        db,
        thread_id,
        "user",
        "/looks-like-command-but-is-a-consumed-answer",
        extra={
            "no_api": True,
            "keep_user_turn": True,
            "consumed_by_tool_call_id": "call-1",
        },
    )
    ts.record_submitted_command(db, thread_id, "/new", source="test")
    ts.append_message(db, thread_id, "user", "untracked after adoption")

    assert ts.list_input_history(db, thread_id) == ["legacy prompt", "$ echo hello", "/new"]


def test_legacy_history_skips_recent_synthetic_messages(tmp_path):
    db, thread_id = make_thread(tmp_path)
    ts.append_message(db, thread_id, "user", "older operator prompt")
    for index in range(70):
        ts.append_message(
            db,
            thread_id,
            "user",
            f"synthetic {index}",
            extra={"synthetic_user_tool_request": True, "tool_calls": []},
        )

    assert ts.list_input_history(db, thread_id, limit=1) == ["older operator prompt"]


def test_message_and_history_event_are_atomic(tmp_path, monkeypatch):
    db, thread_id = make_thread(tmp_path)
    import eggthreads.api as api

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "append_normal_user_message", fail)
    try:
        ts.append_submitted_user_message(db, thread_id, "not committed")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected append failure")

    assert ts.list_input_history(db, thread_id) == []
    count = db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE thread_id=? AND type='input.submitted'",
        (thread_id,),
    ).fetchone()[0]
    assert count == 0


def test_message_append_rolls_back_when_history_link_fails(tmp_path, monkeypatch):
    db, thread_id = make_thread(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("eggthreads.input_history._append_message_input_submission", fail)
    try:
        ts.append_submitted_user_message(db, thread_id, "not committed")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected history append failure")

    assert ts.list_input_history(db, thread_id) == []
    messages = ts.create_snapshot(db, thread_id)["messages"]
    assert [message["content"] for message in messages] == ["system"]


def test_navigator_restores_draft_and_clamps_ends():
    navigator = ts.InputHistoryNavigator(["first", "second\nline"])
    assert navigator.older("draft") == "second\nline"
    assert navigator.older("ignored") == "first"
    assert navigator.older("ignored") == "first"
    assert navigator.newer() == "second\nline"
    assert navigator.newer() == "draft"
    assert navigator.newer() is None


def test_history_limit_preserves_deliberate_repeat_submissions(tmp_path):
    db, thread_id = make_thread(tmp_path)
    ts.record_submitted_command(db, thread_id, "same", source="test")
    ts.record_submitted_command(db, thread_id, "same", source="test")
    ts.record_submitted_command(db, thread_id, "latest", source="test")

    assert ts.list_input_history(db, thread_id, limit=2) == ["same", "latest"]


def test_message_history_follows_effective_edits_and_deletes(tmp_path):
    db, thread_id = make_thread(tmp_path)
    edited = ts.append_submitted_user_message(db, thread_id, "before", source="test")
    deleted = ts.append_submitted_user_message(db, thread_id, "delete me", source="test")

    ts.edit_message(db, thread_id, edited, "after")
    ts.delete_message(db, thread_id, deleted)

    assert ts.list_input_history(db, thread_id) == ["after"]


def test_deleted_recent_message_does_not_hide_older_history_at_limit(tmp_path):
    db, thread_id = make_thread(tmp_path)
    ts.append_submitted_user_message(db, thread_id, "oldest", source="test")
    ts.append_submitted_user_message(db, thread_id, "middle", source="test")
    deleted = ts.append_submitted_user_message(db, thread_id, "deleted", source="test")
    ts.delete_message(db, thread_id, deleted)

    assert ts.list_input_history(db, thread_id, limit=2) == ["oldest", "middle"]


def test_message_history_claimed_by_get_user_uses_same_linked_content(tmp_path):
    db, thread_id = make_thread(tmp_path)

    msg_id = ts.append_submitted_user_message(db, thread_id, "answer", source="test")
    ts.edit_message(
        db,
        thread_id,
        msg_id,
        "answer",
        extra={
            "no_api": True,
            "keep_user_turn": True,
            "consumed_by_tool_call_id": "call-1",
        },
    )

    assert ts.list_input_history(db, thread_id) == ["answer"]
