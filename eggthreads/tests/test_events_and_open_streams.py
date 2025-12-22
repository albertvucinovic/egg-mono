"""Tests for event helpers and open_streams lease handling in eggthreads.

These are library-local equivalents of the functional tests used by
the Egg app, ensuring that eggthreads can be tested on its own.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from eggthreads import ThreadsDB
from eggthreads import events as events_module


def _make_temp_db(tmp_path) -> ThreadsDB:
    db = ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_event_factories_round_trip() -> None:
    """Basic sanity check for :mod:`eggthreads.events` helpers."""

    # stream.open
    ev_open = events_module.ev_stream_open(
        event_id="e-open",
        thread_id="t-1",
        msg_id="m-1",
        invoke_id="inv-1",
    )
    assert ev_open["type"] == "stream.open"
    assert ev_open["event_id"] == "e-open"
    assert ev_open["thread_id"] == "t-1"
    assert ev_open["msg_id"] == "m-1"
    assert ev_open["invoke_id"] == "inv-1"
    assert isinstance(json.loads(ev_open["payload_json"]), dict)

    # stream.delta
    delta_payload: Dict[str, Any] = {"text": "hello"}
    ev_delta = events_module.ev_stream_delta(
        event_id="e-delta",
        thread_id="t-1",
        invoke_id="inv-1",
        chunk_seq=0,
        delta=delta_payload,
    )
    assert ev_delta["type"] == "stream.delta"
    assert ev_delta["invoke_id"] == "inv-1"
    assert ev_delta["chunk_seq"] == 0
    assert json.loads(ev_delta["payload_json"]) == delta_payload

    # msg.create merges extra fields
    ev_msg = events_module.ev_msg_create(
        event_id="e-msg",
        thread_id="t-1",
        msg_id="m-2",
        role="user",
        content="hi",
        extra={"foo": "bar"},
    )
    payload = json.loads(ev_msg["payload_json"])
    assert payload["role"] == "user"
    assert payload["content"] == "hi"
    assert payload["foo"] == "bar"


def test_open_streams_lease_heartbeat_and_release(tmp_path) -> None:
    """Exercise try_open_stream/heartbeat/release on ThreadsDB.open_streams."""

    db = _make_temp_db(tmp_path)

    tid = "thread-open-streams"
    db.create_thread(thread_id=tid, name="Lease test", parent_id=None, depth=0)

    ok = db.try_open_stream(
        thread_id=tid,
        invoke_id="inv-lease",
        lease_until_iso="2999-01-01 00:00:00",
        owner="tester",
        purpose="assistant_stream",
    )
    assert ok is True

    row = db.current_open(tid)
    assert row is not None
    assert row["thread_id"] == tid
    assert row["invoke_id"] == "inv-lease"

    assert db.heartbeat(tid, "inv-lease", "2999-01-02 00:00:00") is True
    assert db.release(tid, "inv-lease") is True
    assert db.current_open(tid) is None
