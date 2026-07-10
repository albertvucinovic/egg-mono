from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import eggthreads as ts


def _db(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _future(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_event_feed_returns_canonical_bounded_envelopes_and_cursor(tmp_path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="feed")
    msg_id = ts.append_message(db, thread_id, "user", "hello")
    invoke_id = "invoke-feed"
    assert db.try_open_stream(
        thread_id, invoke_id, _future(), owner="test", purpose="llm"
    )
    writer = db.invocation_writer(thread_id, invoke_id)
    writer.append_event(
        event_id="feed-open",
        type_="stream.open",
        msg_id="stream-message",
        payload={"stream_kind": "llm"},
    )
    writer.append_event(
        event_id="feed-delta-0",
        type_="stream.delta",
        chunk_seq=0,
        payload={"text": "a"},
    )

    feed = ts.ThreadEventFeed(db, batch_size=2)
    first = feed.read_after(thread_id, -1)
    second = feed.read_after(thread_id, first.cursor)

    assert len(first.events) == 2
    assert [event.event_seq for event in first.events + second.events] == sorted(
        event.event_seq for event in first.events + second.events
    )
    message = next(event for event in first.events + second.events if event.msg_id == msg_id)
    assert message.as_dict() == {
        "event_id": message.event_id,
        "event_seq": message.event_seq,
        "type": "msg.create",
        "ts": message.ts,
        "msg_id": msg_id,
        "invoke_id": None,
        "chunk_seq": None,
        "payload": {"role": "user", "content": "hello"},
    }
    delta = next(event for event in first.events + second.events if event.type == "stream.delta")
    assert delta.invoke_id == invoke_id
    assert delta.chunk_seq == 0
    assert second.active_lease is not None
    assert second.active_lease.invoke_id == invoke_id


def test_event_cursor_precedence_and_validation() -> None:
    assert ts.resolve_event_cursor(after_seq="8", last_event_id="7", default=3) == 8
    assert ts.resolve_event_cursor(after_seq=None, last_event_id="7", default=3) == 7
    assert ts.resolve_event_cursor(after_seq=None, last_event_id=None, default=3) == 3
    for value in ("", "abc", "-2", "1.0", True):
        with pytest.raises(ts.ThreadEventCursorError):
            ts.parse_event_cursor(value)


def test_active_replay_uses_only_unexpired_exact_invocation_lease(tmp_path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="active")
    db.append_event(
        "stale-open",
        thread_id,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="stale-message",
        invoke_id="stale-invoke",
    )
    feed = ts.ThreadEventFeed(db)
    assert feed.active_replay_after_seq(thread_id) is None

    # An expired lease is not active, even when its unmatched open is newest.
    db.conn.execute(
        "INSERT INTO open_streams(thread_id, invoke_id, lease_until, purpose) "
        "VALUES (?, ?, datetime('now', '-1 second'), 'llm')",
        (thread_id, "expired-invoke"),
    )
    db.append_event(
        "expired-open",
        thread_id,
        "stream.open",
        {"stream_kind": "llm"},
        msg_id="expired-message",
        invoke_id="expired-invoke",
    )
    assert feed.active_replay_after_seq(thread_id) is None

    db.conn.execute("DELETE FROM open_streams WHERE thread_id=?", (thread_id,))
    assert db.try_open_stream(
        thread_id, "live-invoke", _future(), owner="test", purpose="tool"
    )
    live_open = db.invocation_writer(thread_id, "live-invoke").append_event(
        event_id="live-open",
        type_="stream.open",
        msg_id="live-message",
        payload={"stream_kind": "tool"},
    )
    assert feed.active_replay_after_seq(thread_id) == live_open - 1


def test_event_feed_batch_size_is_a_hard_bound(tmp_path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="bounded")
    for index in range(5):
        ts.append_message(db, thread_id, "user", str(index))
    feed = ts.ThreadEventFeed(db, batch_size=2)

    assert len(feed.read_after(thread_id, -1).events) == 2
    with pytest.raises(ValueError):
        feed.read_after(thread_id, -1, limit=3)


def test_event_feed_missing_thread_is_typed(tmp_path) -> None:
    feed = ts.ThreadEventFeed(_db(tmp_path))
    with pytest.raises(ts.ThreadEventFeedNotFound):
        feed.current_cursor("missing")
    with pytest.raises(ts.ThreadEventFeedNotFound):
        feed.read_after("missing", -1)


def test_replay_cursor_uses_active_open_or_idle_snapshot(tmp_path) -> None:
    db = _db(tmp_path)
    thread_id = ts.create_root_thread(db, name="replay-contract")
    feed = ts.ThreadEventFeed(db)
    idle_cursor = db.max_event_seq(thread_id)

    idle = feed.replay_cursor(thread_id, idle_cursor)
    assert idle.after_seq == idle_cursor
    assert idle.active_invoke_id is None
    assert idle.streaming_kind is None

    assert db.try_open_stream(
        thread_id, "active-invoke", _future(), owner="test", purpose="tool"
    )
    writer = db.invocation_writer(thread_id, "active-invoke")
    open_seq = writer.append_event(
        event_id="contract-open",
        type_="stream.open",
        msg_id="contract-message",
        payload={"stream_kind": "tool"},
    )
    writer.append_event(
        event_id="contract-delta",
        type_="stream.delta",
        chunk_seq=0,
        payload={"tool_call": {"id": "call-contract", "arguments_delta": "{}"}},
    )
    # Simulate a message projection cursor later than the active frames.
    later_snapshot_cursor = db.max_event_seq(thread_id)

    active = feed.replay_cursor(thread_id, later_snapshot_cursor)
    assert active.after_seq == open_seq - 1
    assert active.active_invoke_id == "active-invoke"
    assert active.streaming_kind == "tool"
    assert [event.event_seq for event in feed.read_after(thread_id, active.after_seq).events][:2] == [
        open_seq,
        open_seq + 1,
    ]
