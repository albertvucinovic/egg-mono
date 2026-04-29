from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


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
        bridge_timeout_sec=5,
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
        bridge_timeout_sec=5,
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
        bridge_timeout_sec=5,
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
