from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


class _ToolCallingLLM:
    current_model_key = "test-model"

    def __init__(self):
        self.seen_tool_names: set[str] = set()

    def set_model(self, model_key):
        self.current_model_key = model_key

    def set_model_with_config(self, model_key, config):
        self.current_model_key = model_key

    async def astream_chat(self, messages, tools=None, tool_choice=None, timeout=None):
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
