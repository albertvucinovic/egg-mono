from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


GET_USER_TOOL_NAME = "get_user_message_while_preserving_llm_turn"


def _snapshot_messages(app):
    row = app.db.get_thread(app.current_thread)
    assert row and row.snapshot_json
    return json.loads(row.snapshot_json)["messages"]


def _append_event(db, tid: str, type_: str, payload: dict, *, msg_id: str | None = None, invoke_id: str | None = None) -> None:
    db.append_event(
        event_id=f"{type_}-{db.max_event_seq(tid) + 1}",
        thread_id=tid,
        type_=type_,
        payload=payload,
        msg_id=msg_id,
        invoke_id=invoke_id,
    )


def _start_get_user_wait(egg_app, *, note: str = "What title should I use?") -> str:
    tid = egg_app.current_thread
    invoke_id = "invoke-edit-answer-get-user"
    tool_call_id = "call-edit-answer-get-user"
    assert egg_app.db.try_open_stream(tid, invoke_id, "2999-01-01 00:00:00", owner="test", purpose="tool")
    ts.append_message(
        egg_app.db,
        tid,
        "assistant",
        "",
        extra={
            "tool_calls": [
                {
                    "id": tool_call_id,
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
        {"tool_call_id": tool_call_id},
        invoke_id=invoke_id,
    )
    note_msg_id = ts.append_message(
        egg_app.db,
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
    ts.create_snapshot(egg_app.db, tid)
    return note_msg_id


def test_quote_markdown_blockquote_preserves_blank_lines_and_source_markdown():
    from egg.edit_answer import quote_markdown_blockquote

    text = "# Title\n\n```python\nprint('hi')\n```\n> old quote"

    assert quote_markdown_blockquote(text) == (
        "> # Title\n"
        ">\n"
        "> ```python\n"
        "> print('hi')\n"
        "> ```\n"
        "> > old quote"
    )


def test_select_assistant_message_defaults_to_latest_textual_assistant():
    from egg.edit_answer import select_assistant_message

    messages = [
        {"role": "assistant", "content": "first", "msg_id": "aaa111"},
        {"role": "user", "content": "middle", "msg_id": "user"},
        {"role": "assistant", "content": "", "msg_id": "empty"},
        {"role": "assistant", "content": "second", "msg_id": "bbb222"},
    ]

    message, text = select_assistant_message(messages)

    assert message["msg_id"] == "bbb222"
    assert text == "second"


def test_select_assistant_message_accepts_unique_msg_id_suffix():
    from egg.edit_answer import select_assistant_message

    messages = [
        {"role": "assistant", "content": "first", "msg_id": "01ABCDEF"},
        {"role": "assistant", "content": "second", "msg_id": "01UVWXYZ"},
    ]

    message, text = select_assistant_message(messages, "WXYZ")

    assert message["msg_id"] == "01UVWXYZ"
    assert text == "second"


def test_select_assistant_message_can_prefer_assistant_notes():
    from egg.edit_answer import select_assistant_message

    messages = [
        {"role": "assistant", "content": "final", "msg_id": "final"},
        {"role": "assistant", "content": "note", "msg_id": "note", "answer_user_preserve_turn": True},
        {"role": "assistant", "content": "later final", "msg_id": "later"},
    ]

    message, text = select_assistant_message(messages, prefer_notes=True)

    assert message["msg_id"] == "note"
    assert text == "note"


def test_edit_answer_command_opens_quoted_raw_markdown_and_loads_edited_input(egg_app, monkeypatch):
    from eggthreads import append_message, create_snapshot

    append_message(egg_app.db, egg_app.current_thread, "user", "question")
    msg_id = append_message(
        egg_app.db,
        egg_app.current_thread,
        "assistant",
        "# Heading\n\n```python\nprint('hi')\n```",
    )
    create_snapshot(egg_app.db, egg_app.current_thread)

    seen_initial = {}

    async def fake_external(argv):
        path = Path(argv[-1])
        seen_initial["text"] = path.read_text(encoding="utf-8")
        path.write_text(path.read_text(encoding="utf-8") + "\nUser note\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(egg_app, "run_external_terminal_command", fake_external)

    egg_app.handle_command("/editAnswer")

    assert seen_initial["text"] == "> # Heading\n>\n> ```python\n> print('hi')\n> ```\n"
    assert egg_app.input_panel.editor.editor.get_text() == (
        "> # Heading\n>\n> ```python\n> print('hi')\n> ```\n\nUser note"
    )
    assert any(msg_id[-8:] in entry for entry in egg_app._system_log)
    # The command only prepares the user input. It must not append/send it.
    assert len(_snapshot_messages(egg_app)) == 3


def test_editor_command_opens_empty_external_editor_for_input_prompt(egg_app, monkeypatch):
    seen_initial = {}

    async def fake_external(argv):
        path = Path(argv[-1])
        seen_initial["text"] = path.read_text(encoding="utf-8")
        path.write_text("Draft prompt from editor\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(egg_app, "run_external_terminal_command", fake_external)

    egg_app.handle_command("/editor")

    assert seen_initial["text"] == ""
    assert egg_app.input_panel.editor.editor.get_text() == "Draft prompt from editor"
    assert any("input message draft" in entry for entry in egg_app._system_log)


def test_edit_answer_command_falls_back_to_empty_editor_without_answer(egg_app, monkeypatch):
    seen_initial = {}

    async def fake_external(argv):
        path = Path(argv[-1])
        seen_initial["text"] = path.read_text(encoding="utf-8")
        path.write_text("Prompt when no assistant exists\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(egg_app, "run_external_terminal_command", fake_external)

    egg_app.handle_command("/editAnswer")

    assert seen_initial["text"] == ""
    assert egg_app.input_panel.editor.editor.get_text() == "Prompt when no assistant exists"
    assert any("input message draft" in entry for entry in egg_app._system_log)


def test_edit_answer_command_refuses_to_overwrite_existing_input(egg_app, monkeypatch):
    from eggthreads import append_message, create_snapshot

    append_message(egg_app.db, egg_app.current_thread, "assistant", "answer")
    create_snapshot(egg_app.db, egg_app.current_thread)
    egg_app.input_panel.editor.editor.set_text("draft")

    called = False

    async def fake_external(argv):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(egg_app, "run_external_terminal_command", fake_external)

    egg_app.handle_command("/editAnswer")

    assert called is False
    assert egg_app.input_panel.editor.editor.get_text() == "draft"
    assert any("input panel is not empty" in entry for entry in egg_app._system_log)


def test_edit_answer_in_get_answer_mode_edits_waiting_assistant_note(egg_app, monkeypatch):
    from eggthreads import append_message, create_snapshot

    append_message(egg_app.db, egg_app.current_thread, "assistant", "Older final answer")
    note_msg_id = _start_get_user_wait(egg_app, note="## Waiting note\n\nPlease edit me")
    # This should not normally happen while the tool is waiting, but it makes
    # the regression explicit: get-answer mode targets the waiting Assistant
    # Note, not merely the latest assistant-role message.
    append_message(egg_app.db, egg_app.current_thread, "assistant", "Later assistant text")
    create_snapshot(egg_app.db, egg_app.current_thread)

    seen_initial = {}

    async def fake_external(argv):
        path = Path(argv[-1])
        seen_initial["text"] = path.read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(egg_app, "run_external_terminal_command", fake_external)

    egg_app.handle_command("/editAnswer")

    assert seen_initial["text"] == "> ## Waiting note\n>\n> Please edit me\n"
    assert egg_app.input_panel.editor.editor.get_text() == "> ## Waiting note\n>\n> Please edit me"
    assert any(note_msg_id[-8:] in entry for entry in egg_app._system_log)


def test_edit_answer_command_is_registered_and_completable(egg_app):
    from egg.completion import get_autocomplete_items

    assert "editAnswer" in egg_app.command_registry.names()
    assert "editor" in egg_app.command_registry.names()

    items = get_autocomplete_items(
        "/edit",
        len("/edit"),
        egg_app.db,
        lambda: egg_app.current_thread,
        egg_app.llm_client,
        egg_app.command_registry,
    )

    assert any(item["display"] == "/editAnswer" for item in items)

    editor_items = get_autocomplete_items(
        "/edi",
        len("/edi"),
        egg_app.db,
        lambda: egg_app.current_thread,
        egg_app.llm_client,
        egg_app.command_registry,
    )
    assert any(item["display"] == "/editor" for item in editor_items)
