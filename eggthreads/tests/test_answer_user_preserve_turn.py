from __future__ import annotations

import asyncio
import json

import eggthreads as ts
from eggthreads.approval import APPROVAL_ALLOW, ApprovalRequest, create_approval_policy_registry
from eggthreads.command_catalog import CommandContext, create_default_command_registry
from eggthreads.runner import ThreadRunner
from eggthreads.tool_state import AUTO_APPROVED_TOOL_NAMES
from eggthreads.tools import ToolExecutionResult, ToolStreamContext, create_tool_registry


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


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


async def _wait_for_message(db, tid, predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for msg in _messages(db, tid):
            if predicate(msg):
                return msg
        await asyncio.sleep(0.01)
    raise AssertionError("message was not appended before timeout")


def test_default_tool_registry_includes_answer_user_preserve_turn_tool():
    registry = create_tool_registry()

    assert "answer_user_while_preserving_llm_turn" in registry._tools
    spec = registry._tools["answer_user_while_preserving_llm_turn"]["spec"]["function"]
    assert spec["parameters"]["required"] == ["message"]


def test_default_tool_registry_includes_get_user_message_preserve_turn_tool():
    registry = create_tool_registry()

    assert GET_USER_TOOL_NAME in registry._tools
    exposed_names = [spec["function"]["name"] for spec in registry.tools_spec()]
    assert GET_USER_TOOL_NAME in exposed_names
    spec = registry._tools[GET_USER_TOOL_NAME]["spec"]["function"]
    assert spec["parameters"]["required"] == ["assistant_note"]
    assert spec["parameters"]["additionalProperties"] is False
    assert "assistant_note" in spec["parameters"]["properties"]
    assert GET_USER_TOOL_NAME in AUTO_APPROVED_TOOL_NAMES


def test_default_approval_policy_allows_get_user_message_preserve_turn_tool():
    registry = create_approval_policy_registry()

    verdict = registry.get("compact_thread").evaluate(
        ApprovalRequest(
            db=None,
            thread_id="tid",
            tool_call_id="tcid",
            tool_name=GET_USER_TOOL_NAME,
            origin="assistant",
            parent_role="assistant",
        )
    )

    assert verdict.decision == APPROVAL_ALLOW


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


def test_get_user_message_tool_appends_note_with_metadata_and_cancels(tmp_path):
    db, tid = _new_thread(tmp_path)
    registry = create_tool_registry()
    cancelled = {"value": False}

    async def run():
        task = asyncio.create_task(
            registry.execute_async(
                GET_USER_TOOL_NAME,
                {"assistant_note": "What should I use for the title?"},
                thread_id=tid,
                db=db,
                initial_model_key="model-test",
                cancel_check=lambda: cancelled["value"],
                stream=ToolStreamContext(
                    db=db,
                    thread_id=tid,
                    invoke_id="invoke-get-user",
                    tool_call_id="call-get-user",
                    tool_name=GET_USER_TOOL_NAME,
                ),
                preserve_tool_result=True,
            )
        )

        note = await _wait_for_message(
            db,
            tid,
            lambda msg: msg.get("content") == "What should I use for the title?",
        )
        assert note["role"] == "assistant"
        assert note["answer_user_preserve_turn"] is True
        assert note["model_key"] == "model-test"
        assert note["source_tool_name"] == GET_USER_TOOL_NAME
        assert note["tool_call_id"] == "call-get-user"
        assert note["awaiting_user_message_tool_call_id"] == "call-get-user"

        cancelled["value"] = True
        result = await asyncio.wait_for(task, timeout=1)
        assert isinstance(result, ToolExecutionResult)
        assert result.reason == "interrupted"
        assert "INTERRUPTED" in result.output

    asyncio.run(run())


def test_get_user_message_tool_returns_later_user_message_and_marks_it_consumed(tmp_path):
    db, tid = _new_thread(tmp_path)
    registry = create_tool_registry()

    async def run():
        task = asyncio.create_task(
            registry.execute_async(
                GET_USER_TOOL_NAME,
                {"assistant_note": "Please provide the missing title."},
                thread_id=tid,
                db=db,
                initial_model_key="model-test",
                stream=ToolStreamContext(
                    db=db,
                    thread_id=tid,
                    invoke_id="invoke-get-user",
                    tool_call_id="call-get-user",
                    tool_name=GET_USER_TOOL_NAME,
                ),
            )
        )
        await _wait_for_message(
            db,
            tid,
            lambda msg: msg.get("content") == "Please provide the missing title.",
        )

        user_msg_id = ts.append_message(db, tid, "user", "The Practical Guide")
        result = await asyncio.wait_for(task, timeout=1)

        assert result == "The Practical Guide"
        messages = _messages(db, tid)
        user_msg = next(msg for msg in messages if msg.get("msg_id") == user_msg_id)
        assert user_msg["role"] == "user"
        assert user_msg["content"] == "The Practical Guide"
        assert user_msg["no_api"] is True
        assert user_msg["keep_user_turn"] is True
        assert user_msg["consumed_by_tool_call_id"] == "call-get-user"
        assert user_msg["consumed_by_tool_name"] == GET_USER_TOOL_NAME

        edit_row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND msg_id=? AND type='msg.edit' ORDER BY event_seq DESC LIMIT 1",
            (tid, user_msg_id),
        ).fetchone()
        assert edit_row is not None
        edit_payload = json.loads(edit_row[0])
        assert edit_payload["no_api"] is True
        assert edit_payload["keep_user_turn"] is True
        assert edit_payload["consumed_by_tool_call_id"] == "call-get-user"
        assert edit_payload["consumed_by_tool_name"] == GET_USER_TOOL_NAME

    asyncio.run(run())


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


