from __future__ import annotations

import asyncio
import json

import pytest

import eggthreads as ts
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.output_optimizer import (
    DEFAULT_OUTPUT_OPTIMIZER_MODE,
    OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE,
    get_thread_output_optimizer_config,
    get_thread_output_optimizer_policy_config,
    output_optimizer_enabled,
    set_thread_output_optimizer_enabled,
    set_thread_output_optimizer_mode,
)
from eggthreads.tools import ToolRegistry


def _make_db(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


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


def test_output_optimizer_env_gate_mapping_behavior_is_preserved(monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)

    assert output_optimizer_enabled() is True
    assert output_optimizer_enabled({}, environ={}) is True
    assert output_optimizer_enabled(environ={"EGG_OUTPUT_OPTIMIZER": "off"}) is False
    assert output_optimizer_enabled(environ={"EGG_OUTPUT_OPTIMIZER": "yes"}) is True
    assert output_optimizer_enabled({"output_optimizer_enabled": False}, environ={"EGG_OUTPUT_OPTIMIZER": "yes"}) is False
    assert output_optimizer_enabled({"native_output_optimizer_enabled": "on"}, environ={}) is True
    assert output_optimizer_enabled({"egg_output_optimizer": "off"}, environ={"EGG_OUTPUT_OPTIMIZER": "on"}) is False


def test_output_optimizer_config_inherits_from_ancestors_and_overrides_by_field(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    no_event = get_thread_output_optimizer_config(db, grandchild)
    assert no_event.has_explicit_config is False
    assert no_event.enabled is None
    assert no_event.effective_enabled(environ={}) is True
    assert no_event.effective_enabled(environ={"EGG_OUTPUT_OPTIMIZER": "off"}) is False
    assert no_event.mode == DEFAULT_OUTPUT_OPTIMIZER_MODE
    assert get_thread_output_optimizer_policy_config(db, grandchild) == {}

    set_thread_output_optimizer_enabled(db, root, True, reason="test-root-on")
    set_thread_output_optimizer_mode(db, root, "conservative", reason="test-root-mode")

    inherited = get_thread_output_optimizer_config(db, grandchild)
    assert inherited.has_explicit_config is True
    assert inherited.enabled is True
    assert inherited.effective_enabled(environ={}) is True
    assert inherited.mode == "conservative"
    assert inherited.enabled_source == f"event:{root}"
    assert inherited.mode_source == f"event:{root}"
    assert inherited.to_policy_config()["output_optimizer_enabled"] is True
    assert inherited.to_policy_config()["output_optimizer_mode_min_confidence"] == 0.9

    set_thread_output_optimizer_enabled(db, child, False, reason="test-child-off")

    child_override = get_thread_output_optimizer_config(db, grandchild)
    assert child_override.enabled is False
    assert child_override.effective_enabled(environ={"EGG_OUTPUT_OPTIMIZER": "on"}) is False
    assert child_override.mode == "conservative"
    assert child_override.enabled_source == f"event:{child}"
    assert child_override.mode_source == f"event:{root}"

    set_thread_output_optimizer_mode(db, grandchild, "aggressive", reason="test-grandchild-mode")

    local_mode = get_thread_output_optimizer_config(db, grandchild)
    assert local_mode.enabled is False
    assert local_mode.mode == "aggressive"
    assert local_mode.mode_source == f"event:{grandchild}"
    assert local_mode.to_policy_config()["output_optimizer_mode_min_confidence"] == 0.0

    rows = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
        (root, OUTPUT_OPTIMIZER_CONFIG_EVENT_TYPE),
    ).fetchall()
    assert [json.loads(row[0])["reason"] for row in rows] == ["test-root-on", "test-root-mode"]


def test_output_optimizer_slash_commands_toggle_status_and_validate_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="commands")
    registry = create_default_command_registry()
    logs: list[str] = []
    printed: list[tuple[str, str]] = []
    ctx = CommandContext(
        db=db,
        current_thread=tid,
        log_system=logs.append,
        console_print_block=lambda title, text, **kwargs: printed.append((title, text)),
    )

    assert "outputOptimizerStatus" in registry.names()
    assert "outputOptimizerOn" in registry.names()
    assert "outputOptimizerOff" in registry.names()
    assert "outputOptimizerMode" in registry.names()
    assert registry.complete("outputOptimizerMode", ctx, "a") == ["aggressive"]

    status = registry.execute("outputOptimizerStatus", ctx)
    assert status.clear_input is True
    assert "Enabled: ENABLED" in status.message
    assert "Enabled source: default enabled" in status.message
    assert "Event config present: False" in status.message
    assert any(title == "Output Optimizer" and "Mode: balanced" in text for title, text in printed)

    registry.execute("outputOptimizerOn", ctx)
    cfg = get_thread_output_optimizer_config(db, tid)
    assert cfg.enabled is True
    assert any("ENABLED" in message for message in logs)

    registry.execute("outputOptimizerMode", ctx, "conservative")
    cfg = get_thread_output_optimizer_config(db, tid)
    assert cfg.mode == "conservative"

    invalid = registry.execute("outputOptimizerMode", ctx, "reckless")
    assert invalid.clear_input is False
    assert get_thread_output_optimizer_config(db, tid).mode == "conservative"
    assert any("Usage: /outputOptimizerMode" in message for message in logs)

    registry.execute("outputOptimizerOff", ctx)
    assert get_thread_output_optimizer_config(db, tid).enabled is False
    assert any("DISABLED" in message for message in logs)


