from __future__ import annotations

import json


def _snapshot_messages(app):
    from eggthreads import create_snapshot

    create_snapshot(app.db, app.current_thread)
    snap = json.loads(app.db.get_thread(app.current_thread).snapshot_json)
    return snap["messages"]


def test_attach_command_stages_lists_and_clear_attachments(egg_app, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")

    egg_app.handle_command(f"/attach {source}")

    assert any("Attached note.txt" in msg for msg in egg_app._system_log)
    assert egg_app._staged_attachment_count_for_current_thread() == 1

    egg_app.handle_command("/attachments")
    assert any("Staged attachments:" in msg and "note.txt" in msg for msg in egg_app._system_log)

    egg_app.handle_command("/clearAttachments")
    assert egg_app._staged_attachment_count_for_current_thread() == 0
    assert any("Cleared 1 staged attachment" in msg for msg in egg_app._system_log)


def test_submit_with_staged_attachment_appends_content_parts_and_clears(egg_app, tmp_path):
    source = tmp_path / "pixel.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")

    egg_app.handle_command(f"/attach {source}")
    assert egg_app.on_submit("please inspect") is True

    messages = _snapshot_messages(egg_app)
    user_messages = [m for m in messages if m.get("role") == "user"]
    content = user_messages[-1]["content"]

    assert egg_app._staged_attachment_count_for_current_thread() == 0
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "please inspect"}
    assert content[1]["type"] == "attachment"
    assert content[1]["filename"] == "pixel.png"
    assert content[1]["mime_type"] == "image/png"
    assert content[1]["presentation"] == "image"


def test_submit_without_attachments_preserves_string_message_behavior(egg_app):
    assert egg_app.on_submit("plain text") is True

    messages = _snapshot_messages(egg_app)
    user_messages = [m for m in messages if m.get("role") == "user"]

    assert user_messages[-1]["content"] == "plain text"


def test_attachment_only_submission_is_allowed_from_input_handler(egg_app, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")
    egg_app.handle_command(f"/attach {source}")

    assert egg_app.on_submit("") is True

    content = [m for m in _snapshot_messages(egg_app) if m.get("role") == "user"][-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "attachment"
    assert content[0]["filename"] == "note.txt"


def test_attach_denied_by_effective_sandbox_policy_does_not_stage(egg_app, tmp_path):
    from eggthreads import set_thread_sandbox_config

    source = tmp_path / "secret.txt"
    source.write_text("hidden", encoding="utf-8")
    set_thread_sandbox_config(
        egg_app.db,
        egg_app.current_thread,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"denyRead": [str(source)], "allowWrite": ["."], "denyWrite": []}},
        reason="test",
    )

    egg_app.handle_command(f"/attach {source}")

    assert egg_app._staged_attachment_count_for_current_thread() == 0
    assert any("/attach failed" in msg and "denyRead" in msg for msg in egg_app._system_log)


def test_input_panel_title_shows_staged_attachment_status(egg_app, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")
    egg_app.handle_command(f"/attach {source}")

    egg_app._update_get_user_message_input_mode()
    assert "1 attachment staged" in egg_app.input_panel.title
