from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.runner import ThreadRunner
from eggthreads.tools import create_tool_registry


def _new_thread(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    return db, tid


def _messages(db, tid):
    return ts.create_snapshot(db, tid)["messages"]


def _tool_message(db, tid, tool_call_id):
    for msg in _messages(db, tid):
        if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
            return msg
    return None


def test_default_tool_registry_includes_answer_user_preserve_turn_tool():
    registry = create_tool_registry()

    assert "answer_user_while_preserving_llm_turn" in registry._tools
    spec = registry._tools["answer_user_while_preserving_llm_turn"]["spec"]["function"]
    assert spec["parameters"]["required"] == ["message"]


def test_answer_user_tool_appends_interim_assistant_message(tmp_path):
    db, tid = _new_thread(tmp_path)
    registry = create_tool_registry()

    out = registry.execute(
        "answer_user_while_preserving_llm_turn",
        {"message": "Still working, but here is the short answer."},
        thread_id=tid,
        db=db,
        initial_model_key="model-test",
    )

    assert out == "Interim answer shown to user."
    msg = _messages(db, tid)[-1]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Still working, but here is the short answer."
    assert msg["answer_user_preserve_turn"] is True
    assert msg["model_key"] == "model-test"


def test_answer_user_tool_call_publishes_hidden_keep_turn_result(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "please keep me updated")
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-answer-note",
                    "type": "function",
                    "function": {
                        "name": "answer_user_while_preserving_llm_turn",
                        "arguments": json.dumps({"message": "Interim note visible to user."}),
                    },
                }
            ]
        },
    )

    runner = ThreadRunner(db, tid, llm=object(), tools=create_tool_registry())
    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True

    messages = _messages(db, tid)
    note = next(msg for msg in messages if msg.get("answer_user_preserve_turn"))
    assert note["content"] == "Interim note visible to user."
    tool_msg = _tool_message(db, tid, "call-answer-note")
    assert tool_msg is not None
    assert tool_msg.get("no_api") is not True
    assert tool_msg.get("keep_user_turn") is not True


def test_model_can_answer_user_with_tool_then_continue_to_final_message(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "answer and keep working")
    ts.create_snapshot(db, tid)

    class LLM:
        current_model_key = "test-model"

        def __init__(self):
            self.calls = []

        async def astream_chat(self, messages, tools=None, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                yield {
                    "type": "done",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-answer-note",
                                "type": "function",
                                "function": {
                                    "name": "answer_user_while_preserving_llm_turn",
                                    "arguments": json.dumps({"message": "Interim answer."}),
                                },
                            }
                        ],
                    },
                }
            else:
                yield {"type": "done", "message": {"role": "assistant", "content": "Final answer."}}

    llm = LLM()
    runner = ThreadRunner(db, tid, llm=llm, tools=create_tool_registry())

    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True
    assert asyncio.run(runner.run_once()) is True

    messages = _messages(db, tid)
    assert any(msg.get("answer_user_preserve_turn") and msg.get("content") == "Interim answer." for msg in messages)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Final answer."
    tool_msg = _tool_message(db, tid, "call-answer-note")
    assert tool_msg is not None
    assert tool_msg.get("no_api") is not True
    assert tool_msg.get("keep_user_turn") is not True
    assert len(llm.calls) == 2
    assert all(not msg.get("answer_user_preserve_turn") for msg in llm.calls[1])
    assistant_tool_call = next(msg for msg in llm.calls[1] if msg.get("role") == "assistant" and msg.get("tool_calls"))
    assert assistant_tool_call["tool_calls"][0]["function"]["name"] == "answer_user_while_preserving_llm_turn"
    assert any(msg.get("role") == "tool" and msg.get("tool_call_id") == "call-answer-note" for msg in llm.calls[1])


def test_btw_command_appends_request_and_starts_scheduler(tmp_path):
    db, tid = _new_thread(tmp_path)
    started = []
    registry = create_default_command_registry()

    assert "btw" in registry.names()
    result = registry.execute(
        "btw",
        CommandContext(db=db, current_thread=tid, start_scheduler=started.append),
        "what is the current status?",
    )

    assert result.clear_input is True
    assert result.start_schedulers == (tid,)
    assert started == [tid]
    msg = _messages(db, tid)[-1]
    assert msg["role"] == "user"
    assert "answer_user_while_preserving_llm_turn" in msg["content"]
    assert "what is the current status?" in msg["content"]
