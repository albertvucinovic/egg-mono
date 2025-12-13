from __future__ import annotations

"""Tests for the headless_subtree_scheduler example.

These tests exercise the helper functions and main orchestration in
``eggthreads.examples.headless_subtree_scheduler`` to ensure the
example remains correct and importable as the core library evolves.

We deliberately avoid hitting real LLM providers:

* Helper functions (collect_subtree, word_count_*, list_active_threads,
  wait_subtree_idle) are tested directly against an in‑memory
  ``ThreadsDB`` created under a temporary directory.
* For the ``main()`` routine we monkeypatch ``SubtreeScheduler`` and
  the long‑running reporter / wait helpers so that the example runs to
  completion quickly without external dependencies, while still
  creating the expected thread subtree and seeding messages.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import eggthreads as ts
import eggthreads.examples.headless_subtree_scheduler as hs  # type: ignore


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_collect_subtree_bfs_order(tmp_path) -> None:
    """collect_subtree should return a BFS-ordered list of thread_ids.

    We build a small synthetic tree:

        root
          ├─ c1
          │   └─ gc
          └─ c2

    and assert that collect_subtree walks it breadth‑first starting
    from the given root.
    """

    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    c1 = ts.create_child_thread(db, root, name="c1")
    c2 = ts.create_child_thread(db, root, name="c2")
    gc = ts.create_child_thread(db, c1, name="gc")

    subtree = hs.collect_subtree(db, root)

    assert subtree[0] == root
    # All nodes in the tree must appear exactly once.
    assert set(subtree) == {root, c1, c2, gc}
    # Children of the root should appear before deeper descendants.
    # ULID timing means the exact order of c1/c2 is not guaranteed, so
    # we assert presence and that the grandchild comes last.
    assert set(subtree[1:3]) == {c1, c2}
    assert subtree[3] == gc


def test_word_count_snapshot_and_events_consistent_without_streaming(tmp_path) -> None:
    """word_count_from_events == word_count_from_snapshot when no new events.

    With no stream.delta events and no events after the snapshot,
    ``word_count_from_events`` should reduce to the pure snapshot word
    count.
    """

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="t")
    # Single user message
    ts.append_message(db, tid, "user", "hello world")
    ts.create_snapshot(db, tid)

    base = hs.word_count_from_snapshot(db, tid)
    total = hs.word_count_from_events(db, tid)
    assert total == base


def test_word_count_events_includes_post_snapshot_messages(tmp_path) -> None:
    """word_count_from_events should grow when new messages arrive after snapshot.

    After taking a snapshot, we append a new user message but do not
    rebuild the snapshot. ``word_count_from_events`` should include the
    new message's content words on top of the snapshot baseline.
    """

    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="t")
    ts.append_message(db, tid, "user", "base")
    ts.create_snapshot(db, tid)
    base = hs.word_count_from_snapshot(db, tid)

    # Append a user message *after* the snapshot; we intentionally do
    # not call create_snapshot() again so that word_count_from_events
    # includes it from the event log.
    extra_text = "three extra words"
    ts.append_message(db, tid, "user", extra_text)

    total = hs.word_count_from_events(db, tid)
    # The difference should be exactly the number of words in the new
    # message content, as the post-snapshot logic only counts
    # payload['content'] for role in ('user', 'tool').
    assert total - base == len(extra_text.split())


def test_list_active_threads_marks_open_streams(tmp_path) -> None:
    """list_active_threads should flag threads with an open stream as active."""

    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    c1 = ts.create_child_thread(db, root, name="c1")
    c2 = ts.create_child_thread(db, root, name="c2")

    subtree = [c1, c2]

    # No open streams and no runnable work -> no active threads.
    active_initial = hs.list_active_threads(db, subtree)
    assert active_initial == []

    # Mark c1 as having an open stream (simulating a running runner).
    lease_until = "2099-01-01 00:00:00"
    assert db.try_open_stream(c1, "invoke-1", lease_until, owner="test", purpose="test")

    active = hs.list_active_threads(db, subtree)
    assert active == [c1]


def test_wait_subtree_idle_returns_when_no_runnable_threads(tmp_path) -> None:
    """wait_subtree_idle should return quickly when nothing is runnable."""

    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    _ = ts.create_child_thread(db, root, name="c1")

    # No events and no open streams -> is_thread_runnable() is False
    # for all threads, so wait_subtree_idle should return after the
    # required number of quiet checks.
    asyncio.run(hs.wait_subtree_idle(db, root_id=root, poll_sec=0.01, quiet_checks=1))


def test_main_creates_expected_subtree(tmp_path, monkeypatch) -> None:
    """headless_subtree_scheduler.main builds the expected batch tree.

    We monkeypatch SubtreeScheduler and the long-running coroutines so
    that ``main()`` runs to completion quickly without needing a real
    LLM or external resources, then assert that the root and its child
    agents are created as documented.
    """

    # Run the example in an isolated working directory so its default
    # .egg/threads.sqlite does not interfere with other tests.
    monkeypatch.chdir(tmp_path)

    # Dummy scheduler that simply returns from run_forever almost
    # immediately instead of starting real runners.
    class DummyScheduler:
        def __init__(self, db, root_thread_id, config=None, models_path=None, all_models_path=None):  # type: ignore[no-untyped-def]
            self.db = db
            self.root_thread_id = root_thread_id

        async def run_forever(self, poll_sec: float = 0.05) -> None:  # pragma: no cover - tiny
            await asyncio.sleep(0)

    async def _dummy_reporter(db, root_id, interval_sec: float = 2.0) -> None:  # type: ignore[no-untyped-def]
        return None

    async def _dummy_wait_idle(db, root_id, poll_sec: float = 0.1, quiet_checks: int = 3) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(hs, "SubtreeScheduler", DummyScheduler)
    monkeypatch.setattr(hs, "periodic_reporter", _dummy_reporter)
    monkeypatch.setattr(hs, "wait_subtree_idle", _dummy_wait_idle)

    # Run the example's main coroutine.
    asyncio.run(hs.main())

    # Inspect the resulting database under the isolated cwd.
    db_path = tmp_path / ".egg" / "threads.sqlite"
    assert db_path.exists()

    db = ts.ThreadsDB(db_path)
    db.init_schema()

    threads = ts.list_threads(db)
    assert threads, "example main() should create at least one thread"

    # There should be exactly one root thread named "Batch Root".
    root_threads = [t for t in threads if ts.get_parent(db, t.thread_id) is None]
    assert len(root_threads) == 1
    root = root_threads[0]
    assert root.name == "Batch Root"

    # Its direct children are the batch agents created in main().
    children_meta = ts.list_children_with_meta(db, root.thread_id)
    # main() constructs ``range(1, num_tasks)`` with num_tasks=20 -> 19 agents.
    assert len(children_meta) == 19

    names = {name for (_tid, name, _recap, _created_at) in children_meta}
    assert "agent-001" in names
    assert "agent-019" in names

    # Each child should have a snapshot containing one system prompt
    # message and one user task message with the expected prefix.
    for tid, name, _recap, _created_at in children_meta:
        row = db.get_thread(tid)
        assert row is not None
        assert row.snapshot_json is not None
        snap = json.loads(row.snapshot_json)
        assert isinstance(snap, dict)
        msgs: List[Dict[str, Any]] = snap.get("messages", []) or []
        # system + user
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        # When running in a temp dir with no systemPrompt file,
        # load_system_prompt() falls back to SYSTEM_PROMPT_DEFAULT.
        assert msgs[0]["content"] == hs.SYSTEM_PROMPT_DEFAULT

        assert msgs[1]["role"] == "user"
        assert "Write a story named story_#" in msgs[1]["content"]
