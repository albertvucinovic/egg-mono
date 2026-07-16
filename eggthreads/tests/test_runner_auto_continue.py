from __future__ import annotations

import asyncio
import json
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
    assert any("Previous error: LLM/runner error: HTTP 503 Service Unavailable" in notice.get("content", "") for notice in notices)
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
    assert any("Previous error: LLM/runner error: HTTP 400 Bad Request" in notice.get("content", "") for notice in notices)
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
    for notice in (scheduled[0], applied[0]):
        assert notice["decision_category"] == category
        assert notice["decision_reason"]
        assert notice["source_detail"] == f"LLM/runner error: {error}"
        if category == "server_error":
            assert "Responses API error (server_error)" in notice["source_detail"]
            assert "553755af-e48c-412a-b824-beee36624640" in notice["source_detail"]
        assert notice["trigger_msg_id"] == user_msg_id
        assert notice["delay_sec"] == expected_delay
        assert error in notice["content"]


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
    assert error in stopped[0]["content"]
