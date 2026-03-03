"""Functional-style tests for :mod:`eggthreads.db.ThreadsDB`.

These mirror the tests in the Egg application repo, but live alongside
the library so ``eggthreads`` can be tested in isolation.

The focus is on schema-level behaviour:

* creating and fetching threads with ``create_thread`` / ``get_thread``
* maintaining parent/child relationships via the ``children`` table
* appending events and tracking ``event_seq`` via ``append_event`` and
  ``max_event_seq``
* ensuring ON DELETE CASCADE cleans up related rows in ``events``,
  ``children`` and ``open_streams`` when a thread is deleted
"""

from __future__ import annotations

import json
from pathlib import Path

from eggthreads import ThreadsDB


def _make_temp_db(tmp_path) -> tuple[ThreadsDB, Path]:
    """Create a :class:`ThreadsDB` bound to a temporary SQLite file.

    Using a per-test database keeps tests isolated and allows easy
    inspection of the file on failure.
    """

    db_path = tmp_path / "threads.sqlite"
    db = ThreadsDB(db_path)
    db.init_schema()
    return db, db_path


def test_create_and_get_thread_round_trip(tmp_path) -> None:
    db, db_path = _make_temp_db(tmp_path)
    assert db_path.exists()

    tid = "thread-1"
    name = "Test thread"

    # Create as a root thread
    db.create_thread(thread_id=tid, name=name, parent_id=None, initial_model_key="model-x", depth=0)

    row = db.get_thread(tid)
    assert row is not None
    assert row.thread_id == tid
    assert row.name == name
    assert row.initial_model_key == "model-x"
    assert row.depth == 0
    # Newly created threads should start as active with no snapshot.
    assert row.status == "active"
    assert row.snapshot_json in (None, "")
    assert row.snapshot_last_event_seq == -1


def test_child_relationship_via_children_table(tmp_path) -> None:
    db, _ = _make_temp_db(tmp_path)

    parent = "parent-1"
    child = "child-1"

    db.create_thread(thread_id=parent, name="Parent", parent_id=None, depth=0)
    db.create_thread(thread_id=child, name="Child", parent_id=parent, depth=1)

    # children table should contain the link
    cur = db.conn.execute(
        "SELECT parent_id, child_id FROM children WHERE parent_id=? AND child_id=?",
        (parent, child),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == parent
    assert row[1] == child


def test_append_event_and_max_event_seq(tmp_path) -> None:
    db, _ = _make_temp_db(tmp_path)

    tid = "thread-events"
    db.create_thread(thread_id=tid, name=None, parent_id=None, depth=0)

    # Initially there should be no events.
    assert db.max_event_seq(tid) == -1

    payload = {"role": "user", "content": "hello"}
    seq1 = db.append_event("ev1", tid, "msg.create", payload, msg_id="m1")
    assert isinstance(seq1, int)
    assert db.max_event_seq(tid) >= seq1

    payload2 = {"role": "assistant", "content": "world"}
    seq2 = db.append_event("ev2", tid, "msg.create", payload2, msg_id="m2")
    assert seq2 > seq1
    assert db.max_event_seq(tid) >= seq2

    # Sanity-check the stored JSON payload for the last event.
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND event_id=?",
        (tid, "ev2"),
    )
    row = cur.fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored == payload2


def test_delete_thread_cascades_to_children_events_and_open_streams(tmp_path) -> None:
    db, _ = _make_temp_db(tmp_path)

    parent = "parent-x"
    child = "child-x"
    db.create_thread(thread_id=parent, name="Parent", parent_id=None, depth=0)
    db.create_thread(thread_id=child, name="Child", parent_id=parent, depth=1)

    # Insert an event and an open_streams row for the child.
    db.append_event("ev-child", child, "msg.create", {"role": "user", "content": "hi"}, msg_id="mc")
    db.conn.execute(
        "INSERT INTO open_streams(thread_id, invoke_id, last_chunk_seq, owner, purpose, lease_until) "
        "VALUES (?,?,?,?,?, datetime('now'))",
        (child, "inv-child", -1, "tester", "assistant_stream"),
    )

    # Sanity check preconditions.
    assert db.get_thread(child) is not None
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE thread_id=?", (child,)).fetchone()[0] == 1
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM children WHERE parent_id=? AND child_id=?",
            (parent, child),
        ).fetchone()[0]
        == 1
    )
    assert db.conn.execute("SELECT COUNT(*) FROM open_streams WHERE thread_id=?", (child,)).fetchone()[0] == 1

    # Delete child thread and ensure cascading clean-up.
    db.conn.execute("DELETE FROM threads WHERE thread_id=?", (child,))

    assert db.get_thread(child) is None
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE thread_id=?", (child,)).fetchone()[0] == 0
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM children WHERE parent_id=? AND child_id=?",
            (parent, child),
        ).fetchone()[0]
        == 0
    )
    assert db.conn.execute("SELECT COUNT(*) FROM open_streams WHERE thread_id=?", (child,)).fetchone()[0] == 0
