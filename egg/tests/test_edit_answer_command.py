from __future__ import annotations

import json
from pathlib import Path


def _snapshot_messages(app):
    row = app.db.get_thread(app.current_thread)
    assert row and row.snapshot_json
    return json.loads(row.snapshot_json)["messages"]


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


def test_edit_answer_command_is_registered_and_completable(egg_app):
    from egg.completion import get_autocomplete_items

    assert "editAnswer" in egg_app.command_registry.names()

    items = get_autocomplete_items(
        "/edit",
        len("/edit"),
        egg_app.db,
        lambda: egg_app.current_thread,
        egg_app.llm_client,
        egg_app.command_registry,
    )

    assert any(item["display"] == "/editAnswer" for item in items)
