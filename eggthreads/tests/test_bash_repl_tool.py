from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_execute_bash_repl_memory_provider_persists_environment(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out1 = ts.execute_bash_repl(db, parent, "export FOO=bar")
    assert "ERROR" not in out1

    out2 = ts.execute_bash_repl(db, parent, "echo $FOO")
    assert "bar" in out2

    runtime = ts.find_runtime_thread(db, parent, language="bash")
    assert runtime is not None
    assert runtime.runtime_thread_id in ts.list_children_ids(db, parent)


def test_bash_repl_records_completed_canonical_request_on_runtime(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out = ts.execute_bash_repl(
        db,
        parent,
        "printf once",
        caller_tool_call_id="caller-bash-repl",
    )

    runtime = ts.find_runtime_thread(db, parent, language="bash")
    assert runtime is not None
    assert ts.list_children_ids(db, parent) == [runtime.runtime_thread_id]
    states = ts.build_tool_call_states(db, runtime.runtime_thread_id)
    assert len(states) == 1
    state = next(iter(states.values()))
    assert state.state == "TC6"
    assert state.name == "bash_repl"
    assert ts.discover_runner_actionable(db, runtime.runtime_thread_id) is None

    payloads = [
        json.loads(row[0])
        for row in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq",
            (runtime.runtime_thread_id,),
        )
    ]
    request = next(payload for payload in payloads if payload["role"] == "user")
    result = next(payload for payload in payloads if payload["role"] == "tool")
    assert "```bash\nprintf once\n```" in request["content"]
    assert request["caller_tool_call_id"] == "caller-bash-repl"
    assert request["no_api"] is True
    assert result["tool_call_id"] == state.tool_call_id
    assert result["content"] == out
    assert result["no_api"] is True
    assert out.count("once") == 1


def test_execute_bash_repl_auto_creates_session_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_SESSION_PROVIDER", "memory")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_bash_repl(db, parent, "echo hi")
    assert "hi" in out
    runtime = ts.find_runtime_thread(db, parent, language="bash")
    assert runtime is not None
    cfg = ts.get_thread_session_config(db, runtime.runtime_thread_id)
    assert cfg.enabled is True
    assert cfg.provider == "memory"


def test_execute_bash_repl_reports_disabled_auto_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EGG_RLM_AUTO_SESSION", "0")
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    out = ts.execute_bash_repl(db, parent, "echo hi")
    assert "auto-create is disabled" in out


def test_bash_repl_tool_registered():
    tools = ts.create_default_tools()
    specs = {spec["function"]["name"]: spec for spec in tools.tools_spec()}
    assert "bash_repl" in specs
    props = specs["bash_repl"]["function"]["parameters"]["properties"]
    assert "timeout" in props
    assert "timeout_sec" not in props
    assert "drive_runtime_tools" not in props


def test_bash_repl_memory_can_use_eval_token_env(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.enable_thread_session(db, parent, provider="memory")

    out = ts.execute_bash_repl(db, parent, "test -n \"$EGG_EVAL_TOKEN\" && echo token-present")
    assert "token-present" in out
