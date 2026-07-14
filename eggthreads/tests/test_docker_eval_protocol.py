from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import eggthreads.session as session


def _run_host(tmp_path: Path, monkeypatch, *, cancel_check=None, timeout=5.0):
    bridge = tmp_path / "bridge"; bridge.mkdir(exist_ok=True)
    seen: dict = {}

    def write(path, payload):
        seen.update(payload)
        path.write_text(json.dumps(payload))

    monkeypatch.setattr(session, "_atomic_write_json", write)
    monkeypatch.setattr(session, "_service_tool_requests", lambda *_a, **_k: None)
    monkeypatch.setattr(session, "_docker_daemon_generation", lambda _p: "generation-a")
    return bridge, seen


def test_host_eval_envelope_and_cancel_ack(monkeypatch, tmp_path):
    bridge, seen = _run_host(tmp_path, monkeypatch)
    cancelled = threading.Event()
    result: list[str] = []

    def host():
        result.append(session._run_docker_eval_request(
            object(), "runtime", bridge,
            {"language": "python", "code": "while True: pass", "repl_name": "chan"},
            60, cancelled.is_set,
        ))

    thread = threading.Thread(target=host); thread.start()
    while not list(bridge.glob("eval_*.req.json")): time.sleep(0.005)
    req = list(bridge.glob("eval_*.req.json"))[0]
    while not req.read_text(): time.sleep(0.005)
    payload = json.loads(req.read_text())
    req_id = payload["request_id"]
    assert payload["protocol_version"] == 2
    assert payload["channel"] == "chan"
    assert payload["host_owner_id"] == session._DOCKER_HOST_OWNER_ID
    assert payload["daemon_generation"] == "generation-a"
    assert payload["deadline_duration_sec"] == 60
    cancelled.set()
    while not (bridge / f"eval_{req_id}.cancel.json").exists(): time.sleep(0.005)
    (bridge / f"eval_{req_id}.cancel.ack.json").write_text(json.dumps({"request_id": req_id, "state": "accepted"}))
    (bridge / f"eval_{req_id}.res.json").write_text(json.dumps({"request_id": req_id, "ok": True, "output": "--- INTERRUPTED ---\nreset"}))
    thread.join(2)
    assert result == ["--- INTERRUPTED ---\nreset"]
    assert not list(bridge.glob(f"eval_{req_id}.*"))


def test_terminal_result_wins_host_cancel_race_without_restart(monkeypatch, tmp_path):
    bridge, _seen = _run_host(tmp_path, monkeypatch)
    monkeypatch.setattr(session, "_DOCKER_CANCEL_ACK_SEC", 0.02)
    stops: list[str] = []
    monkeypatch.setattr(session, "stop_thread_session", lambda _db, _thread, reason: stops.append(reason))
    original_write = session._atomic_write_json

    def write_with_racing_result(path, payload):
        original_write(path, payload)
        if path.name.endswith(".cancel.json"):
            req_id = str(payload["request_id"])
            (bridge / f"eval_{req_id}.res.json").write_text(json.dumps({
                "protocol_version": 2,
                "request_id": req_id,
                "ok": True,
                "reason": "success",
                "output": "result won",
            }))

    monkeypatch.setattr(session, "_atomic_write_json", write_with_racing_result)

    result = session._run_docker_eval_request(
        object(), "runtime", bridge,
        {"language": "python", "code": "40 + 2", "repl_name": "chan"},
        60, lambda: True,
    )

    assert result == "result won"
    assert stops == []
    assert not list(bridge.glob("eval_*"))


def test_unresponsive_cancel_restarts_session(monkeypatch, tmp_path):
    bridge, _seen = _run_host(tmp_path, monkeypatch)
    monkeypatch.setattr(session, "_DOCKER_CANCEL_ACK_SEC", 0.02)
    stops: list[str] = []
    monkeypatch.setattr(session, "stop_thread_session", lambda _db, _thread, reason: stops.append(reason))
    result = session._run_docker_eval_request(
        object(), "runtime", bridge,
        {"language": "bash", "script": "sleep 99", "repl_name": "shell"},
        0.01, None,
    )
    assert "TIMEOUT" in result
    assert stops == ["docker_eval_cancel_unresponsive:timeout"]
    assert not list(bridge.glob("eval_*"))


def test_tool_bridge_owner_scoping_leaves_foreign_request(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    foreign = bridge / "tool_foreign.req.json"
    foreign.write_text(json.dumps({
        "host_owner_id": "other-host", "eval_request_id": "other-eval",
        "token": "token", "name": "skill", "arguments": {},
    }))
    monkeypatch.setattr("eggthreads.repl_bridge.call_tool", lambda *_a, **_k: "stolen")
    session._service_tool_requests(bridge, host_owner_id="this-host", eval_request_id="this-eval")
    assert foreign.exists()
    assert not (bridge / "tool_foreign.res.json").exists()


def test_version_two_tool_request_requires_complete_matching_owner(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge"; bridge.mkdir()
    request = bridge / "tool_unowned.req.json"
    request.write_text(json.dumps({
        "protocol_version": 2,
        "host_owner_id": "",
        "eval_request_id": "",
        "token": "token",
        "name": "skill",
        "arguments": {},
    }))
    monkeypatch.setattr("eggthreads.repl_bridge.call_tool", lambda *_a, **_k: "stolen")

    session._service_tool_requests(bridge, host_owner_id="this-host", eval_request_id="this-eval")

    assert request.exists()
    assert not (bridge / "tool_unowned.res.json").exists()


def test_repl_tool_context_cancel_signal_reaches_public_session_api(monkeypatch):
    from eggthreads.builtin_plugins import session as plugin
    from eggthreads.tools import ToolContext

    cancelled = threading.Event()
    seen: dict = {}
    monkeypatch.setattr(plugin, "_context_db", lambda _ctx: object())
    monkeypatch.setattr("eggthreads.session.execute_python_repl", lambda *_a, **kwargs: seen.update(kwargs) or "ok")
    ctx = ToolContext(thread_id="thread", cancel_check=cancelled.is_set, timeout_sec=9)
    assert plugin.execute_python_repl_tool({"code": "pass"}, ctx) == "ok"
    assert seen["cancel_check"] is cancelled.is_set or seen["cancel_check"]() is False
    assert seen["timeout_sec"] == 9