def test_model_can_get_user_message_with_tool_then_continue_to_final_message(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "Ask me for a title, then continue.")
    ts.create_snapshot(db, tid)

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
                                "id": "call-get-user-e2e",
                                "type": "function",
                                "function": {
                                    "name": GET_USER_TOOL_NAME,
                                    "arguments": json.dumps({"assistant_note": "What title should I use?"}),
                                },
                            }
                        ],
                    },
                }
            else:
                assert any(
                    msg.get("role") == "tool"
                    and msg.get("tool_call_id") == "call-get-user-e2e"
                    and msg.get("content") == "The Practical Guide"
                    for msg in messages
                )
                assert not any(
                    msg.get("role") == "user" and msg.get("content") == "The Practical Guide"
                    for msg in messages
                )
                assert all(not msg.get("answer_user_preserve_turn") for msg in messages)
                yield {
                    "type": "done",
                    "message": {
                        "role": "assistant",
                        "content": "Final response using The Practical Guide.",
                    },
                }

    async def run():
        llm = LLM()
        runner = ThreadRunner(db, tid, llm=llm, tools=create_tool_registry())

        assert await runner.run_once() is True

        waiting_task = asyncio.create_task(runner.run_once())
        note = await _wait_for_message(
            db,
            tid,
            lambda msg: msg.get("awaiting_user_message_tool_call_id") == "call-get-user-e2e",
        )
        assert note["content"] == "What title should I use?"

        wait_result = ts.wait_for_threads(db, [tid], timeout_sec=0)[tid]
        assert wait_result.finished is True
        assert wait_result.state == "waiting_user"
        assert wait_result.last_assistant_message == "What title should I use?"

        user_msg_id = ts.append_message(db, tid, "user", "The Practical Guide")
        assert await asyncio.wait_for(waiting_task, timeout=2) is True

        messages = _messages(db, tid)
        consumed_user = next(msg for msg in messages if msg.get("msg_id") == user_msg_id)
        assert consumed_user["role"] == "user"
        assert consumed_user["content"] == "The Practical Guide"
        assert consumed_user["no_api"] is True
        assert consumed_user["keep_user_turn"] is True
        assert consumed_user["consumed_by_tool_call_id"] == "call-get-user-e2e"
        assert consumed_user["consumed_by_tool_name"] == GET_USER_TOOL_NAME

        assert await runner.run_once() is True
        tool_msg = _tool_message(db, tid, "call-get-user-e2e")
        assert tool_msg is not None
        assert tool_msg["content"] == "The Practical Guide"

        assert await runner.run_once() is True
        assert await runner.run_once() is False

        messages = _messages(db, tid)
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"] == "Final response using The Practical Guide."
        assert len(llm.calls) == 2
        assert ts.discover_runner_actionable(db, tid) is None

    asyncio.run(run())


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
