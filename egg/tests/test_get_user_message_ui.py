from __future__ import annotations

import json

import eggthreads as ts


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


def _append_event(db, tid: str, type_: str, payload: dict, *, msg_id: str | None = None, invoke_id: str | None = None) -> None:
    db.append_event(
        event_id=f"{type_}-{db.max_event_seq(tid) + 1}",
        thread_id=tid,
        type_=type_,
        payload=payload,
        msg_id=msg_id,
        invoke_id=invoke_id,
    )


def _start_get_user_wait(egg_app, *, note: str = "What title should I use?") -> None:
    tid = egg_app.current_thread
    invoke_id = "invoke-get-user-ui"
    assert egg_app.db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        egg_app.db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-get-user-ui",
                    "type": "function",
                    "function": {
                        "name": GET_USER_TOOL_NAME,
                        "arguments": json.dumps({"assistant_note": note}),
                    },
                }
            ]
        },
    )
    _append_event(
        egg_app.db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "call-get-user-ui"},
        invoke_id=invoke_id,
    )
    ts.append_message(
        egg_app.db,
        tid,
        "assistant",
        note,
        extra={
            "answer_user_preserve_turn": True,
            "source_tool_name": GET_USER_TOOL_NAME,
            "tool_call_id": "call-get-user-ui",
            "awaiting_user_message_tool_call_id": "call-get-user-ui",
        },
    )
    ts.create_snapshot(egg_app.db, tid)


def _start_normal_tool(egg_app) -> None:
    tid = egg_app.current_thread
    invoke_id = "invoke-bash-ui"
    assert egg_app.db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        egg_app.db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": "call-bash-ui",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"script": "sleep 10"})},
                }
            ]
        },
    )
    _append_event(egg_app.db, tid, "tool_call.approval", {"tool_call_id": "call-bash-ui", "decision": "granted"})
    _append_event(
        egg_app.db,
        tid,
        "tool_call.execution_started",
        {"tool_call_id": "call-bash-ui"},
        invoke_id=invoke_id,
    )
    ts.create_snapshot(egg_app.db, tid)


def _messages(egg_app):
    return ts.create_snapshot(egg_app.db, egg_app.current_thread)["messages"]


def test_input_panel_marks_active_get_user_answer_mode_and_restores_after_reply(egg_app):
    normal_title = egg_app.input_panel.title
    normal_border = egg_app.input_panel.style.border_style

    _start_get_user_wait(egg_app)
    egg_app.update_panels()

    assert egg_app.input_panel.title == "Message Input (get answer tool)"
    assert egg_app.input_panel.style.border_style != normal_border

    ts.append_message(egg_app.db, egg_app.current_thread, "user", "The Practical Guide")
    ts.create_snapshot(egg_app.db, egg_app.current_thread)
    egg_app.update_panels()

    assert egg_app.input_panel.title == normal_title
    assert egg_app.input_panel.style.border_style == normal_border


def test_slash_command_during_active_get_user_wait_does_not_answer_tool(egg_app):
    _start_get_user_wait(egg_app)
    egg_app.input_panel.editor.editor.set_text("/help")

    result = egg_app.handle_key("\x04")

    assert result is True
    assert ts.get_active_get_user_message_waiting_note(egg_app.db, egg_app.current_thread) is not None
    messages = _messages(egg_app)
    assert not any(msg.get("role") == "user" and msg.get("content") == "/help" for msg in messages)
    assert any("Help" in msg or "help" in msg.lower() for msg in egg_app._system_log)


def test_shell_command_during_active_get_user_wait_does_not_answer_tool(egg_app):
    _start_get_user_wait(egg_app)
    egg_app.input_panel.editor.editor.set_text("$ echo hi")

    result = egg_app.handle_key("\x04")

    assert result is True
    assert ts.get_active_get_user_message_waiting_note(egg_app.db, egg_app.current_thread) is not None
    messages = _messages(egg_app)
    command_msg = next(msg for msg in messages if msg.get("role") == "user" and msg.get("content") == "$ echo hi")
    assert command_msg.get("keep_user_turn") is True
    assert command_msg.get("tool_calls")


def test_ctrl_c_cancels_active_get_user_wait_with_keep_user_turn(egg_app):
    _start_get_user_wait(egg_app)
    egg_app.update_panels()
    assert egg_app.input_panel.title == "Message Input (get answer tool)"

    result = egg_app.handle_key("\x03")

    assert result is True
    assert egg_app.db.current_open(egg_app.current_thread) is None
    messages = _messages(egg_app)
    tool_msg = next(msg for msg in messages if msg.get("role") == "tool" and msg.get("tool_call_id") == "call-get-user-ui")
    assert "User interrupted" in tool_msg["content"]
    assert tool_msg["keep_user_turn"] is True
    assert not any(msg.get("role") == "user" and "User interrupted" in str(msg.get("content")) for msg in messages)
    assert ts.discover_runner_actionable(egg_app.db, egg_app.current_thread) is None

    egg_app.update_panels()
    assert egg_app.input_panel.title == "Message Input"


def test_normal_active_tool_ctrl_c_does_not_use_get_user_special_message(egg_app):
    _start_normal_tool(egg_app)

    result = egg_app.handle_key("\x03")

    assert result is True
    messages = _messages(egg_app)
    assert not any("get_user_message_while_preserving_llm_turn" in str(msg.get("content")) for msg in messages if msg.get("role") == "tool")
    assert not any(msg.get("role") == "tool" and msg.get("keep_user_turn") for msg in messages)
