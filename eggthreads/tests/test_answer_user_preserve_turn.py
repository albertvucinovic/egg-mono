from __future__ import annotations

import asyncio
import json
import threading

import pytest

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


def _prepare_direct_get_user_execution(db, tid, tool_call_id, note, invoke_id):
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, tool_call_id, note, invoke_id, append_note=False)


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
    _prepare_direct_get_user_execution(
        db, tid, "call-get-user", "What should I use for the title?", "invoke-get-user"
    )

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
    _prepare_direct_get_user_execution(
        db, tid, "call-get-user", "Please provide the missing title.", "invoke-get-user"
    )

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

        user_msg_id = ts.append_normal_user_message(db, tid, "The Practical Guide")
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


def _declare_get_user_wait(db, tid, tool_call_id, note, invoke_id, *, append_note=True):
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [{
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": GET_USER_TOOL_NAME,
                    "arguments": json.dumps({"assistant_note": note}),
                },
            }],
        },
    )
    db.append_event(
        f"started-{tool_call_id}",
        tid,
        "tool_call.execution_started",
        {"tool_call_id": tool_call_id},
        invoke_id=invoke_id,
    )
    if append_note:
        ts.append_message(
            db,
            tid,
            "assistant",
            note,
            extra={
                "answer_user_preserve_turn": True,
                "source_tool_name": GET_USER_TOOL_NAME,
                "tool_call_id": tool_call_id,
                "awaiting_user_message_tool_call_id": tool_call_id,
            },
        )


def test_new_wait_terminalizes_older_sibling_before_blocking(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-older", "Older wait", "invoke-older")
    registry = create_tool_registry()
    cancelled = {"value": False}
    assert db.try_open_stream(tid, "invoke-newest", "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-newest", "Newest wait", "invoke-newest", append_note=False)

    async def run():
        task = asyncio.create_task(
            registry.execute_async(
                GET_USER_TOOL_NAME,
                {"assistant_note": "Newest wait"},
                thread_id=tid,
                db=db,
                cancel_check=lambda: cancelled["value"],
                stream=ToolStreamContext(
                    db=db,
                    thread_id=tid,
                    invoke_id="invoke-newest",
                    tool_call_id="call-get-user-newest",
                    tool_name=GET_USER_TOOL_NAME,
                ),
                preserve_tool_result=True,
            )
        )
        await _wait_for_message(
            db,
            tid,
            lambda message: message.get("awaiting_user_message_tool_call_id") == "call-get-user-newest",
        )
        messages = _messages(db, tid)
        old_result = next(
            message for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") == "call-get-user-older"
        )
        assert "superseded" in old_result["content"].lower()
        assert ts.build_tool_call_states(db, tid)["call-get-user-older"].state == "TC6"
        cancelled["value"] = True
        result = await asyncio.wait_for(task, timeout=1)
        assert isinstance(result, ToolExecutionResult)
        assert result.reason == "interrupted"

    asyncio.run(run())


def test_new_get_user_wait_durably_supersedes_older_wait_and_owns_reply(tmp_path):
    db, tid = _new_thread(tmp_path)
    invoke_id = "invoke-newest-get-user"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-old", "Old question?", "invoke-old")
    _declare_get_user_wait(db, tid, "call-get-user-new", "New question?", invoke_id)

    retired = ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id="call-get-user-new",
    )

    assert retired == ["call-get-user-old"]
    states = ts.build_tool_call_states(db, tid)
    assert states["call-get-user-old"].state == "TC6"
    assert states["call-get-user-new"].state == "TC3"
    old_result = _tool_message(db, tid, "call-get-user-old")
    assert old_result is not None
    assert old_result["content"] == (
        "Wait superseded by a newer get_user_message_while_preserving_llm_turn call."
    )
    assert old_result["keep_user_turn"] is True
    assert ts.get_active_get_user_message_waiting_note(db, tid)["tool_call_id"] == "call-get-user-new"

    reply_id = ts.append_normal_user_message(db, tid, "Answer only the new question")
    from eggthreads.builtin_plugins.answer_user import _claim_next_normal_user_message

    assert _claim_next_normal_user_message(
        db,
        tid,
        note_seq=states["call-get-user-old"].parent_event_seq,
        tool_call_id="call-get-user-old",
    ) is None
    new_note = ts.get_active_get_user_message_waiting_note(db, tid)
    assert new_note is None  # Canonical append already claimed the reply.
    claimed = _claim_next_normal_user_message(
        db,
        tid,
        note_seq=next(
            int(row[0])
            for row in db.conn.execute(
                "SELECT event_seq FROM events WHERE thread_id=? AND type='msg.create' "
                "AND json_extract(payload_json, '$.awaiting_user_message_tool_call_id')=?",
                (tid, "call-get-user-new"),
            )
        ),
        tool_call_id="call-get-user-new",
    )
    assert claimed == {"event_seq": claimed["event_seq"], "msg_id": reply_id, "content": "Answer only the new question"}
    reply = next(message for message in _messages(db, tid) if message.get("msg_id") == reply_id)
    assert reply["consumed_by_tool_call_id"] == "call-get-user-new"
    assert sum(
        1 for row in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.edit' AND msg_id=?",
            (tid, reply_id),
        )
        if json.loads(row[0]).get("consumed_by_tool_call_id")
    ) == 1


