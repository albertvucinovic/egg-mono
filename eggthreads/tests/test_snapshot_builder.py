"""Tests for :mod:`eggthreads.snapshot`.

These are intentionally fairly high level: we feed a sequence of
``msg.create`` events into :class:`SnapshotBuilder` and assert that the
resulting ``snapshot['messages']`` preserves important flags.

The flags under test (``no_api`` and ``keep_user_turn``) are used by
higher-level runners to decide which user messages should be visible to
the LLM and whether a turn should immediately trigger an assistant
call.  Regressions here are subtle but high impact, so we pin the
behaviour with dedicated tests.
"""

from __future__ import annotations

import json

from eggthreads import SnapshotBuilder
import eggthreads as ts


def _msg_create(event_seq: int, msg_id: str, payload: dict) -> dict:
    """Helper to build a minimal ``msg.create`` event dict.

    ``SnapshotBuilder.build`` only looks at a small subset of columns
    (``type``, ``msg_id``, ``event_seq``, ``payload_json``), so the
    helper keeps fixture data concise and focused.
    """

    return {
        "type": "msg.create",
        "msg_id": msg_id,
        "event_seq": event_seq,
        "payload_json": json.dumps(payload),
    }


def _msg_edit(event_seq: int, msg_id: str, payload: dict) -> dict:
    return {
        "type": "msg.edit",
        "msg_id": msg_id,
        "event_seq": event_seq,
        "payload_json": json.dumps(payload),
    }


def _msg_delete(event_seq: int, msg_id: str) -> dict:
    return {
        "type": "msg.delete",
        "msg_id": msg_id,
        "event_seq": event_seq,
        "payload_json": json.dumps({"reason": "user"}),
    }


def test_snapshot_preserves_no_api_and_keep_user_turn_for_user_messages() -> None:
    """``no_api`` and ``keep_user_turn`` flags must survive snapshots.

    The `$` / `$$` command handling in the front-end app relies on
    these flags when reconstructing LLM context.  If they were dropped
    during snapshot building, hidden user commands could leak into
    provider API calls or turns that should keep control with the user
    could accidentally trigger assistant calls.
    """

    builder = SnapshotBuilder()

    events = [
        _msg_create(
            1,
            "msg_hidden",
            {
                "role": "user",
                "content": "$$ echo hidden",
                "no_api": True,
                "keep_user_turn": True,
            },
        ),
        _msg_create(
            2,
            "msg_visible",
            {
                "role": "user",
                "content": "$ echo visible",
                "keep_user_turn": True,
            },
        ),
    ]

    snapshot = builder.build(events)
    messages = snapshot.get("messages", [])

    assert len(messages) == 2

    first, second = messages

    # First message ($$ command) should keep both flags.
    assert first["role"] == "user"
    assert first["content"] == "$$ echo hidden"
    assert first.get("no_api") is True
    assert first.get("keep_user_turn") is True

    # Second message ($ command) should *not* have no_api, but should
    # keep keep_user_turn so the runner knows this turn does not
    # trigger an immediate LLM call.
    assert second["role"] == "user"
    assert second["content"] == "$ echo visible"
    assert "no_api" not in second or second.get("no_api") in (None, False)
    assert second.get("keep_user_turn") is True


def test_snapshot_applies_content_edit_without_dropping_provider_fields() -> None:
    builder = SnapshotBuilder()

    snapshot = builder.build([
        _msg_create(
            1,
            "msg_assistant",
            {
                "role": "assistant",
                "content": "old",
                "reasoning": "thought",
                "provider_specific": {"signature": "abc"},
            },
        ),
        _msg_edit(2, "msg_assistant", {"content": "new"}),
    ])

    message = snapshot["messages"][0]
    assert message["content"] == "new"
    assert message["reasoning"] == "thought"
    assert message["provider_specific"] == {"signature": "abc"}


def test_snapshot_excludes_deleted_messages() -> None:
    builder = SnapshotBuilder()

    snapshot = builder.build([
        _msg_create(1, "keep", {"role": "user", "content": "keep"}),
        _msg_create(2, "delete", {"role": "assistant", "content": "remove"}),
        _msg_delete(3, "delete"),
    ])

    assert [m["msg_id"] for m in snapshot["messages"]] == ["keep"]


