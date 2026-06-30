from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.runner import RunnerConfig, SubtreeScheduler


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_repl_bridge_call_tool_enqueues_ra3_and_direct_drives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")

    # Runtime must allow bash for this programmatic call.
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["bash"])

    ctx = ts.create_eval_context(
        db,
        caller_thread_id=parent,
        runtime_thread_id=runtime,
        session_id=ts.get_thread_session_config(db, runtime).session_id,
        drive_runtime_tools=True,
        timeout_sec=5,
    )
    try:
        out = ts.repl_bridge_call_tool(ctx.token, "bash", {"script": "echo bridge-ok"})
    finally:
        ts.dispose_eval_context(ctx.token)

    assert "bridge-ok" in out
    states = ts.build_tool_call_states(db, runtime)
    assert len(states) == 1
    tc = next(iter(states.values()))
    assert tc.name == "bash"
    assert tc.state == "TC6"

    # The runtime transcript stores a hidden RA3 user message.
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' AND msg_id=?",
        (runtime, tc.parent_msg_id),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["origin"] == "repl"
    assert payload["no_api"] is True
    assert payload["tool_calls"][0]["function"]["arguments"]
    call_args = json.loads(payload["tool_calls"][0]["function"]["arguments"])
    assert call_args["timeout"] == 5.0
    assert "timeout_sec" not in call_args


