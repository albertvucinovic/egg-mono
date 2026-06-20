from eggthreads import ThreadsDB, create_root_thread
from eggthreads.token_count import (
    APPROX_IMAGE_ATTACHMENT_TOKENS,
    _cost_for_usage,
    count_text_tokens,
    extend_snapshot_token_stats,
    live_llm_tps_for_invoke,
    snapshot_token_stats,
    thread_token_stats,
)

import pytest


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


def test_thread_token_stats_usage_since_compaction_uses_provider_context(tmp_path):
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "old prompt " * 20)
    start = ts.append_message(db, tid, "assistant", "summary")
    ts.commit_thread_compaction(db, tid, start, created_by="test")
    ts.append_message(db, tid, "user", "current prompt")
    ts.append_message(db, tid, "assistant", "current answer", extra={"model_key": "m"})
    ts.create_snapshot(db, tid)

    stats = thread_token_stats(db, tid)
    full_api = stats["api_usage"]
    current_api = stats["api_usage_since_compaction"]

    assert stats["full_thread_tokens"] > stats["context_tokens"]
    assert full_api["approx_call_count"] == 2
    assert current_api["approx_call_count"] == 1
    assert current_api["total_input_tokens"] < full_api["total_input_tokens"]


def test_full_api_usage_sums_compaction_epochs_not_raw_full_prompt(tmp_path):
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "old prompt " * 50)
    first_answer = ts.append_message(db, tid, "assistant", "first answer", extra={"model_key": "m"})
    ts.commit_thread_compaction(db, tid, first_answer, created_by="test")
    ts.append_message(db, tid, "user", "new prompt")
    ts.append_message(db, tid, "assistant", "second answer", extra={"model_key": "m"})
    snapshot = ts.create_snapshot(db, tid)

    raw_full = snapshot_token_stats(snapshot)["api_usage"]
    stats = thread_token_stats(db, tid)
    segmented = stats["api_usage"]

    assert segmented["approx_call_count"] == 2
    assert segmented["total_output_tokens"] == raw_full["total_output_tokens"]
    assert segmented["total_input_tokens"] < raw_full["total_input_tokens"]


def test_snapshot_token_stats_prefers_actual_assistant_api_usage():
    snapshot = {
        "messages": [
            {"msg_id": "u1", "role": "user", "content": "hello"},
            {
                "msg_id": "a1",
                "role": "assistant",
                "content": "short local text should not determine output usage",
                "model_key": "m",
                "api_usage": {
                    "total_input_tokens": 1000,
                    "total_image_input_tokens": 120,
                    "cached_input_tokens": 600,
                    "cache_creation_input_tokens": 40,
                    "total_output_tokens": 50,
                    "total_reasoning_tokens": 7,
                },
            },
        ]
    }

    usage = snapshot_token_stats(snapshot)["api_usage"]

    assert usage["total_input_tokens"] == 1000
    assert usage["total_image_input_tokens"] == 120
    assert usage["cached_input_tokens"] == 600
    assert usage["cache_creation_input_tokens"] == 40
    assert usage["total_output_tokens"] == 50
    assert usage["total_reasoning_tokens"] == 7
    assert usage["approx_call_count"] == 1
    assert usage["actual_call_count"] == 1
    assert usage["estimated_call_count"] == 0
    assert usage["api_confirmed_usage"]["actual_call_count"] == 1
    assert usage["api_confirmed_usage"]["total_input_tokens"] == 1000
    assert usage["api_confirmed_usage"]["total_image_input_tokens"] == 120
    assert usage["api_confirmed_usage"]["cached_input_tokens"] == 600
    assert usage["api_confirmed_usage"]["cache_creation_input_tokens"] == 40
    assert usage["api_confirmed_usage"]["total_output_tokens"] == 50
    assert usage["api_confirmed_usage"]["field_call_counts"]["total_input_tokens"] == 1
    assert usage["api_confirmed_usage"]["field_call_counts"]["total_image_input_tokens"] == 1
    assert usage["cached_tokens"] == 1000
    assert usage["last_call_input_tokens"] == 1000
    assert usage["by_model"]["m"]["actual_call_count"] == 1
    assert usage["by_model"]["m"]["estimated_call_count"] == 0
    assert usage["by_model"]["m"]["cache_creation_input_tokens"] == 40


def test_thread_token_stats_recomputes_old_cached_stats_for_confirmed_usage(tmp_path):
    import json
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.append_message(
        db,
        tid,
        "assistant",
        "answer",
        extra={
            "model_key": "m",
            "api_usage": {
                "total_input_tokens": 10,
                "cached_input_tokens": 4,
                "total_output_tokens": 2,
            },
        },
    )
    snap = ts.create_snapshot(db, tid)
    old_stats = dict(snap["token_stats"])
    old_stats["api_usage"] = {k: v for k, v in old_stats["api_usage"].items() if k != "api_confirmed_usage"}
    snap["token_stats"] = old_stats
    db.conn.execute(
        "UPDATE threads SET snapshot_json=? WHERE thread_id=?",
        (json.dumps(snap), tid),
    )

    usage = thread_token_stats(db, tid)["api_usage"]

    assert usage["api_confirmed_usage"]["total_input_tokens"] == 10
    assert usage["api_confirmed_usage"]["cached_input_tokens"] == 4
    assert usage["api_confirmed_usage"]["total_output_tokens"] == 2