def test_create_snapshot_appends_msg_create_tail_incrementally(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    first_id = ts.append_message(db, tid, "user", "first")
    ts.create_snapshot(db, tid)
    first_row = db.get_thread(tid)
    assert first_row is not None
    first_seq = first_row.snapshot_last_event_seq

    second_id = ts.append_message(db, tid, "assistant", "second", extra={"provider_specific": {"signature": "abc"}})

    token_calls = []

    def fake_extend_token_stats(snapshot, tail_messages):
        token_calls.append([m["msg_id"] for m in tail_messages])
        return {"extended": True}

    def fail_full_rebuild(self, events):
        raise AssertionError("full snapshot rebuild should not run for append-only msg.create tail")

    monkeypatch.setattr("eggthreads.api.SnapshotBuilder.build", fail_full_rebuild)
    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fake_extend_token_stats)
    snapshot = ts.create_snapshot(db, tid)

    assert [m["msg_id"] for m in snapshot["messages"]] == [first_id, second_id]
    assert snapshot["messages"][-1]["provider_specific"] == {"signature": "abc"}
    assert snapshot["token_stats"] == {"extended": True}
    assert token_calls == [[second_id]]
    assert db.get_thread(tid).snapshot_last_event_seq > first_seq


def test_create_snapshot_returns_cached_snapshot_when_tail_empty(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    msg_id = ts.append_message(db, tid, "user", "first")
    ts.create_snapshot(db, tid)

    def fail_full_rebuild(self, events):
        raise AssertionError("full snapshot rebuild should not run when snapshot is current")

    monkeypatch.setattr("eggthreads.api.SnapshotBuilder.build", fail_full_rebuild)
    snapshot = ts.create_snapshot(db, tid)

    assert [m["msg_id"] for m in snapshot["messages"]] == [msg_id]
    assert db.get_thread(tid).snapshot_last_event_seq == db.max_event_seq(tid)


def test_create_snapshot_advances_ignored_event_tail_incrementally(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    msg_id = ts.append_message(db, tid, "user", "first")
    ts.create_snapshot(db, tid)
    first_row = db.get_thread(tid)
    assert first_row is not None
    first_seq = first_row.snapshot_last_event_seq

    db.append_event("stream-open", tid, "stream.open", {"stream_kind": "tool"}, msg_id="stream-msg", invoke_id="inv")
    db.append_event("stream-delta", tid, "stream.delta", {"tool": {"text": "out"}}, invoke_id="inv", chunk_seq=0)
    db.append_event("tool-summary", tid, "tool_call.summary", {"tool_call_id": "tc", "summary": "running"})
    db.append_event("stream-close", tid, "stream.close", {}, invoke_id="inv")

    def fail_full_rebuild(self, events):
        raise AssertionError("full snapshot rebuild should not run for ignored event tail")

    def fail_extend_token_stats(snapshot, tail_messages):
        raise AssertionError("token stats should not extend when no messages were appended")

    monkeypatch.setattr("eggthreads.api.SnapshotBuilder.build", fail_full_rebuild)
    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fail_extend_token_stats)
    snapshot = ts.create_snapshot(db, tid)

    row = db.get_thread(tid)
    assert row is not None
    assert row.snapshot_last_event_seq > first_seq
    assert row.snapshot_last_event_seq == db.max_event_seq(tid)
    assert [m["msg_id"] for m in snapshot["messages"]] == [msg_id]


def test_create_snapshot_appends_mixed_ignored_and_msg_create_tail_incrementally(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    first_id = ts.append_message(db, tid, "user", "first")
    ts.create_snapshot(db, tid)

    db.append_event("stream-open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="stream-msg", invoke_id="inv")
    db.append_event("stream-delta", tid, "stream.delta", {"text": "second"}, invoke_id="inv", chunk_seq=0)
    second_id = ts.append_message(db, tid, "assistant", "second", extra={"provider_specific": {"signature": "abc"}})
    db.append_event("stream-close", tid, "stream.close", {}, invoke_id="inv")

    token_calls = []

    def fake_extend_token_stats(snapshot, tail_messages):
        token_calls.append([m["msg_id"] for m in tail_messages])
        return {"extended": True}

    def fail_full_rebuild(self, events):
        raise AssertionError("full snapshot rebuild should not run for mixed ignored/msg.create tail")

    monkeypatch.setattr("eggthreads.api.SnapshotBuilder.build", fail_full_rebuild)
    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fake_extend_token_stats)
    snapshot = ts.create_snapshot(db, tid)

    assert [m["msg_id"] for m in snapshot["messages"]] == [first_id, second_id]
    assert snapshot["messages"][-1]["provider_specific"] == {"signature": "abc"}
    assert snapshot["token_stats"] == {"extended": True}
    assert token_calls == [[second_id]]
    assert db.get_thread(tid).snapshot_last_event_seq == db.max_event_seq(tid)


def test_create_snapshot_falls_back_to_full_rebuild_for_edits(tmp_path) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    msg_id = ts.append_message(db, tid, "user", "old", extra={"provider_specific": {"signature": "abc"}})
    ts.create_snapshot(db, tid)
    ts.edit_message(db, tid, msg_id, "new")

    snapshot = ts.create_snapshot(db, tid)
    message = snapshot["messages"][0]

    assert message["content"] == "new"
    assert message["provider_specific"] == {"signature": "abc"}
