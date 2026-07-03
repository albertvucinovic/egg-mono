from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import eggthreads as ts
from eggthreads.output_optimizer import (
    OptimizeRequest,
    OutputOptimizer,
    RtkPipeFilter,
    create_default_output_optimizer,
    output_optimizer_rtk_command,
    output_optimizer_rtk_enabled,
    output_optimizer_rtk_privacy_opt_in,
    output_optimizer_rtk_timeout_seconds,
)
from eggthreads.tools import ToolRegistry


def _make_fake_rtk(tmp_path: Path) -> Path:
    script = tmp_path / "fake-rtk.py"
    script.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations
import os
from pathlib import Path
import sys
import time

log_path = os.environ.get("FAKE_RTK_ENV_LOG")
if log_path:
    Path(log_path).write_text(
        "telemetry=" + os.environ.get("RTK_TELEMETRY_DISABLED", "") + "\\n"
        "home=" + os.environ.get("HOME", "") + "\\n"
        "rtk_home=" + os.environ.get("RTK_HOME", "") + "\\n"
        "xdg_config=" + os.environ.get("XDG_CONFIG_HOME", "") + "\\n"
        "xdg_state=" + os.environ.get("XDG_STATE_HOME", "") + "\\n"
        "xdg_cache=" + os.environ.get("XDG_CACHE_HOME", "") + "\\n"
    )

if sys.argv[1:] != ["pipe"]:
    print("expected pipe subcommand", file=sys.stderr)
    raise SystemExit(9)

mode = os.environ.get("FAKE_RTK_MODE", "success")
if mode == "sleep":
    time.sleep(5)
if mode == "fail":
    print("fake rtk failure", file=sys.stderr)
    raise SystemExit(2)

data = sys.stdin.read()
if mode == "expand":
    sys.stdout.write(data + data)
else:
    first = data.splitlines()[0] if data.splitlines() else ""
    sys.stdout.write("RTK summary:\\n" + first + "\\n")