def test_runner_output_policy_uses_resolved_thread_config_to_disable_env_enabled_optimizer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EGG_OUTPUT_OPTIMIZER", "on")
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="runner-disable")
    set_thread_output_optimizer_enabled(db, tid, False, reason="test-disable")
    tcid = ts.enqueue_user_tool_call(db, tid, "repeat", {}, auto_approve=True, hidden=False)
    line = "thread config disable repeated line " * 8
    raw_output = "\n".join([line] * 6)

    tools = ToolRegistry()
    tools.register("repeat", "Repeat", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] == raw_output
    assert "optimizer" not in approval["channels"]


def test_runner_output_policy_uses_resolved_thread_config_to_enable_without_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="runner-enable")
    set_thread_output_optimizer_enabled(db, tid, True, reason="test-enable")
    tcid = ts.enqueue_user_tool_call(db, tid, "repeat", {}, auto_approve=True, hidden=False)
    line = "thread config enable repeated line " * 8
    raw_output = "\n".join([line] * 6)

    tools = ToolRegistry()
    tools.register("repeat", "Repeat", {"type": "object", "properties": {}}, lambda args: raw_output)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True

    approval = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert approval["decision"] == "whole"
    assert approval["preview"] != raw_output
    assert "[... repeated 5 more times ...]" in approval["preview"]
    assert approval["channels"]["optimizer"]["optimized"] is True
    assert approval["channels"]["optimizer"]["fallback"] is False


def test_output_optimizer_mode_controls_min_confidence_in_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EGG_OUTPUT_OPTIMIZER", raising=False)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="runner-mode")
    set_thread_output_optimizer_enabled(db, tid, True, reason="test-enable")
    set_thread_output_optimizer_mode(db, tid, "conservative", reason="test-conservative")
    line = "conservative mode generic repeated line " * 8
    raw_output = "\n".join([line] * 6)

    tools = ToolRegistry()
    tools.register("repeat", "Repeat", {"type": "object", "properties": {}}, lambda args: raw_output)
    tcid = ts.enqueue_user_tool_call(db, tid, "repeat", {}, auto_approve=True, hidden=False)

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=tools)
    assert asyncio.run(runner.run_once()) is True
    conservative = _latest_payload(db, tid, "tool_call.output_approval", tcid)
    assert conservative["preview"] == raw_output
    optimizer = conservative["channels"]["optimizer"]
    assert optimizer["optimized"] is False
    assert optimizer["fallback"] is True
    assert any(
        item.get("filter_name") == "generic" and item.get("reason") == "low_confidence"
        for item in optimizer["metadata"]["rejected_filters"]
    )

    # Publish the first tool message so the next run executes the new tool call
    # instead of completing TC5 for the earlier call.
    assert asyncio.run(runner.run_once()) is True

    set_thread_output_optimizer_mode(db, tid, "aggressive", reason="test-aggressive")
    tcid2 = ts.enqueue_user_tool_call(db, tid, "repeat", {}, auto_approve=True, hidden=False)
    assert asyncio.run(runner.run_once()) is True
    aggressive = _latest_payload(db, tid, "tool_call.output_approval", tcid2)
    assert aggressive["preview"] != raw_output
    assert "[... repeated 5 more times ...]" in aggressive["preview"]
    assert aggressive["channels"]["optimizer"]["optimized"] is True


def test_invalid_output_optimizer_mode_api_rejected(tmp_path) -> None:
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="invalid")

    with pytest.raises(ValueError):
        set_thread_output_optimizer_mode(db, tid, "reckless")