def test_python_repl_eggtools_bash_uses_runtime_thread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["bash"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import bash\nprint(bash('echo from-eggtools'))",
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    assert "from-eggtools" in out
    runtime2 = ts.find_runtime_thread(db, parent, language="python")
    assert runtime2 is not None
    assert runtime2.runtime_thread_id == runtime

    states = ts.build_tool_call_states(db, runtime)
    assert any(tc.name == "bash" and tc.state == "TC6" for tc in states.values())


def test_repl_bridge_denies_non_allowlisted_tool(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tool_allowlist(db, runtime, ["web_search"])

    ctx = ts.create_eval_context(
        db,
        caller_thread_id=parent,
        runtime_thread_id=runtime,
        session_id="sess_test",
        drive_runtime_tools=True,
    )
    try:
        try:
            ts.repl_bridge_call_tool(ctx.token, "bash", {"script": "echo nope"})
        except ts.ReplBridgeError as e:
            assert "not allowed" in str(e)
        else:
            raise AssertionError("Expected ReplBridgeError")
    finally:
        ts.dispose_eval_context(ctx.token)


def test_repl_bridge_rejects_reserved_context_arguments(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tool_allowlist(db, runtime, ["bash", "spawn_agent"])

    ctx = ts.create_eval_context(
        db,
        caller_thread_id=parent,
        runtime_thread_id=runtime,
        session_id="sess_test",
        drive_runtime_tools=True,
    )
    try:
        for name, args in (
            ("bash", {"script": "echo nope", "_thread_id": other}),
            ("spawn_agent", {"context_text": "nope", "parent_thread_id": other}),
        ):
            try:
                ts.repl_bridge_call_tool(ctx.token, name, args)
            except ts.ReplBridgeError as e:
                assert "reserved tool context" in str(e)
            else:
                raise AssertionError("Expected ReplBridgeError")
    finally:
        ts.dispose_eval_context(ctx.token)


def test_python_repl_spawn_agent_creates_child_under_runtime_and_attenuates_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["spawn_agent", "wait", "web_search"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import spawn_agent\nprint(spawn_agent('child task', label='from-repl', allowed_tools=['web_search', 'bash']))",
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    # Extract the spawned thread id from the printed output.
    lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith('---')]
    child = lines[-1]
    assert child in ts.list_children_ids(db, runtime)
    assert child not in ts.list_children_ids(db, parent)

    child_cfg = ts.get_thread_tools_config(db, child)
    assert child_cfg.allowed_tools == {"web_search"}
    assert child_cfg.is_tool_allowed("web_search")
    assert not child_cfg.is_tool_allowed("bash")


def test_runtime_spawned_child_inherits_runtime_tool_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, False)
    ts.set_thread_tool_allowlist(db, runtime, ["spawn_agent", "bash"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import spawn_agent\n"
        "print(spawn_agent('child task', label='from-repl', allowed_tools=['bash']))",
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    child = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith('---')][-1]
    cfg = ts.get_thread_tools_config(db, child)
    assert cfg.llm_tools_enabled is False
    assert cfg.allowed_tools == {"bash"}


def test_python_repl_wait_observes_child_result_under_scheduler(tmp_path, monkeypatch):
    """Regression: eggtools.wait() should work from /pythonRepl scheduler flow."""

    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["spawn_agent", "wait"])

    class MockLLM:
        current_model_key = "mock"

        def set_model(self, model_key):
            self.current_model_key = model_key

        def set_model_with_config(self, model_key, config):
            self.current_model_key = model_key

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            yield {"type": "content_delta", "text": "child done"}
            yield {"type": "done", "message": {"role": "assistant", "content": "child done"}}

    async def run() -> str:
        code = (
            "from eggtools import spawn_agent, wait\n"
            "child = spawn_agent('Say exactly child done', label='kid')\n"
            "print(wait([child], timeout_sec=10))\n"
        )
        tcid = ts.enqueue_user_tool_call(
            db,
            parent,
            "python_repl",
            {"code": code, "timeout_sec": 10},
            hidden=True,
            keep_user_turn=True,
            origin="ui_python_repl",
            auto_approve=True,
        )
        scheduler = SubtreeScheduler(
            db,
            parent,
            llm=MockLLM(),
            config=RunnerConfig(max_concurrent_threads=4, api_timeout_sec=5, lease_ttl_sec=5),
            owner="test",
        )
        task = asyncio.create_task(scheduler.run_forever(poll_sec=0.01))
        try:
            result = await ts.wait_for_tool_call_result_async(db, parent, tcid, timeout_sec=15, poll_interval=0.05)
            assert result.state == "TC6"
            assert not result.timed_out
            return result.content or ""
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    out = asyncio.run(run())
    assert "Thread " in out
    assert "Last assistant message:\nchild done" in out
    assert "(no assistant content found)" not in out


def test_wait_accepts_message_event_as_completed_assistant_turn(tmp_path, monkeypatch):
    """Regression: wait should not block when adapters finish with message events."""

    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "work")
    ts.create_snapshot(db, child)

    class MessageEventLLM:
        current_model_key = "mock"

        def set_model(self, model_key):
            self.current_model_key = model_key

        def set_model_with_config(self, model_key, config):
            self.current_model_key = model_key

        async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
            yield {"type": "content_delta", "text": "child via message"}
            yield {"type": "message", "role": "assistant", "content": "child via message", "stop_reason": "end_turn"}

    async def run() -> str:
        scheduler = SubtreeScheduler(
            db,
            parent,
            llm=MessageEventLLM(),
            config=RunnerConfig(max_concurrent_threads=2, api_timeout_sec=5, lease_ttl_sec=5),
            owner="test",
        )
        task = asyncio.create_task(scheduler.run_forever(poll_sec=0.01))
        try:
            result = await asyncio.to_thread(ts.create_default_tools().execute, "wait", {"thread_ids": [child], "timeout_sec": 2}, thread_id=parent)
            return result
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    out = asyncio.run(run())
    assert "Thread " in out
    assert "finished" in out
    assert "child via message" in out


def test_python_repl_can_send_message_to_child(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    ts.append_message(db, parent, "system", "system")
    ts.enable_thread_session(db, parent, provider="memory")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")
    ts.set_thread_tools_enabled(db, runtime, True)
    ts.set_thread_tool_allowlist(db, runtime, ["spawn_agent", "send_message_to_child"])

    out = ts.execute_python_repl(
        db,
        parent,
        "from eggtools import spawn_agent, send_message_to_child\n"
        "child = spawn_agent('initial task', label='worker', allowed_tools=[])\n"
        "print(send_message_to_child(child, 'please refine', require_idle=False))",
        drive_runtime_tools=True,
        timeout_sec=5,
    )

    assert "Sent message" in out
    children = ts.list_children_ids(db, runtime)
    assert len(children) == 1
    worker = children[0]
    messages = json.loads(db.get_thread(worker).snapshot_json)["messages"]
    assert messages[-1]["content"] == "please refine"
    assert messages[-1]["from_thread_id"] == runtime
