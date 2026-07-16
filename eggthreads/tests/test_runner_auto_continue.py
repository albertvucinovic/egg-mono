from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest

import eggthreads as ts
from eggthreads.runner import RunnerConfig


class _BoomLLM:
    current_model_key = "mock"

    def __init__(self, *errors: str):
        self.errors = list(errors)
        self.calls = 0

    def set_model(self, model_key):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.calls += 1
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        yield {"type": "done", "message": {"role": "assistant", "content": f"ok {self.calls}"}}


class _RateLimitThenOkLLM:
    current_model_key = "mock"

    def __init__(self):
        self.calls = 0

    def set_model(self, model_key):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 429 rate limit exceeded; retry after 0.01 seconds")
        yield {"type": "done", "message": {"role": "assistant", "content": "ok after rate limit"}}


class _PartialThenBoomLLM:
    current_model_key = "mock"

    def __init__(self):
        self.calls = 0

    def set_model(self, model_key):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "content_delta", "text": "partial"}
            raise RuntimeError(
                "Response payload is not completed <TransferEncodingError: 400, "
                "message='Not enough data to satisfy transfer length header.'>; retry after 0 seconds"
            )
        yield {"type": "done", "message": {"role": "assistant", "content": "ok after partial"}}


def _make_db(tmp_path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _make_thread(db: ts.ThreadsDB) -> tuple[str, str]:
    tid = ts.create_root_thread(db, name="auto-continue")
    user_msg_id = ts.append_message(db, tid, "user", "Hello")
    ts.create_snapshot(db, tid)
    return tid, user_msg_id


def _payloads(db: ts.ThreadsDB, tid: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _notices(db: ts.ThreadsDB, tid: str) -> list[dict]:
    return [payload for payload in _payloads(db, tid) if payload.get("recovery_notice")]


def _recovery_actions(db: ts.ThreadsDB, tid: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? "
        "AND type='thread.recovery_action' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _recovery_attempts(db: ts.ThreadsDB, tid: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? "
        "AND type='thread.recovery_attempt' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _runner_error_record(db: ts.ThreadsDB, tid: str) -> tuple[int, str, dict]:
    rows = db.conn.execute(
        "SELECT event_seq, msg_id, payload_json FROM events "
        "WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    for event_seq, msg_id, payload_json in rows:
        payload = json.loads(payload_json)
        if payload.get("runner_error"):
            return int(event_seq), str(msg_id), payload
    raise AssertionError("runner error source not found")


def _skipped_msg_ids(db: ts.ThreadsDB, tid: str) -> set[str]:
    rows = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    out: set[str] = set()
    for msg_id, payload_json in rows:
        payload = json.loads(payload_json)
        if payload.get("skipped_on_continue"):
            out.add(msg_id)
    return out


async def _run_until_idle(runner: ts.ThreadRunner, limit: int = 5) -> None:
    for _ in range(limit):
        if not await runner.run_once():
            return
    raise AssertionError("runner did not become idle")


def test_ra1_503_error_auto_continues_and_reruns_once(tmp_path):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _BoomLLM("HTTP 503 Service Unavailable; retry after 0 seconds")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    payloads = _payloads(db, tid)
    assert any(payload.get("role") == "assistant" and payload.get("content") == "ok 2" for payload in payloads)
    notices = _notices(db, tid)
    assert any(notice.get("action") == "scheduled" for notice in notices)
    assert any(notice.get("action") == "applied" and notice.get("trigger_msg_id") == user_msg_id for notice in notices)
    assert [action.get("action") for action in _recovery_actions(db, tid)] == ["scheduled", "applied"]
    assert len(_recovery_attempts(db, tid)) == 1
    assert any("Previous error summary: LLM/runner error: HTTP 503 Service Unavailable" in notice.get("content", "") for notice in notices)
    errors = [payload for payload in payloads if payload.get("runner_error")]
    assert errors and all(error.get("no_api") is True for error in errors)
    assert user_msg_id not in _skipped_msg_ids(db, tid)


def test_transfer_truncation_incomplete_auto_continues_from_trigger(tmp_path):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _PartialThenBoomLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    skipped = _skipped_msg_ids(db, tid)
    payloads = _payloads(db, tid)
    incomplete_ids = [
        row[0]
        for row in db.conn.execute(
            "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' AND payload_json LIKE '%incomplete%'",
            (tid,),
        ).fetchall()
    ]
    assert incomplete_ids and incomplete_ids[0] in skipped
    assert user_msg_id not in skipped
    assert any(payload.get("role") == "assistant" and payload.get("content") == "ok after partial" for payload in payloads)


def test_max_output_incomplete_metadata_queues_recovery_compaction_not_continue(tmp_path):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)

    class MaxOutputLLM:
        current_model_key = "mock"
        calls = 0

        def set_model(self, model_key):
            self.current_model_key = model_key

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            self.calls += 1
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "incomplete": True,
                    "incomplete_reason": "response.incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }

    llm = MaxOutputLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    payloads = _payloads(db, tid)
    assert any(
        payload.get("role") == "assistant"
        and payload.get("incomplete") is True
        and payload.get("incomplete_details") == {"reason": "max_output_tokens"}
        for payload in payloads
    )
    assert not any(notice.get("action") == "applied" for notice in _notices(db, tid))
    assert any(
        notice.get("action") == "compaction_scheduled" and notice.get("decision_category") == "max_output"
        for notice in _notices(db, tid)
    )
    request = next(payload for payload in payloads if payload.get("auto_compaction_request"))
    assert "checkpoint_and_resume" in request.get("content", "")

    source_msg_id = db.conn.execute(
        "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' AND payload_json LIKE '%max_output_tokens%' ORDER BY event_seq ASC LIMIT 1",
        (tid,),
    ).fetchone()[0]
    compaction_payload = json.loads(
        db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='thread.compaction' ORDER BY event_seq ASC LIMIT 1",
            (tid,),
        ).fetchone()[0]
    )
    assert compaction_payload["selector"] == source_msg_id
    assert compaction_payload["start_msg_id"] == source_msg_id


def test_max_output_from_compaction_request_does_not_queue_recursive_compaction(tmp_path):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    result = ts.append_auto_compaction_summary_request(db, tid, selector="last_user")
    assert result.success is True

    class MaxOutputLLM:
        current_model_key = "mock"
        calls = 0

        def set_model(self, model_key):
            self.current_model_key = model_key

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            self.calls += 1
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "incomplete": True,
                    "incomplete_reason": "response.incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }

    llm = MaxOutputLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    assert len(ts.list_thread_compactions(db, tid)) == 1
    assert any(notice.get("action") == "stopped" and notice.get("decision_category") == "max_output" for notice in _notices(db, tid))
    assert not any(notice.get("action") == "compaction_scheduled" for notice in _notices(db, tid))


def test_timeout_auto_continues_once(tmp_path):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    llm = _BoomLLM("asyncio.TimeoutError: provider read timeout; retry after 0 seconds")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    assert any(payload.get("role") == "assistant" and payload.get("content") == "ok 2" for payload in _payloads(db, tid))


def test_generic_400_does_not_auto_continue(tmp_path):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    llm = _BoomLLM("HTTP 400 Bad Request")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 1
    notices = _notices(db, tid)
    assert any(notice.get("action") == "stopped" and notice.get("decision_category") == "bad_request" for notice in notices)
    assert any("Previous error summary: LLM/runner error: HTTP 400 Bad Request" in notice.get("content", "") for notice in notices)
    assert ts.discover_runner_actionable(db, tid) is None


def test_context_length_path_does_not_auto_continue_directly(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _BoomLLM("should not be called")
    monkeypatch.setattr("eggthreads.token_count.thread_token_stats", lambda db_arg, tid_arg: {"context_tokens": 10})
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig(context_limit=1))

    asyncio.run(runner.run_once())

    assert llm.calls == 0
    notices = _notices(db, tid)
    assert not any(notice.get("auto_continue") for notice in notices)
    assert user_msg_id not in _skipped_msg_ids(db, tid)


def test_fence_cancels_if_newer_user_activity_appears(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    llm = _RateLimitThenOkLLM()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        ts.append_message(db, tid, "user", "newer activity")
        await real_sleep(0)

    monkeypatch.setattr("eggthreads.runner.asyncio.sleep", fake_sleep)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    notices = _notices(db, tid)
    assert any(notice.get("action") == "stopped" and "newer user" in notice.get("stop_reason", "") for notice in notices)
    assert ts.discover_runner_actionable(db, tid) is not None


def test_pending_auto_continue_cancels_after_manual_continue(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _RateLimitThenOkLLM()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        result = ts.continue_thread(db, tid, msg_id=user_msg_id)
        assert result.success is True
        await real_sleep(0)

    monkeypatch.setattr("eggthreads.runner.asyncio.sleep", fake_sleep)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    stopped = [notice for notice in _notices(db, tid) if notice.get("action") == "stopped"]
    assert any("continued manually" in notice.get("stop_reason", "") for notice in stopped)
    assert len([notice for notice in _notices(db, tid) if notice.get("action") == "applied"]) == 0


def test_pending_auto_continue_cancels_after_continue_interrupt_even_if_source_unskipped(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _RateLimitThenOkLLM()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        db.append_event(
            event_id=str(uuid.uuid4()),
            thread_id=tid,
            type_="control.interrupt",
            payload={"purpose": "continue", "reason": "continue", "continue_from_msg_id": user_msg_id},
        )
        await real_sleep(0)

    monkeypatch.setattr("eggthreads.runner.asyncio.sleep", fake_sleep)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    assert not _skipped_msg_ids(db, tid)
    stopped = [notice for notice in _notices(db, tid) if notice.get("action") == "stopped"]
    assert any("continued manually" in notice.get("stop_reason", "") for notice in stopped)
    assert len([notice for notice in _notices(db, tid) if notice.get("action") == "applied"]) == 0


def test_attempt_cap_prevents_loops(tmp_path):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _BoomLLM(
        "HTTP 503 Service Unavailable; retry after 0 seconds",
        "HTTP 503 Service Unavailable; retry after 0 seconds",
    )
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    applied = [notice for notice in _notices(db, tid) if notice.get("action") == "applied"]
    stopped = [notice for notice in _notices(db, tid) if notice.get("action") == "stopped"]
    assert len([notice for notice in applied if notice.get("trigger_msg_id") == user_msg_id]) == 1
    assert any(
        "attempt cap" in notice.get("stop_reason", "")
        or notice.get("stop_reason") == "attempt_cap"
        for notice in stopped
    )
    assert ts.discover_runner_actionable(db, tid) is None


def test_applied_action_failure_rolls_back_claim_and_continuation(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _BoomLLM("HTTP 503 Service Unavailable; retry after 0 seconds")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())
    original_append_event = db.append_event

    def fail_applied_action(*args, **kwargs):
        type_ = kwargs.get("type_")
        payload = kwargs.get("payload") or {}
        if type_ == "thread.recovery_action" and payload.get("action") == "applied":
            raise RuntimeError("applied action persistence failed")
        return original_append_event(*args, **kwargs)

    monkeypatch.setattr(db, "append_event", fail_applied_action)

    asyncio.run(runner.run_once())

    assert llm.calls == 1
    assert _recovery_attempts(db, tid) == []
    assert user_msg_id not in _skipped_msg_ids(db, tid)
    assert not [action for action in _recovery_actions(db, tid) if action.get("action") == "applied"]
    assert any(
        action.get("action") == "stopped"
        and action.get("stop_reason") == "transaction_failed"
        for action in _recovery_actions(db, tid)
    )



def test_attempt_count_reads_only_indexed_recovery_claim(tmp_path):
    from eggthreads.runner_recovery import (
        RECOVERY_ATTEMPT_EVENT_TYPE,
        recovery_attempt_count,
    )

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    for index in range(2_000):
        db.append_event(
            event_id=f"unrelated-{index}",
            thread_id=tid,
            type_="msg.create",
            msg_id=f"unrelated-msg-{index}",
            payload={
                "role": "system",
                "content": "unrelated",
                "recovery_notice": True,
                "auto_continue": True,
                "action": "applied",
                "trigger_msg_id": trigger_msg_id,
            },
        )
    db.append_event(
        event_id="canonical-applied",
        thread_id=tid,
        type_=RECOVERY_ATTEMPT_EVENT_TYPE,
        msg_id=trigger_msg_id,
        payload={"state": "claimed", "trigger_msg_id": trigger_msg_id},
    )

    seen_sql: list[str] = []
    db.conn.set_trace_callback(seen_sql.append)
    try:
        assert recovery_attempt_count(db, tid, trigger_msg_id) == 1
    finally:
        db.conn.set_trace_callback(None)

    selects = [sql for sql in seen_sql if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert "INDEXED BY events_msg_seq" in selects[0]
    assert "type='thread.recovery_attempt'" in selects[0]
    assert "msg_id=" in selects[0]
    assert "msg.create" not in selects[0]
    assert "json_extract" not in selects[0]
    assert "LIMIT 1" in selects[0]
    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT payload_json FROM events "
        "INDEXED BY events_msg_seq WHERE msg_id=? AND thread_id=? AND type=? LIMIT 1",
        (trigger_msg_id, tid, RECOVERY_ATTEMPT_EVENT_TYPE),
    ).fetchall()
    detail = " ".join(str(row[3]) for row in plan)
    assert "events_msg_seq" in detail
    assert "msg_id=?" in detail


def test_attempt_claim_detected_with_newer_nonclaim_actions(tmp_path):
    from eggthreads.runner_recovery import (
        RECOVERY_ACTION_EVENT_TYPE,
        RECOVERY_ATTEMPT_EVENT_TYPE,
        recovery_attempt_count,
    )

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    db.append_event(
        event_id="canonical-claim",
        thread_id=tid,
        type_=RECOVERY_ATTEMPT_EVENT_TYPE,
        msg_id=trigger_msg_id,
        payload={"state": "claimed", "trigger_msg_id": trigger_msg_id},
    )
    for index, action in enumerate(("scheduled", "stopped")):
        db.append_event(
            event_id=f"newer-action-{index}",
            thread_id=tid,
            type_=RECOVERY_ACTION_EVENT_TYPE,
            msg_id=trigger_msg_id,
            payload={"action": action, "trigger_msg_id": trigger_msg_id},
        )

    assert recovery_attempt_count(db, tid, trigger_msg_id) == 1


def test_attempt_count_recognizes_legacy_applied_notice(tmp_path):
    from eggthreads.runner_recovery import recovery_attempt_count

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    ts.append_message(
        db,
        tid,
        "system",
        "legacy applied notice",
        extra={
            "recovery_notice": True,
            "auto_continue": True,
            "action": "applied",
            "trigger_msg_id": trigger_msg_id,
        },
    )

    assert recovery_attempt_count(db, tid, trigger_msg_id) == 1


def test_saturated_legacy_window_with_older_applied_fails_closed(tmp_path):
    from eggthreads.runner_recovery import recovery_attempt_count

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    ts.append_message(
        db,
        tid,
        "system",
        "legacy applied notice",
        extra={
            "recovery_notice": True,
            "auto_continue": True,
            "action": "applied",
            "trigger_msg_id": trigger_msg_id,
        },
    )
    for index in range(256):
        db.append_event(
            event_id=f"newer-legacy-row-{index}",
            thread_id=tid,
            type_="msg.create",
            msg_id=f"newer-legacy-msg-{index}",
            payload={"role": "system", "content": "unrelated"},
        )

    assert recovery_attempt_count(db, tid, trigger_msg_id) == 1


def test_legacy_attempt_compatibility_scan_is_bounded(tmp_path):
    from eggthreads.runner_recovery import recovery_attempt_count

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    for index in range(2_000):
        db.append_event(
            event_id=f"legacy-unrelated-{index}",
            thread_id=tid,
            type_="msg.create",
            msg_id=f"legacy-unrelated-msg-{index}",
            payload={"role": "system", "content": "unrelated"},
        )

    seen_sql: list[str] = []
    db.conn.set_trace_callback(seen_sql.append)
    try:
        assert recovery_attempt_count(db, tid, trigger_msg_id) == 1
    finally:
        db.conn.set_trace_callback(None)

    selects = [sql for sql in seen_sql if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 3
    legacy_select = selects[-1]
    assert "INDEXED BY events_thread_type" in legacy_select
    assert "ORDER BY event_seq DESC LIMIT 256" in legacy_select


def test_long_failure_is_canonical_only_at_source_and_actions_are_bounded(tmp_path):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    marker = "EXACT-UNBOUNDED-TAIL-MARKER"
    long_error = (
        "remote connection failure; retry after 0 seconds; "
        + ("diagnostic-block-" * 8_000)
        + marker
    )
    llm = _BoomLLM(long_error)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    source_event_seq, source_msg_id, source = _runner_error_record(db, tid)
    assert source["content"] == f"LLM/runner error: {long_error}"
    assert source["content"].endswith(marker)

    actions = _recovery_actions(db, tid)
    assert [action["action"] for action in actions] == ["scheduled", "applied"]
    assert all(action["trigger_msg_id"] == user_msg_id for action in actions)
    action_rows = db.conn.execute(
        "SELECT msg_id FROM events WHERE thread_id=? "
        "AND type='thread.recovery_action' ORDER BY event_seq ASC",
        (tid,),
    ).fetchall()
    assert [str(row[0]) for row in action_rows] == [user_msg_id, user_msg_id]
    notices = [
        notice
        for notice in _notices(db, tid)
        if notice.get("action") in {"scheduled", "applied"}
    ]
    assert len(notices) == 2
    for payload in [*actions, *notices]:
        assert payload["trigger_msg_id"] == user_msg_id
        assert payload["source_msg_id"] == source_msg_id
        assert payload["source_event_seq"] == source_event_seq
        assert "source_detail" not in payload
        assert len(payload["source_summary"]) <= 240
        assert marker not in json.dumps(payload, ensure_ascii=False)
        assert len(json.dumps(payload, ensure_ascii=False)) < 2_000


def test_final_transaction_fence_keeps_injected_new_user_unskipped(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    llm = _BoomLLM("HTTP 503 Service Unavailable; retry after 0 seconds")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())
    from eggthreads.api import apply_auto_continue as original_apply
    injected: dict[str, str] = {}

    def inject_then_apply(*args, **kwargs):
        injected["msg_id"] = ts.append_message(db, tid, "user", "arrived after external fence")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr("eggthreads.api.apply_auto_continue", inject_then_apply)

    asyncio.run(runner.run_once())

    assert injected["msg_id"] not in _skipped_msg_ids(db, tid)
    assert _recovery_attempts(db, tid) == []
    assert any(
        notice.get("action") == "stopped" and "newer user" in notice.get("stop_reason", "")
        for notice in _notices(db, tid)
    )


def test_concurrent_auto_continue_callbacks_claim_and_apply_once(tmp_path):
    from eggthreads.api import apply_auto_continue
    from eggthreads.runner_recovery import classify_failure_text, format_auto_continue_notice

    db = _make_db(tmp_path)
    tid, trigger_msg_id = _make_thread(db)
    source_msg_id = ts.append_message(
        db,
        tid,
        "system",
        "LLM/runner error: HTTP 503 Service Unavailable",
        extra={"no_api": True, "runner_error": True},
    )
    source_event_seq = int(
        db.conn.execute(
            "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? "
            "AND type='msg.create'",
            (tid, source_msg_id),
        ).fetchone()[0]
    )
    decision = classify_failure_text("LLM/runner error: HTTP 503 Service Unavailable")
    payload = {
        "auto_continue": True,
        "trigger_msg_id": trigger_msg_id,
        "source_msg_id": source_msg_id,
        "source_event_seq": source_event_seq,
        "decision_category": decision.category,
        "decision_reason": decision.reason,
        "source_summary": decision.source_summary,
        "delay_sec": 0.0,
    }
    notice = format_auto_continue_notice(
        decision,
        action="applied",
        trigger_msg_id=trigger_msg_id,
        source_msg_id=source_msg_id,
    )
    barrier = threading.Barrier(2)
    outcomes: list[tuple[bool, str]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        local = ts.ThreadsDB(db.path)
        try:
            barrier.wait()
            result = apply_auto_continue(
                local,
                tid,
                trigger_msg_id=trigger_msg_id,
                source_msg_id=source_msg_id,
                source_event_seq=source_event_seq,
                action_payload=payload,
                notice_content=notice,
            )
            outcomes.append((result.applied, result.reason))
        except BaseException as exc:
            errors.append(exc)
        finally:
            local.conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert sorted(applied for applied, _reason in outcomes) == [False, True]
    assert len(_recovery_attempts(db, tid)) == 1
    assert len([action for action in _recovery_actions(db, tid) if action.get("action") == "applied"]) == 1


def test_compaction_action_failure_rolls_back_compaction_request(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)

    class MaxOutputLLM:
        current_model_key = "***"

        def set_model(self, model_key):
            self.current_model_key = model_key

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            yield {
                "type": "done",
                "message": {
                    "role": "assistant",
                    "incomplete": True,
                    "incomplete_reason": "response.incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }

    original_append_event = db.append_event

    def fail_compaction_action(*args, **kwargs):
        payload = kwargs.get("payload") or {}
        if kwargs.get("type_") == "thread.recovery_action" and payload.get("action") == "compaction_scheduled":
            raise RuntimeError("compaction action persistence failed")
        return original_append_event(*args, **kwargs)

    monkeypatch.setattr(db, "append_event", fail_compaction_action)
    runner = ts.ThreadRunner(db, tid, llm=MaxOutputLLM(), config=RunnerConfig())
    asyncio.run(runner.run_once())

    assert ts.list_thread_compactions(db, tid) == []
    assert not [payload for payload in _payloads(db, tid) if payload.get("auto_compaction_request")]
    stopped = [notice for notice in _notices(db, tid) if notice.get("action") == "stopped"]
    assert len(stopped) == 1
    assert stopped[0]["stop_reason"] == "compaction action persistence failed"
    assert not [notice for notice in _notices(db, tid) if notice.get("action") == "compaction_scheduled"]


def test_toggle_off_disables_auto_continue(tmp_path):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    ts.set_thread_recovery(db, tid, auto_continue_on_error=False)
    llm = _BoomLLM("HTTP 503 Service Unavailable")
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 1
    assert not _notices(db, tid)
    assert ts.discover_runner_actionable(db, tid) is None


OPENAI_RESPONSES_SERVER_ERROR = (
    "Responses API error (server_error): "
    "An error occurred while processing your request. "
    "You can retry your request, or contact us through our help center at "
    "help.openai.com if the error persists. Please include the request ID "
    "553755af-e48c-412a-b824-beee36624640 in your message."
)


@pytest.mark.parametrize(
    "error, category, expected_delay",
    [
        (
            OPENAI_RESPONSES_SERVER_ERROR + "\nRetry-After: 0",
            "server_error",
            0.0,
        ),
        (
            "remote connection failure; retry after 0 seconds; upstream reset request-id=req_transport_full",
            "transport",
            0.0,
        ),
    ],
)
def test_phase4_transient_errors_schedule_apply_and_retry_once(
    tmp_path, error, category, expected_delay
):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    llm = _BoomLLM(error)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    notices = _notices(db, tid)
    scheduled = [notice for notice in notices if notice.get("action") == "scheduled"]
    applied = [notice for notice in notices if notice.get("action") == "applied"]
    assert len(scheduled) == 1
    assert len(applied) == 1
    source_event_seq, source_msg_id, source = _runner_error_record(db, tid)
    assert source["content"] == f"LLM/runner error: {error}"
    for notice in (scheduled[0], applied[0]):
        assert notice["decision_category"] == category
        assert notice["decision_reason"]
        assert "source_detail" not in notice
        assert notice["source_summary"]
        assert len(notice["source_summary"]) <= 240
        assert notice["source_msg_id"] == source_msg_id
        assert notice["source_event_seq"] == source_event_seq
        assert notice["trigger_msg_id"] == user_msg_id
        assert notice["delay_sec"] == expected_delay
        assert len(notice["content"]) < 1_000
        if len(f"LLM/runner error: {error}") > 240:
            assert f"LLM/runner error: {error}" not in notice["content"]


def test_phase4_processing_retry_attempt_cap_stops_second_failure(tmp_path):
    db = _make_db(tmp_path)
    tid, user_msg_id = _make_thread(db)
    error = OPENAI_RESPONSES_SERVER_ERROR + "\nRetry-After: 0"
    llm = _BoomLLM(error, error)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 2
    notices = _notices(db, tid)
    assert len(
        [
            notice
            for notice in notices
            if notice.get("action") == "applied"
            and notice.get("trigger_msg_id") == user_msg_id
        ]
    ) == 1
    assert any(
        notice.get("action") == "stopped"
        and notice.get("stop_reason") == "attempt_cap"
        for notice in notices
    )


@pytest.mark.parametrize(
    "error, category",
    [
        (
            OPENAI_RESPONSES_SERVER_ERROR.replace("server_error", "invalid_request_error", 1),
            "invalid_request",
        ),
        (
            OPENAI_RESPONSES_SERVER_ERROR.replace("help.openai.com", "support.example.com"),
            "unknown",
        ),
        (
            OPENAI_RESPONSES_SERVER_ERROR.replace("You can retry your request, or ", ""),
            "unknown",
        ),
        (
            OPENAI_RESPONSES_SERVER_ERROR.replace(
                "553755af-e48c-412a-b824-beee36624640", "missing-request-id"
            ),
            "unknown",
        ),
        ("upstream rejected invalid request", "invalid_request"),
        ("remote connection refused: invalid api key", "auth"),
    ],
)
def test_phase4_nearby_permanent_or_ambiguous_errors_stop_without_retry(
    tmp_path, error, category
):
    db = _make_db(tmp_path)
    tid, _user_msg_id = _make_thread(db)
    llm = _BoomLLM(error)
    runner = ts.ThreadRunner(db, tid, llm=llm, config=RunnerConfig())

    asyncio.run(_run_until_idle(runner))

    assert llm.calls == 1
    notices = _notices(db, tid)
    assert not [notice for notice in notices if notice.get("action") in {"scheduled", "applied"}]
    stopped = [notice for notice in notices if notice.get("action") == "stopped"]
    assert len(stopped) == 1
    assert stopped[0]["decision_category"] == category
    assert "source_detail" not in stopped[0]
    assert len(stopped[0]["source_summary"]) <= 240
    source_event_seq, source_msg_id, source = _runner_error_record(db, tid)
    assert source["content"] == f"LLM/runner error: {error}"
    assert stopped[0]["source_msg_id"] == source_msg_id
    assert stopped[0]["source_event_seq"] == source_event_seq
    assert len(stopped[0]["content"]) < 1_000
    if len(f"LLM/runner error: {error}") > 240:
        assert f"LLM/runner error: {error}" not in stopped[0]["content"]
