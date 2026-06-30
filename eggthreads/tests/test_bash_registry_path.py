from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.runner import RunnerConfig


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_runner_executes_bash_through_tool_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "echo registry-bash"},
        auto_approve=True,
        hidden=True,
    )

    calls = []
    original = ts.ToolRegistry.execute_async

    async def wrapped(self, name, arguments, **context):
        calls.append((name, context))
        return await original(self, name, arguments, **context)

    monkeypatch.setattr(ts.ToolRegistry, "execute_async", wrapped)

    async def run() -> None:
        runner = ts.ThreadRunner(db, tid, llm=object(), config=RunnerConfig())
        assert await runner.run_once() is True
        assert await runner.run_once() is True

    asyncio.run(run())

    assert calls
    assert calls[0][0] == "bash"
    assert calls[0][1]["stream"].tool_name == "bash"

    states = ts.build_tool_call_states(db, tid)
    assert states[tcid].finished_reason == "success"
    assert "registry-bash" in (states[tcid].finished_output or "")


def test_runner_persists_bash_timeout_reason_from_tool_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    tcid = ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "sleep 10", "timeout_sec": 1},
        auto_approve=True,
        hidden=True,
    )

    async def run() -> None:
        runner = ts.ThreadRunner(db, tid, llm=object(), config=RunnerConfig())
        assert await runner.run_once() is True

    asyncio.run(run())

    states = ts.build_tool_call_states(db, tid)
    assert states[tcid].finished_reason == "timeout"
    assert "TIMEOUT" in (states[tcid].finished_output or "")


def test_bash_tool_streams_live_output_through_tool_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EGG_SANDBOX_MODE", "off")
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.enqueue_user_tool_call(
        db,
        tid,
        "bash",
        {"script": "echo stream-through-context"},
        auto_approve=True,
        hidden=True,
    )

    async def run() -> None:
        runner = ts.ThreadRunner(db, tid, llm=object(), config=RunnerConfig())
        assert await runner.run_once() is True

    asyncio.run(run())

    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='stream.delta' ORDER BY event_seq",
        (tid,),
    ).fetchall()
    deltas = [json.loads(row[0]) for row in rows]
    streamed = "".join(str(delta.get("tool", {}).get("text") or "") for delta in deltas)

    assert "--- STDOUT ---" in streamed
    assert "stream-through-context" in streamed
    assert streamed.count("stream-through-context") == 1