"""
    )
    script.chmod(0o755)
    return script


def _fake_rtk_command(path: Path) -> str:
    return f"{sys.executable} {path}"


def _raw_output() -> str:
    return "\n".join(
        [
            "important first line that should survive",
            "unique noisy detail alpha with enough characters to make the fake summary smaller",
            "unique noisy detail beta with enough characters to make the fake summary smaller",
            "unique noisy detail gamma with enough characters to make the fake summary smaller",
        ]
    )


def _latest_payload(db, thread_id: str, event_type: str, tool_call_id: str | None = None) -> dict:
    if tool_call_id is None:
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq DESC LIMIT 1",
            (thread_id, event_type),
        ).fetchone()
    else:
        row = db.conn.execute(
            """
            SELECT payload_json FROM events
             WHERE thread_id=? AND type=? AND json_extract(payload_json, '$.tool_call_id')=?
             ORDER BY event_seq DESC LIMIT 1
            """,
            (thread_id, event_type, tool_call_id),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_rtk_config_helpers_are_disabled_by_default_and_mapping_precedes_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER_RTK", raising=False)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER_RTK_BIN", raising=False)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER_RTK_TIMEOUT", raising=False)
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER_RTK_PRIVACY_OPT_IN", raising=False)

    assert output_optimizer_rtk_enabled(environ={}) is False
    assert output_optimizer_rtk_enabled({"output_optimizer_rtk_enabled": False}, environ={"EGG_OUTPUT_OPTIMIZER_RTK": "1"}) is False
    assert output_optimizer_rtk_enabled({"native_output_optimizer_rtk_enabled": "yes"}, environ={}) is True

    fake = tmp_path / "rtk"
    assert output_optimizer_rtk_command(environ={}) == "rtk"
    assert output_optimizer_rtk_command({"output_optimizer_rtk_command": str(fake)}, environ={}) == str(fake)
    assert output_optimizer_rtk_command({}, environ={"EGG_OUTPUT_OPTIMIZER_RTK_BIN": str(fake)}) == str(fake)

    assert output_optimizer_rtk_timeout_seconds(environ={}) == 3.0
    assert output_optimizer_rtk_timeout_seconds({"output_optimizer_rtk_timeout_seconds": "0.25"}, environ={}) == 0.25
    assert output_optimizer_rtk_timeout_seconds({}, environ={"EGG_OUTPUT_OPTIMIZER_RTK_TIMEOUT": "0.5"}) == 0.5

    assert output_optimizer_rtk_privacy_opt_in(environ={}) is False
    assert output_optimizer_rtk_privacy_opt_in({"output_optimizer_rtk_privacy_opt_in": "on"}, environ={}) is True


def test_default_factory_excludes_rtk_until_explicitly_included(tmp_path) -> None:
    fake = _make_fake_rtk(tmp_path)

    assert "rtk_pipe" not in create_default_output_optimizer().names()

    optimizer = create_default_output_optimizer(include_rtk=True, rtk_command=_fake_rtk_command(fake), rtk_timeout_seconds=1.0)
    names = optimizer.names()
    assert "rtk_pipe" in names
    assert names.index("rtk_pipe") < names.index("generic")

    decision = optimizer.optimize(OptimizeRequest(tool_name="bash", tool_args={"script": "printf output"}, output=_raw_output()))
    assert decision.optimized is True
    assert decision.filter_name == "rtk_pipe"
    assert decision.output.startswith("RTK summary:")


def test_rtk_filter_success_sets_privacy_safe_env_defaults(tmp_path, monkeypatch) -> None:
    fake = _make_fake_rtk(tmp_path)
    env_log = tmp_path / "env.log"
    monkeypatch.setenv("FAKE_RTK_ENV_LOG", str(env_log))
    monkeypatch.setenv("RTK_TELEMETRY_DISABLED", "0")

    decision = OutputOptimizer([RtkPipeFilter(command=_fake_rtk_command(fake), timeout_seconds=1.0)]).optimize(
        OptimizeRequest(output=_raw_output())
    )

    assert decision.optimized is True
    assert decision.filter_name == "rtk_pipe"
    assert decision.metadata["rtk_telemetry_disabled"] is True
    assert decision.metadata["rtk_privacy_opt_in"] is False
    text = env_log.read_text()
    assert "telemetry=1" in text
    assert "home=" in text and "home" in text
    assert "rtk_home=" in text and "rtk-home" in text
    assert "xdg_config=" in text and "config" in text
    assert "xdg_state=" in text and "state" in text
    assert "xdg_cache=" in text and "cache" in text


def test_rtk_filter_privacy_opt_in_does_not_isolate_or_override_tracking(tmp_path, monkeypatch) -> None:
    fake = _make_fake_rtk(tmp_path)
    env_log = tmp_path / "env.log"
    monkeypatch.setenv("FAKE_RTK_ENV_LOG", str(env_log))
    monkeypatch.setenv("RTK_TELEMETRY_DISABLED", "0")

    decision = OutputOptimizer([RtkPipeFilter(command=_fake_rtk_command(fake), timeout_seconds=1.0, privacy_opt_in=True)]).optimize(
        OptimizeRequest(output=_raw_output())
    )

    assert decision.optimized is True
    assert decision.metadata["rtk_privacy_opt_in"] is True
    text = env_log.read_text()
    env_lines = dict(line.split("=", 1) for line in text.splitlines())
    assert "telemetry=0" in text
    assert env_lines.get("home")  # inherited process HOME remains available when explicitly opted in.
    assert env_lines.get("rtk_home") == ""


def test_rtk_filter_fallbacks_for_missing_failure_timeout_and_expansion(tmp_path, monkeypatch) -> None:
    raw = _raw_output()
    missing = OutputOptimizer([RtkPipeFilter(command=str(tmp_path / "missing-rtk"), timeout_seconds=0.2)]).optimize(
        OptimizeRequest(output=raw)
    )
    assert missing.optimized is False
    assert missing.output == raw

    fake = _make_fake_rtk(tmp_path)
    monkeypatch.setenv("FAKE_RTK_MODE", "fail")
    failing = OutputOptimizer([RtkPipeFilter(command=_fake_rtk_command(fake), timeout_seconds=0.5)]).optimize(OptimizeRequest(output=raw))
    assert failing.optimized is False
    assert failing.output == raw

    monkeypatch.setenv("FAKE_RTK_MODE", "sleep")
    timed_out = OutputOptimizer([RtkPipeFilter(command=_fake_rtk_command(fake), timeout_seconds=0.1)]).optimize(OptimizeRequest(output=raw))
    assert timed_out.optimized is False
    assert timed_out.output == raw

    monkeypatch.setenv("FAKE_RTK_MODE", "expand")
    expanded = OutputOptimizer([RtkPipeFilter(command=_fake_rtk_command(fake), timeout_seconds=0.5)]).optimize(OptimizeRequest(output=raw))
    assert expanded.optimized is False
    assert expanded.output == raw
    assert expanded.metadata["rejected_filters"][0]["reason"] == "not_smaller"


def test_output_policy_uses_rtk_only_when_explicitly_enabled_and_preserves_raw(tmp_path, monkeypatch) -> None:
    fake = _make_fake_rtk(tmp_path)
    raw_output = _raw_output()

    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "1")
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER_RTK_BIN", _fake_rtk_command(fake))
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER_RTK", raising=False)

    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="rtk-disabled")
    tcid = ts.enqueue_user_tool_call(db, tid, "bash", {"script": "printf output"}, auto_approve=True, hidden=False)
    tools = ToolRegistry()
    tools.register("bash", "Bash", {"type": "object", "properties": {}}, lambda args: raw_output)
    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)

    assert asyncio.run(runner.run_once()) is True
    disabled = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert disabled["preview"] == raw_output
    assert disabled["channels"]["optimizer"]["optimized"] is False
    rejected = disabled["channels"]["optimizer"]["metadata"]["rejected_filters"]
    assert all(item.get("filter_name") != "rtk_pipe" for item in rejected)

    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER_RTK", "1")
    db2 = ts.ThreadsDB(tmp_path / "threads2.sqlite")
    db2.init_schema()
    tid2 = ts.create_root_thread(db2, name="rtk-enabled")
    tcid2 = ts.enqueue_user_tool_call(db2, tid2, "bash", {"script": "printf output"}, auto_approve=True, hidden=False)
    runner2 = ts.ThreadRunner(db2, tid2, llm=object(), tools=tools)

    assert asyncio.run(runner2.run_once()) is True
    state = ts.build_tool_call_states(db2, tid2)[tcid2]
    assert state.finished_output == raw_output
    enabled = _latest_payload(db2, tid2, "tool_call.output_approval", tcid2)
    assert enabled["preview"].startswith("RTK summary:")
    assert enabled["channels"]["optimizer"]["filter_name"] == "rtk_pipe"
    assert enabled["channels"]["optimizer"]["optimized"] is True

    assert asyncio.run(runner2.run_once()) is True
    tool_msg = _latest_payload(db2, tid2, "msg.create", tcid2)
    assert tool_msg["role"] == "tool"
    assert "RTK summary:" in tool_msg["content"]
    assert tool_msg.get("output_optimizer", {}).get("summary", "").startswith("Egg optimized")
