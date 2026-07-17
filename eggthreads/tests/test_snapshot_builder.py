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

import asyncio
import json
import threading

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


def _fail_canonical_projection(*args, **kwargs):
    raise AssertionError("canonical projection should not run for a coherent safe snapshot path")


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


def test_create_snapshot_fast_tail_carries_pending_optimizer_metadata(tmp_path, monkeypatch) -> None:
    """Snapshot extension applies serialized approval state without replay."""

    db = ts.ThreadsDB(tmp_path / "optimizer-tail.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="optimizer-tail")
    tool_call_id = "optimizer-tail-call"
    db.append_event(
        "optimizer-tail-approval",
        tid,
        "tool_call.output_approval",
        {
            "tool_call_id": tool_call_id,
            "decision": "whole",
            "channels": {
                "raw": {"stored_in_finished_event": True},
                "optimizer": {
                    "optimized": True,
                    "fallback": False,
                    "published_savings_pct": 89.0,
                },
            },
        },
    )
    ts.create_snapshot(db, tid)
    msg_id = ts.append_message(
        db,
        tid,
        "tool",
        "late output",
        extra={"name": "bash", "tool_call_id": tool_call_id},
    )

    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
    snapshot = ts.create_snapshot(db, tid)

    message = next(item for item in snapshot["messages"] if item["msg_id"] == msg_id)
    assert message["output_optimizer"]["savings_pct"] == 89.0
    assert tool_call_id not in snapshot["_thread_projection"][
        "pending_optimizer_metadata"
    ]


def test_create_snapshot_fast_tail_updates_pending_optimizer_order(tmp_path, monkeypatch) -> None:
    """Successful tail approvals update state; quiet approvals cannot erase it."""

    db = ts.ThreadsDB(tmp_path / "optimizer-tail-order.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="optimizer-tail-order")
    tool_call_id = "optimizer-tail-order-call"
    ts.append_message(db, tid, "user", "anchor")
    ts.create_snapshot(db, tid)
    for event_id, payload in (
        (
            "optimizer-tail-success",
            {
                "tool_call_id": tool_call_id,
                "decision": "whole",
                "channels": {
                    "optimizer": {
                        "optimized": True,
                        "fallback": False,
                        "published_savings_pct": 87.0,
                    },
                },
            },
        ),
        (
            "optimizer-tail-default",
            {"tool_call_id": tool_call_id, "decision": "whole"},
        ),
    ):
        db.append_event(event_id, tid, "tool_call.output_approval", payload)
    msg_id = ts.append_message(
        db,
        tid,
        "tool",
        "ordered output",
        extra={"name": "bash", "tool_call_id": tool_call_id},
    )

    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
    snapshot = ts.create_snapshot(db, tid)

    message = next(item for item in snapshot["messages"] if item["msg_id"] == msg_id)
    assert message["output_optimizer"]["savings_pct"] == 87.0


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

    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fake_extend_token_stats)
    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
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

    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
    monkeypatch.setattr(
        "eggthreads.api._snapshot_from_projection",
        _fail_canonical_projection,
    )
    monkeypatch.setattr("eggthreads.api.json.dumps", _fail_canonical_projection)
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

    def fail_extend_token_stats(snapshot, tail_messages):
        raise AssertionError("token stats should not extend when no messages were appended")

    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fail_extend_token_stats)
    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
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

    monkeypatch.setattr("eggthreads.token_count.extend_snapshot_token_stats", fake_extend_token_stats)
    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
    snapshot = ts.create_snapshot(db, tid)

    assert [m["msg_id"] for m in snapshot["messages"]] == [first_id, second_id]
    assert snapshot["messages"][-1]["provider_specific"] == {"signature": "abc"}
    assert snapshot["token_stats"] == {"extended": True}
    assert token_calls == [[second_id]]
    assert db.get_thread(tid).snapshot_last_event_seq == db.max_event_seq(tid)
    canonical = ts.load_thread_projection(
        db,
        tid,
        db.max_event_seq(tid),
        use_snapshot=False,
    ).to_snapshot_dict()
    assert snapshot["messages"] == canonical["messages"]
    assert snapshot["_thread_projection"] == canonical["_thread_projection"]


def test_create_snapshot_tail_work_does_not_materialize_historical_projection(tmp_path, monkeypatch) -> None:
    """A one-message tail must not rebuild old projected messages."""

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    old_ids = [ts.append_message(db, tid, "user", f"old-{index}") for index in range(250)]
    ts.create_snapshot(db, tid)
    new_id = ts.append_message(db, tid, "assistant", "tail")

    monkeypatch.setattr("eggthreads.projection.load_thread_projection", _fail_canonical_projection)
    monkeypatch.setattr(
        "eggthreads.projection.ProjectedMessage._from_state_dict",
        _fail_canonical_projection,
    )
    snapshot = ts.create_snapshot(db, tid)

    assert [message["msg_id"] for message in snapshot["messages"]] == [*old_ids, new_id]


def test_create_snapshot_unknown_tail_event_falls_back_to_canonical_projection(tmp_path, monkeypatch) -> None:
    """New event types must be reviewed rather than silently fast-pathed."""

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    msg_id = ts.append_message(db, tid, "user", "first")
    ts.create_snapshot(db, tid)
    db.append_event("future-event", tid, "future.message_semantics", {"value": 1})

    import eggthreads.projection as projection_module

    calls = []
    original = projection_module.load_thread_projection

    def capture_projection(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(projection_module, "load_thread_projection", capture_projection)
    snapshot = ts.create_snapshot(db, tid)

    assert calls == [True]
    assert [message["msg_id"] for message in snapshot["messages"]] == [msg_id]
    assert db.get_thread(tid).snapshot_last_event_seq == db.max_event_seq(tid)


def test_create_snapshot_async_uses_worker_owned_connection(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    msg_id = ts.append_message(db, tid, "user", "hello")
    caller_thread = threading.get_ident()
    calls = []

    import eggthreads.api as api_module

    original = api_module.create_snapshot

    def capture_snapshot(worker_db, thread_id):
        calls.append((threading.get_ident(), worker_db is db, worker_db.path, thread_id))
        return original(worker_db, thread_id)

    monkeypatch.setattr(api_module, "create_snapshot", capture_snapshot)
    snapshot = asyncio.run(api_module.create_snapshot_async(db, tid))

    assert [message["msg_id"] for message in snapshot["messages"]] == [msg_id]
    assert len(calls) == 1
    worker_thread, shared_connection, worker_path, worker_tid = calls[0]
    assert worker_thread != caller_thread
    assert shared_connection is False
    assert worker_path == db.path
    assert worker_tid == tid


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
