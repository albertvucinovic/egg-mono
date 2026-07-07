from __future__ import annotations

import asyncio
import json
from pathlib import Path

import eggthreads as ts
from eggthreads.runner import ThreadRunner
from eggthreads.tools import create_default_tools, create_tool_registry


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_send_message_to_child_api_appends_user_message_to_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "system", "system")
    ts.append_message(db, child, "user", "initial")
    ts.append_message(db, child, "assistant", "done")
    ts.create_snapshot(db, child)

    msg_id = ts.send_message_to_child_thread(db, parent, child, "please refine")

    assert msg_id
    row = db.get_thread(child)
    assert row and row.snapshot_json
    messages = json.loads(row.snapshot_json)["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "please refine"
    assert messages[-1]["origin"] == "manager_message"
    assert messages[-1]["from_thread_id"] == parent


def test_send_message_to_child_answers_active_get_user_message_tool(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "Ask the manager for the next task.")
    ts.create_snapshot(db, child)

    class LLM:
        current_model_key = "test-model"

        def __init__(self):
            self.calls = []

        async def astream_chat(self, messages, tools=None, **kwargs):
            self.calls.append([dict(msg) for msg in messages])
            if len(self.calls) == 1:
                yield {
                    "type": "done",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-get-user-child",
                                "type": "function",
                                "function": {
                                    "name": GET_USER_TOOL_NAME,
                                    "arguments": json.dumps({"assistant_note": "Ready for next slice."}),
                                },
                            }
                        ],
                    },
                }
            else:
                assert any(
                    msg.get("role") == "tool"
                    and msg.get("tool_call_id") == "call-get-user-child"
                    and msg.get("content") == "Continue with Phase 2."
                    for msg in messages
                )
                assert not any(
                    msg.get("role") == "user" and msg.get("content") == "Continue with Phase 2."
                    for msg in messages
                )
                yield {"type": "done", "message": {"role": "assistant", "content": "Continuing."}}

    async def wait_for_note():
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
            for msg in messages:
                if msg.get("awaiting_user_message_tool_call_id") == "call-get-user-child":
                    return msg
            await asyncio.sleep(0.01)
        raise AssertionError("get-user note was not appended")

    async def run():
        runner = ThreadRunner(db, child, llm=LLM(), tools=create_tool_registry())

        assert await runner.run_once() is True

        waiting_task = asyncio.create_task(runner.run_once())
        note = await wait_for_note()
        assert note["content"] == "Ready for next slice."

        msg_id = ts.send_message_to_child_thread(db, parent, child, "Continue with Phase 2.")
        assert await asyncio.wait_for(waiting_task, timeout=2) is True

        messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
        manager_msg = next(msg for msg in messages if msg.get("msg_id") == msg_id)
        assert manager_msg["role"] == "user"
        assert manager_msg["content"] == "Continue with Phase 2."
        assert manager_msg["origin"] == "manager_message"
        assert manager_msg["from_thread_id"] == parent
        assert manager_msg["no_api"] is True
        assert manager_msg["keep_user_turn"] is True
        assert manager_msg["consumed_by_tool_call_id"] == "call-get-user-child"
        assert manager_msg["consumed_by_tool_name"] == GET_USER_TOOL_NAME

        assert await runner.run_once() is True
        messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
        tool_msg = next(msg for msg in messages if msg.get("role") == "tool")
        assert tool_msg["tool_call_id"] == "call-get-user-child"
        assert tool_msg["content"] == "Continue with Phase 2."

        assert await runner.run_once() is True
        assert json.loads(db.get_thread(child).snapshot_json)["messages"][-1]["content"] == "Continuing."

    asyncio.run(run())


def test_send_message_to_child_api_rejects_non_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")

    try:
        ts.send_message_to_child_thread(db, parent, other, "nope")
    except ValueError as e:
        assert "descendant" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_send_message_to_child_tool_uses_current_thread_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "system", "system")
    ts.append_message(db, child, "user", "initial")
    ts.append_message(db, child, "assistant", "done")
    ts.create_snapshot(db, child)

    out = create_default_tools().execute(
        "send_message_to_child",
        {"child_thread_id": child, "message": "next step"},
        thread_id=parent,
    )

    assert "Sent message" in out
    messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
    assert messages[-1]["content"] == "next step"


def test_send_message_to_child_registered_and_exposed():
    tools = create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "send_message_to_child" in names


def test_continue_child_thread_api_continues_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "initial")
    assistant_msg = ts.append_message(db, child, "assistant", "bad partial")
    ts.append_message(db, child, "system", "LLM/runner error: provider exploded")
    ts.create_snapshot(db, child)

    result = ts.continue_child_thread(db, parent, child, msg_id=assistant_msg)

    assert result.success is True
    assert result.continue_from_msg_id == assistant_msg
    messages = json.loads(db.get_thread(child).snapshot_json)["messages"]
    assert [m["content"] for m in messages] == ["initial", "bad partial"]


def test_continue_child_thread_api_rejects_non_descendant(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    other = ts.create_root_thread(db, name="other")

    result = ts.continue_child_thread(db, parent, other)

    assert result.success is False
    assert "descendant" in result.message


def test_continue_subthread_tool_uses_current_thread_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    ts.append_message(db, child, "user", "initial")
    assistant_msg = ts.append_message(db, child, "assistant", "bad partial")
    ts.append_message(db, child, "system", "LLM/runner error: provider exploded")
    ts.create_snapshot(db, child)

    out = create_default_tools().execute(
        "continue_subthread",
        {"child_thread_id": child, "msg_id": assistant_msg},
        thread_id=parent,
    )
    payload = json.loads(out)

    assert payload["success"] is True
    assert payload["continue_from_msg_id"] == assistant_msg
    messages = ts.create_snapshot(db, child)["messages"]
    notices = [msg for msg in messages if msg.get("recovery_notice")]
    assert len(notices) == 1
    assert "manual continue_subthread" in notices[0]["content"]
    assert "Previous error: LLM/runner error: provider exploded" in notices[0]["content"]


def test_continue_subthread_registered_and_exposed():
    tools = create_default_tools()
    names = {spec["function"]["name"] for spec in tools.tools_spec()}
    assert "continue_subthread" in names