def test_snapshot_token_stats_preserves_heuristic_usage_without_api_usage():
    messages = [
        {"msg_id": "u1", "role": "user", "content": "hello"},
        {"msg_id": "a1", "role": "assistant", "content": "answer", "model_key": "m"},
        {"msg_id": "u2", "role": "user", "content": "next"},
        {"msg_id": "a2", "role": "assistant", "content": "second", "model_key": "m"},
    ]

    usage = snapshot_token_stats({"messages": messages})["api_usage"]

    input_1 = count_text_tokens("hello")
    input_2 = input_1 + count_text_tokens("answer") + count_text_tokens("next")
    output_total = count_text_tokens("answer") + count_text_tokens("second")
    assert usage["total_input_tokens"] == input_1 + input_2
    assert usage["total_output_tokens"] == output_total
    assert usage["cached_input_tokens"] == min(input_1, input_2)
    assert usage["approx_call_count"] == 2
    assert usage["actual_call_count"] == 0
    assert usage["estimated_call_count"] == 2
    assert usage["api_confirmed_usage"] == {"actual_call_count": 0, "field_call_counts": {}}
    assert usage["by_model"]["m"]["estimated_call_count"] == 2


def _image_attachment_part():
    return {
        "type": "attachment",
        "input_id": "abc12345",
        "owner_thread_id": "thread",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "pixel.png",
        "size_bytes": 10,
        "sha256": "0123456789abcdef" * 4,
        "options": {},
    }


def test_snapshot_token_stats_counts_image_attachment_fixed_budget():
    messages = [
        {"msg_id": "u1", "role": "user", "content": [{"type": "text", "text": "look"}, _image_attachment_part()]},
        {"msg_id": "a1", "role": "assistant", "content": "answer", "model_key": "m"},
    ]

    stats = snapshot_token_stats({"messages": messages})

    expected_user_tokens = count_text_tokens("look") + APPROX_IMAGE_ATTACHMENT_TOKENS
    assert stats["per_message"]["u1"]["image_tokens"] == APPROX_IMAGE_ATTACHMENT_TOKENS
    assert stats["per_message"]["u1"]["content_tokens"] == expected_user_tokens
    assert stats["api_usage"]["total_input_tokens"] == expected_user_tokens
    assert stats["api_usage"]["total_image_input_tokens"] == APPROX_IMAGE_ATTACHMENT_TOKENS
    assert stats["api_usage"]["by_model"]["m"]["total_image_input_tokens"] == APPROX_IMAGE_ATTACHMENT_TOKENS


def test_thread_cost_includes_heuristic_image_input_tokens(tmp_path):
    import eggthreads as ts

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", [{"type": "text", "text": "look"}, {**_image_attachment_part(), "owner_thread_id": tid}])
    ts.append_message(db, tid, "assistant", "answer", extra={"model_key": "m"})
    ts.create_snapshot(db, tid)

    stats = thread_token_stats(db, tid, llm=_CostLLM({"input_tokens": 1.0, "output_tokens": 0.0}))

    usage = stats["api_usage"]
    assert usage["total_input_tokens"] >= APPROX_IMAGE_ATTACHMENT_TOKENS
    assert usage["total_image_input_tokens"] == APPROX_IMAGE_ATTACHMENT_TOKENS
    assert usage["cost_usd"]["by_model"]["m"]["input"] >= APPROX_IMAGE_ATTACHMENT_TOKENS / 1_000_000


class _CostLLM:
    def __init__(self, cost):
        self.cost = cost

    def current_model_cost_config(self, model_key):
        return self.cost


def test_cost_calculation_uses_cached_and_cache_creation_tiers():
    usage = {
        "total_input_tokens": 1000,
        "cached_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "cache_creation_5m_input_tokens": 40,
        "total_output_tokens": 50,
    }
    llm = _CostLLM({
        "input_tokens": 1.0,
        "cached_input": 0.1,
        "cache_creation_input": 2.0,
        "cache_creation_5m_input": 3.0,
        "output_tokens": 4.0,
    })

    cost = _cost_for_usage(usage, model_key="m", llm=llm)

    assert cost["input"] == pytest.approx(300 / 1_000_000)
    assert cost["cached"] == pytest.approx(600 * 0.1 / 1_000_000)
    assert cost["cache_creation"] == pytest.approx(((60 * 2.0) + (40 * 3.0)) / 1_000_000)
    assert cost["output"] == pytest.approx(50 * 4.0 / 1_000_000)
    assert cost["total"] == pytest.approx(cost["input"] + cost["cached"] + cost["cache_creation"] + cost["output"])


def test_cache_creation_cost_falls_back_to_input_price():
    usage = {
        "total_input_tokens": 1000,
        "cached_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "total_output_tokens": 0,
    }
    llm = _CostLLM({"input_tokens": 1.0, "cached_input": 0.1, "output_tokens": 4.0})

    cost = _cost_for_usage(usage, model_key="m", llm=llm)

    assert cost["input"] == pytest.approx(300 / 1_000_000)
    assert cost["cached"] == pytest.approx(600 * 0.1 / 1_000_000)
    assert cost["cache_creation"] == pytest.approx(100 / 1_000_000)
    assert cost["total"] == pytest.approx((300 + 60 + 100) / 1_000_000)
