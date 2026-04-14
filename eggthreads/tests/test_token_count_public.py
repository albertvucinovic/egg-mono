from eggthreads import ThreadsDB, create_root_thread
from eggthreads.token_count import count_text_tokens, live_llm_tps_for_invoke


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
    db.conn.execute("UPDATE events SET ts='2024-01-01T00:00:00.000Z' WHERE event_id='e1'")

    tps = live_llm_tps_for_invoke(db, invoke, end_ts=1735689600.0)
    assert isinstance(tps, float)
    assert tps > 0