def test_concurrent_claimers_cannot_consume_one_reply_twice(tmp_path):
    db, tid = _new_thread(tmp_path)
    invoke_id = "invoke-concurrent-claim"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-old-race", "Old?", "invoke-old")
    _declare_get_user_wait(db, tid, "call-get-user-new-race", "New?", invoke_id)
    ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id="call-get-user-new-race",
    )
    notes = {
        json.loads(row["payload_json"])["awaiting_user_message_tool_call_id"]: int(row["event_seq"])
        for row in db.conn.execute(
            "SELECT event_seq, payload_json FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.awaiting_user_message_tool_call_id') IS NOT NULL",
            (tid,),
        )
    }
    reply_id = ts.append_normal_user_message(db, tid, "single reply")
    barrier = threading.Barrier(2)
    results = {}

    def claim(tool_call_id):
        from eggthreads.builtin_plugins.answer_user import _claim_next_normal_user_message

        worker_db = ts.ThreadsDB(db.path)
        barrier.wait()
        results[tool_call_id] = _claim_next_normal_user_message(
            worker_db,
            tid,
            note_seq=notes[tool_call_id],
            tool_call_id=tool_call_id,
        )
        worker_db.conn.close()

    threads = [
        threading.Thread(target=claim, args=("call-get-user-old-race",)),
        threading.Thread(target=claim, args=("call-get-user-new-race",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert results["call-get-user-old-race"] is None
    assert results["call-get-user-new-race"]["msg_id"] == reply_id
    claimed_edits = [
        json.loads(row[0])
        for row in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.edit' AND msg_id=?",
            (tid, reply_id),
        )
        if json.loads(row[0]).get("consumed_by_tool_call_id")
    ]
    assert [edit["consumed_by_tool_call_id"] for edit in claimed_edits] == ["call-get-user-new-race"]


def test_thread_continue_terminalizes_wait_before_skipping_and_preserves_protocol(tmp_path):
    db, tid = _new_thread(tmp_path)
    anchor = ts.append_message(db, tid, "user", "anchor")
    _declare_get_user_wait(db, tid, "call-get-user-stale", "Waiting", "invoke-stale")

    result = ts.continue_thread(db, tid, msg_id=anchor)

    assert result.success is True
    state = ts.build_tool_call_states(db, tid)["call-get-user-stale"]
    assert state.state == "TC6"
    snapshot = ts.create_snapshot(db, tid)
    assert any(
        message.get("role") == "assistant"
        and any(call.get("id") == "call-get-user-stale" for call in message.get("tool_calls") or [])
        for message in snapshot["messages"]
    )
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "call-get-user-stale"
        and message.get("content") == "Wait superseded by thread continuation."
        for message in snapshot["messages"]
    )


def test_terminalization_publishes_preexisting_tc5_decision_without_overwriting(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-tc5", "Pending", "invoke-tc5")
    state = ts.build_tool_call_states(db, tid)["call-get-user-tc5"]
    ts.finalize_tool_output(
        db,
        tid,
        "call-get-user-tc5",
        decision="whole",
        source="automatic_policy",
        reason="existing",
        expected_state=("TC3",),
        expected_event_seq=state.state_event_seq,
        publication_plan=ts.ToolOutputPublicationPlan(
            decision="whole",
            preview="Existing canonical result",
            reason="existing",
        ),
    )

    retired = ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id=None,
    )

    assert retired == ["call-get-user-tc5"]
    result = _tool_message(db, tid, "call-get-user-tc5")
    assert result["content"] == "Existing canonical result"
    approvals = list(db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.output_approval' "
        "AND json_extract(payload_json, '$.tool_call_id')=?",
        (tid, "call-get-user-tc5"),
    ))
    assert len(approvals) == 1


