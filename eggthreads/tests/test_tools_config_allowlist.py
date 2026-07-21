from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.tools import ToolRegistry


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "allowed_tool",
        "Allowed test tool",
        {"type": "object", "properties": {}},
        lambda args: "allowed output",
    )
    reg.register(
        "blocked_tool",
        "Blocked test tool",
        {"type": "object", "properties": {}},
        lambda args: "blocked output",
    )
    return reg


def test_tools_config_allowlist_parses_and_disables_override(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    ts.set_thread_tool_allowlist(db, tid, ["allowed_tool", "blocked_tool"])
    cfg = ts.get_thread_tools_config(db, tid)
    assert cfg.allowed_tools == {"allowed_tool", "blocked_tool"}
    assert cfg.is_tool_allowed("allowed_tool")
    assert cfg.is_tool_allowed("blocked_tool")
    assert not cfg.is_tool_allowed("other_tool")

    ts.disable_tool_for_thread(db, tid, "blocked_tool")
    cfg = ts.get_thread_tools_config(db, tid)
    assert cfg.is_tool_allowed("allowed_tool")
    assert not cfg.is_tool_allowed("blocked_tool")

    ts.clear_thread_tool_allowlist(db, tid)
    cfg = ts.get_thread_tools_config(db, tid)
    assert cfg.allowed_tools is None
    assert cfg.is_tool_allowed("allowed_tool")
    assert not cfg.is_tool_allowed("blocked_tool")


def test_historical_python_policy_name_maps_to_python_exec(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="legacy-python-policy")

    ts.set_thread_tool_allowlist(db, tid, ["python"])
    cfg = ts.get_thread_tools_config(db, tid)
    assert cfg.allowed_tools == {"python_exec"}
    assert cfg.is_tool_allowed("python_exec")

    ts.disable_tool_for_thread(db, tid, "python")
    cfg = ts.get_thread_tools_config(db, tid)
    assert cfg.disabled_tools == {"python_exec"}
    assert not cfg.is_tool_allowed("python_exec")

    ts.enable_tool_for_thread(db, tid, "python")
    assert ts.get_thread_tools_config(db, tid).is_tool_allowed("python_exec")

def test_tool_statuses_reflect_allowlist_and_disabled_tools(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    ts.set_thread_tool_allowlist(db, tid, ["allowed_tool", "disabled_tool"])
    ts.disable_tool_for_thread(db, tid, "disabled_tool")
    cfg = ts.get_thread_tools_config(db, tid)

    statuses = {
        item["name"]: item
        for item in ts.get_tool_statuses_for_config(
            cfg,
            {
                "allowed_tool": {"local_only": False},
                "disabled_tool": {"local_only": False},
                "other_tool": {"local_only": True},
            },
        )
    }

    assert statuses["allowed_tool"]["enabled"] is True
    assert statuses["allowed_tool"]["status"] == "enabled"
    assert statuses["disabled_tool"]["enabled"] is False
    assert statuses["disabled_tool"]["status"] == "disabled"
    assert statuses["other_tool"]["enabled"] is False
    assert statuses["other_tool"]["status"] == "not_allowed"
    assert statuses["other_tool"]["local_only"] is True


def test_create_child_thread_inherits_disabled_tools_by_value(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.disable_tool_for_thread(db, parent, "bash")

    child = ts.create_child_thread(db, parent, name="child")
    cfg = ts.get_thread_tools_config(db, child)
    assert not cfg.is_tool_allowed("bash")

    ts.enable_tool_for_thread(db, parent, "bash")
    assert ts.get_thread_tools_config(db, parent).is_tool_allowed("bash")
    assert not ts.get_thread_tools_config(db, child).is_tool_allowed("bash")


def test_create_child_thread_inherits_allowlist_and_disabled_distinctly(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.set_thread_tool_allowlist(db, parent, ["bash", "web_search"])
    ts.disable_tool_for_thread(db, parent, "bash")

    child = ts.create_child_thread(db, parent, name="child")
    cfg = ts.get_thread_tools_config(db, child)

    assert cfg.allowed_tools == {"bash", "web_search"}
    assert cfg.disabled_tools == {"bash"}
    assert not cfg.is_tool_allowed("bash")
    assert cfg.is_tool_allowed("web_search")
    assert not cfg.is_tool_allowed("python_exec")


def test_create_child_thread_inherits_tools_enabled_and_secret_mode(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.set_thread_tools_enabled(db, parent, False)
    ts.set_thread_allow_raw_tool_output(db, parent, False)

    child = ts.create_child_thread(db, parent, name="child")
    cfg = ts.get_thread_tools_config(db, child)

    assert cfg.llm_tools_enabled is False
    assert cfg.allow_raw_tool_output is False


def test_child_cannot_widen_beyond_parent_after_creation(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.set_thread_tool_allowlist(db, parent, ["web_search"])

    child = ts.create_child_thread(db, parent, name="child")
    assert not ts.get_thread_tools_config(db, child).is_tool_allowed("bash")

    ts.set_thread_tool_allowlist(db, child, ["web_search", "bash"])
    cfg = ts.get_thread_tools_config(db, child)
    assert cfg.is_tool_allowed("web_search")
    assert not cfg.is_tool_allowed("bash")


def test_later_parent_restriction_applies_to_existing_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    ts.set_thread_tool_allowlist(db, child, ["web_search", "bash"])
    assert ts.get_thread_tools_config(db, grandchild).is_tool_allowed("bash")

    ts.set_thread_tool_allowlist(db, parent, ["web_search"])
    cfg = ts.get_thread_tools_config(db, grandchild)
    assert cfg.is_tool_allowed("web_search")
    assert not cfg.is_tool_allowed("bash")


def test_parent_raw_output_denial_overrides_child_enable(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    ts.set_thread_allow_raw_tool_output(db, parent, True)
    child = ts.create_child_thread(db, parent, name="child")
    ts.set_thread_allow_raw_tool_output(db, child, True)
    assert ts.get_thread_tools_config(db, child).allow_raw_tool_output is True

    ts.set_thread_allow_raw_tool_output(db, parent, False)
    assert ts.get_thread_tools_config(db, child).allow_raw_tool_output is False


class _ToolCallingLLM:
    current_model_key = "test-model"

    def __init__(self):
        self.seen_tool_names: set[str] = set()

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None, **kwargs):
        self.seen_tool_names = {
            t.get("function", {}).get("name")
            for t in (tools or [])
        }
        yield {"type": "content_delta", "text": "done"}
        yield {"type": "message", "role": "assistant", "content": "done", "stop_reason": "end_turn"}


def test_ra1_exposes_only_allowlisted_tools(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", "hello")
    ts.create_snapshot(db, tid)
    ts.set_thread_tool_allowlist(db, tid, ["allowed_tool"])

    llm = _ToolCallingLLM()
    runner = ts.ThreadRunner(db, tid, llm=llm, tools=_registry())
    assert asyncio.run(runner.run_once()) is True

    assert llm.seen_tool_names == {"allowed_tool"}


def test_ra3_denies_non_allowlisted_tool_without_executing(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_tool_allowlist(db, tid, ["allowed_tool"])

    tc_id = ts.enqueue_user_tool_call(
        db,
        tid,
        "blocked_tool",
        {},
        content="blocked_tool()",
        hidden=True,
        auto_approve=True,
        approval_reason="test",
    )

    runner = ts.ThreadRunner(db, tid, llm=object(), tools=_registry())
    # First run marks the call finished+output-approved with synthetic denial.
    assert asyncio.run(runner.run_once()) is True
    # Second run publishes the tool message (TC5 -> TC6).
    assert asyncio.run(runner.run_once()) is True

    result = ts.get_user_command_result(db, tid, tc_id)
    assert result is not None
    assert "not allowed" in result
    assert "blocked output" not in result


def _insert_corrupt_tools_config(db, thread_id: str, payload_json: str) -> None:
    db.conn.execute(
        """
        INSERT INTO events(event_id, thread_id, type, payload_json)
        VALUES (?, ?, 'tools.config', ?)
        """,
        (f"corrupt-{thread_id}-{db.max_event_seq(thread_id) + 1}", thread_id, payload_json),
    )


def test_missing_policy_uses_safe_usable_defaults(tmp_path):
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    cfg = ts.get_thread_tools_config(db, thread_id)

    assert cfg.policy_error is None
    assert cfg.has_explicit_config is False
    assert cfg.llm_tools_enabled is True
    assert cfg.is_tool_allowed("allowed_tool")
    assert cfg.allow_raw_tool_output is False


def test_corrupt_policy_fails_closed_and_emits_diagnostic(tmp_path):
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    _insert_corrupt_tools_config(db, thread_id, "{broken")

    cfg = ts.get_thread_tools_config(db, thread_id)

    assert cfg.policy_error_kind == "payload_decode"
    assert cfg.llm_tools_enabled is False
    assert cfg.allowed_tools == set()
    assert not cfg.is_tool_allowed("allowed_tool")
    assert cfg.allow_raw_tool_output is False
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tools.policy_error' ORDER BY event_seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["error_kind"] == "payload_decode"
    assert payload["fail_closed"] is True


def test_ancestor_corrupt_policy_fails_descendant_closed(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _insert_corrupt_tools_config(db, parent, "[]")

    cfg = ts.get_thread_tools_config(db, child)

    assert cfg.policy_error_kind == "invalid_payload"
    assert cfg.policy_error_source_thread_id == parent
    assert not cfg.is_tool_allowed("allowed_tool")
    assert cfg.allow_raw_tool_output is False


def test_policy_db_read_failure_fails_closed(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")

    class FailingConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("simulated policy read failure")

    monkeypatch.setattr(db, "conn", FailingConnection())
    cfg = ts.get_thread_tools_config(db, thread_id)

    assert cfg.policy_error_kind == "ancestry_read"
    assert "simulated policy read failure" in (cfg.policy_error or "")
    assert cfg.llm_tools_enabled is False
    assert not cfg.is_tool_allowed("allowed_tool")
    assert cfg.allow_raw_tool_output is False


def test_child_creation_fails_before_insert_when_parent_policy_is_corrupt(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    _insert_corrupt_tools_config(db, parent, "{broken")
    before = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    with pytest.raises(ts.ToolPolicyReadError, match="payload_decode"):
        ts.create_child_thread(db, parent, name="must-not-exist")

    assert db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before
    diagnostic = db.conn.execute(
        "SELECT 1 FROM events WHERE thread_id=? AND type='tools.policy_error'",
        (parent,),
    ).fetchone()
    assert diagnostic is not None


def test_child_creation_rolls_back_when_parent_policy_changes_during_transaction(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    original_create = db.create_thread

    def change_policy_then_create(*args, **kwargs):
        ts.disable_tool_for_thread(db, parent, "bash")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(db, "create_thread", change_policy_then_create)
    before = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    with pytest.raises(ts.ToolPolicyReadError, match="policy_changed"):
        ts.create_child_thread(db, parent, name="must-not-exist")

    assert db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before
    assert db.conn.execute("SELECT COUNT(*) FROM children WHERE parent_id=?", (parent,)).fetchone()[0] == 0


def test_child_creation_rolls_back_when_initial_policy_event_fails(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    original_append = db.append_event

    def fail_initial_policy(*args, **kwargs):
        if kwargs.get("type_") == "tools.config" and kwargs.get("thread_id") != parent:
            raise sqlite3.OperationalError("simulated policy append failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(db, "append_event", fail_initial_policy)
    before = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    with pytest.raises(sqlite3.OperationalError, match="simulated policy append failure"):
        ts.create_child_thread(db, parent, name="must-not-exist")

    assert db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before
    assert db.conn.execute("SELECT COUNT(*) FROM children WHERE parent_id=?", (parent,)).fetchone()[0] == 0


def test_corrupt_policy_hides_tools_from_ra1(tmp_path):
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    ts.append_message(db, thread_id, "user", "hello")
    ts.create_snapshot(db, thread_id)
    _insert_corrupt_tools_config(db, thread_id, "{broken")

    llm = _ToolCallingLLM()
    runner = ts.ThreadRunner(db, thread_id, llm=llm, tools=_registry())
    assert asyncio.run(runner.run_once()) is True

    assert llm.seen_tool_names == set()


def test_corrupt_policy_denies_tool_execution(tmp_path):
    db = _make_db(tmp_path)
    thread_id = ts.create_root_thread(db, name="root")
    _insert_corrupt_tools_config(db, thread_id, "{broken")
    tool_call_id = ts.enqueue_user_tool_call(
        db,
        thread_id,
        "allowed_tool",
        {},
        content="allowed_tool()",
        hidden=True,
        auto_approve=True,
        approval_reason="test",
    )

    runner = ts.ThreadRunner(db, thread_id, llm=object(), tools=_registry())
    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True

    result = ts.get_user_command_result(db, thread_id, tool_call_id)
    assert result is not None
    assert "policy unavailable" in result.lower()
    assert "allowed output" not in result
