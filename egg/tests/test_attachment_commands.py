from __future__ import annotations

import json
from pathlib import Path

from eggthreads.input_artifacts import resolve_input_bytes
from eggthreads.provider_output_artifacts import save_provider_output_bytes

from egg.attachments import staged_attachments_for_thread


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


def test_attach_image_rejected_for_explicit_text_only_model(egg_app, tmp_path, monkeypatch):
    source = tmp_path / "pixel.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")

    class Registry:
        def get_effective_model_config(self, _model_key):
            return {"model_name": "text-only", "input_modalities": ["text"]}

    class LLM:
        current_model_key = "Text Only"
        registry = Registry()

    egg_app.llm_client = LLM()
    monkeypatch.setattr(egg_app, "current_model_for_thread", lambda _tid: "Text Only")

    egg_app.handle_command(f"/attach {source}")

    assert egg_app._staged_attachment_count_for_current_thread() == 0
    assert any("/attach failed" in msg and "not supporting image attachments" in msg for msg in egg_app._system_log)


def test_input_panel_title_shows_staged_attachment_status(egg_app, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("hello", encoding="utf-8")
    egg_app.handle_command(f"/attach {source}")

    egg_app._update_get_user_message_input_mode()
    assert "1 attachment staged" in egg_app.input_panel.title


def test_attach_output_promotes_provider_artifact_and_stages_attachment(egg_app):
    source = save_provider_output_bytes(
        Path.cwd(),
        egg_app.current_thread,
        b"\x89PNG\r\n\x1a\ngenerated-image",
        filename="generated.png",
        mime_type="image/png",
        presentation="image",
        provenance={"kind": "openai_image_generation", "request_id": "req-123"},
        provider_refs={"openai": {"response_id": "resp-123"}},
    )

    egg_app.handle_command(f"/attachOutput {source.artifact_id}")

    staged = staged_attachments_for_thread(egg_app, egg_app.current_thread)
    assert len(staged) == 1
    part = staged[0]
    assert part["type"] == "attachment"
    assert part["owner_thread_id"] == egg_app.current_thread
    assert part["filename"] == "generated.png"
    assert part["mime_type"] == "image/png"
    assert part["presentation"] == "image"
    assert part["sha256"] == source.metadata["sha256"]

    promoted_metadata, promoted_bytes = resolve_input_bytes(Path.cwd(), egg_app.db, egg_app.current_thread, part["input_id"])
    assert promoted_bytes == b"\x89PNG\r\n\x1a\ngenerated-image"
    assert promoted_metadata["provenance"]["kind"] == "provider_output_promotion"
    assert promoted_metadata["provenance"]["source_artifact_id"] == source.artifact_id
    assert promoted_metadata["provenance"]["source_owner_thread_id"] == egg_app.current_thread
    assert promoted_metadata["provenance"]["source_provenance"] == {
        "kind": "openai_image_generation",
        "request_id": "req-123",
    }
    assert promoted_metadata["provider_refs"] == {
        "source_provider_output": {
            "artifact_id": source.artifact_id,
            "owner_thread_id": egg_app.current_thread,
            "sha256": source.metadata["sha256"],
        },
        "source_provider_refs": {"openai": {"response_id": "resp-123"}},
    }
    assert any(
        f"Promoted provider output {source.artifact_id} to input {part['input_id']}" in msg
        and "[Attachment: image generated.png image/png" in msg
        for msg in egg_app._system_log
    )


def test_submit_with_promoted_provider_output_attachment_includes_part_and_clears(egg_app):
    source = save_provider_output_bytes(
        Path.cwd(),
        egg_app.current_thread,
        b"generated text bytes",
        filename="generated.txt",
        mime_type="text/plain",
        presentation="file",
    )

    egg_app.handle_command(f"/attachOutput {source.artifact_id}")
    staged = list(staged_attachments_for_thread(egg_app, egg_app.current_thread))
    assert len(staged) == 1

    assert egg_app.on_submit("reuse this output") is True

    messages = _snapshot_messages(egg_app)
    user_messages = [m for m in messages if m.get("role") == "user"]
    content = user_messages[-1]["content"]
    assert egg_app._staged_attachment_count_for_current_thread() == 0
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "reuse this output"}
    assert content[1] == staged[0]
    assert content[1]["filename"] == "generated.txt"
    assert content[1]["mime_type"] == "text/plain"


def test_attach_output_failures_do_not_stage_anything(egg_app):
    assert egg_app._staged_attachment_count_for_current_thread() == 0

    for command in ("/attachOutput", "/attachOutput ../bad1", "/attachOutput abc12345"):
        egg_app.handle_command(command)
        assert egg_app._staged_attachment_count_for_current_thread() == 0

    failure_logs = [msg for msg in egg_app._system_log if "/attachOutput failed" in msg]
    assert len(failure_logs) >= 3
    assert any("Usage: /attachOutput <artifact_id>" in msg for msg in failure_logs)
    assert any("artifact_id must be" in msg for msg in failure_logs)
    assert any("not found" in msg.lower() for msg in failure_logs)
