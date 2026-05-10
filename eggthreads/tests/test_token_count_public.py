from eggthreads import ThreadsDB, create_root_thread
from eggthreads.token_count import (
    count_text_tokens,
    extend_snapshot_token_stats,
    live_llm_tps_for_invoke,
    snapshot_token_stats,
)


def test_count_text_tokens_zero_for_empty_string():
    assert count_text_tokens("") == 0


def test_count_text_tokens_positive_for_nonempty_string():
    assert count_text_tokens("hello world") > 0


def test_live_llm_tps_for_invoke_counts_text_and_reasoning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ThreadsDB()
    db.init_schema()
    tid = create_root_thread(db, name="t")
    invoke = "inv1"
    db.append_event("e1", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m1", invoke_id=invoke)
    db.append_event("e2", tid, "stream.delta", {"text": "hello"}, invoke_id=invoke, chunk_seq=0)
    db.append_event("e3", tid, "stream.delta", {"reason": "think"}, invoke_id=invoke, chunk_seq=1)
    db.conn.execute("UPDATE events SET ts='2024-01-01T00:00:00.000Z' WHERE event_id='e2'")

    tps = live_llm_tps_for_invoke(db, invoke, end_ts=1735689600.0)
    assert isinstance(tps, float)
    assert tps > 0


def test_live_llm_tps_for_invoke_reuses_cache_for_unchanged_deltas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ThreadsDB()
    db.init_schema()
    tid = create_root_thread(db, name="t")
    invoke = "inv-cache"
    db.append_event("open", tid, "stream.open", {"stream_kind": "llm"}, msg_id="m1", invoke_id=invoke)
    db.append_event("delta", tid, "stream.delta", {"text": "hello"}, invoke_id=invoke, chunk_seq=0)
    db.conn.execute("UPDATE events SET ts='2024-01-01T00:00:00.000Z' WHERE event_id='delta'")

    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        first = live_llm_tps_for_invoke(db, invoke, end_ts=1735689600.0)
        statements.clear()
        second = live_llm_tps_for_invoke(db, invoke, end_ts=1735689601.0)
    finally:
        db.conn.set_trace_callback(None)

    assert isinstance(first, float)
    assert isinstance(second, float)
    assert not any("SELECT payload_json FROM events" in stmt for stmt in statements)


def test_extend_snapshot_token_stats_matches_full_recompute():
    snapshot = {
        "messages": [
            {"msg_id": "u1", "role": "user", "content": "hello"},
            {"msg_id": "a1", "role": "assistant", "content": "hi", "model_key": "m1"},
        ]
    }
    snapshot["token_stats"] = snapshot_token_stats(snapshot)
    tail = [
        {"msg_id": "u2", "role": "user", "content": "next"},
        {"msg_id": "a2", "role": "assistant", "content": "answer", "model_key": "m1"},
    ]
    snapshot["messages"].extend(tail)

    assert extend_snapshot_token_stats(snapshot, tail) == snapshot_token_stats(snapshot)


def test_total_token_stats_records_snapshot_context_boundary(tmp_path):
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "before snapshot")
    ts.create_snapshot(db, tid)
    ts.append_message(db, tid, "user", "after snapshot")

    stats = ts.total_token_stats(db, tid)

    assert stats["snapshot_context_tokens"] > 0
    assert stats["context_tokens"] > stats["snapshot_context_tokens"]
