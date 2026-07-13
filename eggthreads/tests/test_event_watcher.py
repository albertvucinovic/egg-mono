from __future__ import annotations

import asyncio

import eggthreads as ts


def test_event_watcher_pages_available_suffix_without_sleeping(tmp_path, monkeypatch) -> None:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    thread_id = ts.create_root_thread(db, name="root")
    start_after = db.max_event_seq(thread_id)
    expected = [
        db.append_event(f"event-{index}", thread_id, "stream.close", {})
        for index in range(7)
    ]

    watcher = ts.EventWatcher(db, thread_id, after_seq=start_after, batch_size=3)
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("eggthreads.event_watcher.asyncio.sleep", fake_sleep)

    async def collect():
        batches = []
        try:
            async for batch in watcher.aiter():
                batches.append([row["event_seq"] for row in batch])
        except asyncio.CancelledError:
            pass
        return batches

    batches = asyncio.run(collect())

    assert batches == [expected[:3], expected[3:6], expected[6:]]
    assert all(len(batch) <= 3 for batch in batches)
    assert [event_seq for batch in batches for event_seq in batch] == expected
    assert len(sleeps) == 1