def test_normal_user_input_terminalizes_orphan_wait_without_claiming_reply(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-orphan", "Orphan?", "invoke-gone")

    reply_id = ts.append_normal_user_message(db, tid, "Fresh user turn")

    state = ts.build_tool_call_states(db, tid)["call-get-user-orphan"]
    assert state.state == "TC6"
    result = _tool_message(db, tid, "call-get-user-orphan")
    assert result is not None
    assert result["content"] == "Wait superseded because no active get-user owner remained."
    reply = next(message for message in _messages(db, tid) if message.get("msg_id") == reply_id)
    assert reply["role"] == "user"
    assert reply.get("consumed_by_tool_call_id") is None


def test_unrelated_tool_and_non_normal_user_input_do_not_change_get_user_owner(tmp_path):
    db, tid = _new_thread(tmp_path)
    invoke_id = "invoke-get-user-owner"
    assert db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-owner", "Reply", invoke_id)
    ts.append_message(
        db,
        tid,
        "user",
        "$ echo sibling",
        extra={"tool_calls": [{"id": "call-bash", "type": "function", "function": {"name": "bash", "arguments": "{}"}}], "keep_user_turn": True},
    )

    note = ts.get_active_get_user_message_waiting_note(db, tid)
    assert note is not None
    assert note["tool_call_id"] == "call-get-user-owner"
    assert ts.build_tool_call_states(db, tid)["call-get-user-owner"].state == "TC3"


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

        user_msg_id = ts.append_normal_user_message(db, tid, "The Practical Guide")
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
        assert tool_msg.get("keep_user_turn") is not True

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


def test_two_quick_normal_submissions_claim_first_and_wake_waiter(tmp_path):
    db, tid = _new_thread(tmp_path)
    registry = create_tool_registry()
    _prepare_direct_get_user_execution(
        db, tid, "call-get-user-double", "Reply once", "invoke-get-user-double"
    )

    async def run():
        task = asyncio.create_task(
            registry.execute_async(
                GET_USER_TOOL_NAME,
                {"assistant_note": "Reply once"},
                thread_id=tid,
                db=db,
                stream=ToolStreamContext(
                    db=db,
                    thread_id=tid,
                    invoke_id="invoke-get-user-double",
                    tool_call_id="call-get-user-double",
                    tool_name=GET_USER_TOOL_NAME,
                ),
                preserve_tool_result=True,
            )
        )
        await _wait_for_message(
            db,
            tid,
            lambda msg: msg.get("awaiting_user_message_tool_call_id") == "call-get-user-double",
        )
        first = ts.append_normal_user_message(db, tid, "first")
        second_db = ts.ThreadsDB(db.path)
        second = ts.append_normal_user_message(second_db, tid, "second")
        second_db.conn.close()

        assert await asyncio.wait_for(task, timeout=1) == "first"
        messages = _messages(db, tid)
        first_msg = next(msg for msg in messages if msg.get("msg_id") == first)
        second_msg = next(msg for msg in messages if msg.get("msg_id") == second)
        assert first_msg["consumed_by_tool_call_id"] == "call-get-user-double"
        assert second_msg.get("consumed_by_tool_call_id") is None
        assert ts.build_tool_call_states(db, tid)["call-get-user-double"].state == "TC3"

    asyncio.run(run())


def test_stale_lost_lease_wait_cannot_append_note_or_retire_live_owner(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-stale", "Stale", "invoke-stale")
    ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id=None,
        content="Stale finished",
    )
    assert db.try_open_stream(tid, "invoke-live", "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-live", "Live", "invoke-live")
    before = db.max_event_seq(tid)
    registry = create_tool_registry()

    async def run():
        result = await registry.execute_async(
            GET_USER_TOOL_NAME,
            {"assistant_note": "Stale resumed"},
            thread_id=tid,
            db=db,
            invoke_id="invoke-stale",
            tool_call_id="call-get-user-stale",
            cancel_check=lambda: True,
            stream=ToolStreamContext(
                db=db,
                thread_id=tid,
                invoke_id="invoke-stale",
                tool_call_id="call-get-user-stale",
                tool_name=GET_USER_TOOL_NAME,
            ),
            preserve_tool_result=True,
        )
        assert str(result).startswith("Error: failed to append user-message prompt:")

    asyncio.run(run())
    assert db.max_event_seq(tid) == before
    states = ts.build_tool_call_states(db, tid)
    assert states["call-get-user-stale"].state == "TC6"
    assert states["call-get-user-live"].state == "TC3"
    assert ts.get_active_get_user_message_waiting_note(db, tid)["tool_call_id"] == "call-get-user-live"


def test_successful_tc4_recovery_publishes_exact_finished_output(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-tc4", "Pending", "invoke-tc4")
    db.append_event(
        "finished-success-tc4",
        tid,
        "tool_call.finished",
        {"tool_call_id": "call-get-user-tc4", "reason": "success", "output": "exact answer"},
        invoke_id="invoke-tc4",
    )

    retired = ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id=None,
    )

    assert retired == ["call-get-user-tc4"]
    assert ts.build_tool_call_states(db, tid)["call-get-user-tc4"].state == "TC6"
    assert _tool_message(db, tid, "call-get-user-tc4")["content"] == "exact answer"


def test_legacy_skipped_wait_is_lazily_preserved_and_terminalized(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-legacy", "Legacy", "invoke-legacy")
    state = ts.build_tool_call_states(db, tid)["call-get-user-legacy"]
    note_msg_id = next(
        row[0]
        for row in db.conn.execute(
            "SELECT msg_id FROM events WHERE thread_id=? AND type='msg.create' "
            "AND json_extract(payload_json, '$.awaiting_user_message_tool_call_id')=?",
            (tid, "call-get-user-legacy"),
        )
    )
    for msg_id in (state.parent_msg_id, note_msg_id):
        db.append_event(
            f"skip-{msg_id}",
            tid,
            "msg.edit",
            {"skipped_on_continue": True},
            msg_id=msg_id,
        )

    retired = ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id=None,
        content="Legacy wait recovered",
    )

    assert retired == ["call-get-user-legacy"]
    snapshot = ts.create_snapshot(db, tid)
    assert any(
        msg.get("role") == "assistant"
        and any(call.get("id") == "call-get-user-legacy" for call in msg.get("tool_calls") or [])
        for msg in snapshot["messages"]
    )
    assert _tool_message(db, tid, "call-get-user-legacy")["content"] == "Legacy wait recovered"


def test_multiwait_provider_projection_keeps_exact_results_contiguous(tmp_path):
    db, tid = _new_thread(tmp_path)
    ts.append_message(db, tid, "user", "start")
    _declare_get_user_wait(db, tid, "call-get-user-provider-old", "Old", "invoke-old")
    _declare_get_user_wait(db, tid, "call-get-user-provider-new", "New", "invoke-new")
    ts.terminalize_superseded_get_user_waits(
        db,
        tid,
        authoritative_tool_call_id=None,
        content="new answer",
    )
    snapshot = ts.create_snapshot(db, tid)

    class DummyRunner(ThreadRunner):
        def __init__(self):
            self.db = db
            self.thread_id = tid
            self.llm = None

        def _get_tool_call_id_normalization_strategy(self, model_key=None):
            return None

    provider = DummyRunner()._sanitize_messages_for_api(
        [message for message in snapshot["messages"] if not message.get("no_api")],
        tools_cfg=type("Tools", (), {"allow_raw_tool_output": True})(),
    )
    protocol = [
        (
            message.get("role"),
            [call.get("id") for call in message.get("tool_calls") or []],
            message.get("tool_call_id"),
            message.get("content"),
        )
        for message in provider
        if message.get("role") in {"assistant", "tool"}
    ]
    assert protocol == [
        ("assistant", ["call-get-user-provider-old"], None, ""),
        ("tool", [], "call-get-user-provider-old", "new answer"),
        ("assistant", ["call-get-user-provider-new"], None, ""),
        ("tool", [], "call-get-user-provider-new", "new answer"),
    ]


def test_failed_continue_does_not_mutate_wait_lifecycle(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-continue-fail", "Waiting", "invoke-gone")
    before = db.max_event_seq(tid)

    result = ts.continue_thread(db, tid, msg_id="missing")

    assert result.success is False
    assert db.max_event_seq(tid) == before
    assert ts.build_tool_call_states(db, tid)["call-get-user-continue-fail"].state == "TC3"



def test_normal_append_hot_path_does_not_rescan_historical_wait_notes(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    for index in range(120):
        tool_call_id = f"call-completed-{index}"
        _declare_get_user_wait(db, tid, tool_call_id, f"note {index}", f"invoke-{index}")
        ts.terminalize_superseded_get_user_waits(
            db,
            tid,
            authoritative_tool_call_id=None,
            content=f"done {index}",
        )
    # Seed the canonical reducer cache at the current event watermark. The send
    # boundary must inspect its bounded unresolved-candidate index, not run a
    # msg.create history scan or one reducer rebuild per old note.
    ts.build_tool_call_states(db, tid)
    traced = []
    db.conn.set_trace_callback(traced.append)

    ts.append_normal_user_message(db, tid, "ordinary message")

    db.conn.set_trace_callback(None)
    normalized = [statement.lower() for statement in traced]
    assert not any(
        "type='msg.create'" in statement and "order by event_seq asc" in statement
        for statement in normalized
    )
    assert sum("select * from events" in statement for statement in normalized) <= 1



def test_legacy_skipped_published_result_is_repaired_without_duplicate(tmp_path):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-legacy-result", "Legacy", "invoke-legacy-result")
    ts.terminalize_superseded_get_user_waits(
        db, tid, authoritative_tool_call_id=None, content="canonical result"
    )
    state = ts.build_tool_call_states(db, tid)["call-get-user-legacy-result"]
    for msg_id in (state.parent_msg_id, state.waiting_note_msg_id, state.result_msg_id):
        db.append_event(
            f"skip-result-{msg_id}", tid, "msg.edit",
            {"skipped_on_continue": True}, msg_id=msg_id,
        )

    repaired = ts.terminalize_superseded_get_user_waits(
        db, tid, authoritative_tool_call_id=None, content="must not replace"
    )

    assert repaired == ["call-get-user-legacy-result"]
    results = [
        msg for msg in ts.create_snapshot(db, tid)["messages"]
        if msg.get("role") == "tool" and msg.get("tool_call_id") == "call-get-user-legacy-result"
    ]
    assert [msg["content"] for msg in results] == ["canonical result"]



def test_two_connection_simultaneous_submissions_have_one_deterministic_claim(tmp_path):
    db, tid = _new_thread(tmp_path)
    assert db.try_open_stream(tid, "invoke-two-client", "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-two-client", "Reply", "invoke-two-client")
    barrier = threading.Barrier(2)
    outcomes = {}

    def submit(label):
        worker = ts.ThreadsDB(db.path)
        barrier.wait()
        msg_id = ts.append_normal_user_message(worker, tid, label)
        row = worker.conn.execute(
            "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create'",
            (tid, msg_id),
        ).fetchone()
        outcomes[label] = (msg_id, int(row[0]))
        worker.conn.close()

    clients = [threading.Thread(target=submit, args=(label,)) for label in ("client-a", "client-b")]
    for client in clients:
        client.start()
    for client in clients:
        client.join(timeout=2)
        assert not client.is_alive()

    messages = _messages(db, tid)
    claimed = [
        msg for msg in messages
        if msg.get("consumed_by_tool_call_id") == "call-get-user-two-client"
    ]
    assert len(claimed) == 1
    first_label = min(outcomes, key=lambda label: outcomes[label][1])
    assert claimed[0]["content"] == first_label
    assert ts.build_tool_call_states(db, tid)["call-get-user-two-client"].state == "TC3"



def test_orphaned_claim_is_published_exactly_before_next_user_turn(tmp_path):
    db, tid = _new_thread(tmp_path)
    assert db.try_open_stream(tid, "invoke-claimed-orphan", "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(db, tid, "call-get-user-claimed-orphan", "Reply", "invoke-claimed-orphan")
    first = ts.append_normal_user_message(db, tid, "claimed answer")
    db.conn.execute("DELETE FROM open_streams WHERE thread_id=?", (tid,))

    second = ts.append_normal_user_message(db, tid, "next turn")

    assert _tool_message(db, tid, "call-get-user-claimed-orphan")["content"] == "claimed answer"
    assert ts.build_tool_call_states(db, tid)["call-get-user-claimed-orphan"].state == "TC6"
    messages = _messages(db, tid)
    assert next(msg for msg in messages if msg.get("msg_id") == first)["consumed_by_tool_call_id"] == "call-get-user-claimed-orphan"
    assert next(msg for msg in messages if msg.get("msg_id") == second).get("consumed_by_tool_call_id") is None



def test_start_boundary_rolls_back_note_when_retiring_older_wait_fails(tmp_path, monkeypatch):
    db, tid = _new_thread(tmp_path)
    _declare_get_user_wait(db, tid, "call-get-user-start-old", "Old", "invoke-old")
    assert db.try_open_stream(tid, "invoke-start-new", "2999-01-01 00:00:00", owner="test", purpose="tool")
    _declare_get_user_wait(
        db, tid, "call-get-user-start-new", "New", "invoke-start-new", append_note=False
    )
    before = db.max_event_seq(tid)

    def fail_terminalization(*args, **kwargs):
        raise RuntimeError("forced retirement failure")

    monkeypatch.setattr("eggthreads.api.terminalize_superseded_get_user_waits", fail_terminalization)
    with pytest.raises(RuntimeError, match="forced retirement failure"):
        ts.start_get_user_message_wait(
            db,
            tid,
            invoke_id="invoke-start-new",
            tool_call_id="call-get-user-start-new",
            assistant_note="New",
        )

    assert db.max_event_seq(tid) == before
    assert not any(
        json.loads(row[0]).get("awaiting_user_message_tool_call_id") == "call-get-user-start-new"
        for row in db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
            (tid,),
        )
    )



def test_legacy_skip_then_preserve_projection_restores_message(tmp_path):
    db, tid = _new_thread(tmp_path)
    msg_id = ts.append_message(db, tid, "assistant", "restored")
    db.append_event("skip-restored", tid, "msg.edit", {"skipped_on_continue": True}, msg_id=msg_id)
    assert not any(msg.get("msg_id") == msg_id for msg in ts.create_snapshot(db, tid)["messages"])

    db.append_event("preserve-restored", tid, "msg.edit", {"preserve_on_continue": True}, msg_id=msg_id)

    assert any(msg.get("msg_id") == msg_id for msg in ts.create_snapshot(db, tid)["messages"])
