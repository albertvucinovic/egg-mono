from __future__ import annotations

import json

import pytest

import eggthreads as ts
from eggthreads.content_parts import (
    ContentPartError,
    artifact_part_from_provider_output_metadata,
    content_has_artifacts,
    attachment_part_from_input_metadata,
    content_has_attachments,
    content_to_plain_text,
    extract_artifact_refs,
    extract_attachment_refs,
    format_attachment_placeholder,
    format_provider_artifact_placeholder,
    normalize_content_to_parts,
    validate_message_content,
)
from eggthreads.runner import ThreadRunner


SHA = "0123456789abcdef" * 4


def _attachment_part(**overrides):
    part = {
        "type": "attachment",
        "input_id": "a1b2c3d4",
        "owner_thread_id": "01KVOWNER",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "screenshot.png",
        "size_bytes": 186321,
        "sha256": SHA,
        "options": {"detail": "auto"},
    }
    part.update(overrides)
    return part


def _artifact_part(**overrides):
    part = {
        "type": "artifact",
        "artifact_id": "q7x9p2aa",
        "owner_thread_id": "01KVOWNER",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "generated.png",
        "size_bytes": 524288,
        "sha256": SHA,
        "provenance": {"kind": "openai_image_generation"},
        "options": {},
    }
    part.update(overrides)
    return part


def _content_parts():
    return [
        {"type": "text", "text": "What is wrong with this screenshot?"},
        _attachment_part(),
    ]


class _DummyRunner(ThreadRunner):  # type: ignore[misc]
    def __init__(self) -> None:
        self.db = None
        self.thread_id = "thread"
        self.llm = None

    def _get_tool_call_id_normalization_strategy(self, model_key=None):
        return None


def test_normalize_string_content_to_text_part_preserves_string_storage_shape():
    assert normalize_content_to_parts("hello") == [{"type": "text", "text": "hello"}]
    assert validate_message_content("hello") == "hello"
    assert content_to_plain_text("hello") == "hello"


def test_validate_content_array_preserves_attachment_fields_and_canonicalizes():
    content = _content_parts()

    normalized = validate_message_content(content)

    assert normalized[0] == {"type": "text", "text": "What is wrong with this screenshot?"}
    assert normalized[1] == {
        "type": "attachment",
        "input_id": "a1b2c3d4",
        "owner_thread_id": "01KVOWNER",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "screenshot.png",
        "size_bytes": 186321,
        "sha256": SHA,
        "options": {"detail": "auto"},
    }


def test_validate_content_array_preserves_provider_artifact_fields_and_canonicalizes():
    content = [
        {"type": "text", "text": "Generated output"},
        _artifact_part(presentation="IMAGE", mime_type="IMAGE/PNG"),
    ]

    normalized = validate_message_content(content)

    assert normalized[0] == {"type": "text", "text": "Generated output"}
    assert normalized[1] == {
        "type": "artifact",
        "artifact_id": "q7x9p2aa",
        "owner_thread_id": "01KVOWNER",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "generated.png",
        "size_bytes": 524288,
        "sha256": SHA,
        "provenance": {"kind": "openai_image_generation"},
        "options": {},
    }


def test_validate_content_array_rejects_invalid_shapes():
    invalid_cases = [
        [],
        [{"type": "text"}],
        [{"type": "text", "text": 123}],
        [{"type": "unknown", "text": "x"}],
        [_attachment_part(input_id="../bad")],
        [_attachment_part(filename="../secret.png")],
        [_attachment_part(size_bytes=-1)],
        [_attachment_part(sha256="not-a-sha")],
        [_attachment_part(options=[])],
        [_attachment_part(extra="nope")],
        [_artifact_part(artifact_id="../bad")],
        [_artifact_part(filename="../secret.png")],
        [_artifact_part(size_bytes=-1)],
        [_artifact_part(sha256="not-a-sha")],
        [_artifact_part(provenance=[])],
        [_artifact_part(options=[])],
        [_artifact_part(extra="nope")],
    ]

    for content in invalid_cases:
        with pytest.raises(ContentPartError):
            validate_message_content(content)


def test_attachment_placeholder_plain_text_and_ref_extraction():
    attachment = _attachment_part(size_bytes=186321)
    content = [{"type": "text", "text": "See this"}, attachment]

    placeholder = format_attachment_placeholder(attachment)
    plain = content_to_plain_text(content, validate=True)
    refs = extract_attachment_refs(content)

    assert placeholder == "[Attachment: image screenshot.png image/png 182 KB sha256:01234567]"
    assert plain == "See this\n[Attachment: image screenshot.png image/png 182 KB sha256:01234567]"
    assert refs == [validate_message_content([attachment])[0]]
    assert content_has_attachments(content) is True
    assert content_has_attachments("plain") is False


def test_provider_artifact_placeholder_plain_text_and_ref_extraction():
    artifact = _artifact_part(size_bytes=524288)
    content = [{"type": "text", "text": "Generated"}, artifact]

    placeholder = format_provider_artifact_placeholder(artifact)
    plain = content_to_plain_text(content, validate=True)
    refs = extract_artifact_refs(content)

    assert placeholder == "[Provider artifact: image generated.png image/png 512 KB sha256:01234567 artifact_id:q7x9p2aa]"
    assert plain == "Generated\n[Provider artifact: image generated.png image/png 512 KB sha256:01234567 artifact_id:q7x9p2aa]"
    assert refs == [validate_message_content([artifact])[0]]
    assert content_has_artifacts(content) is True
    assert content_has_artifacts("plain") is False
    assert content_has_attachments(content) is False


def test_attachment_part_from_input_metadata_uses_saved_record_shape():
    metadata = {
        "input_id": "a1b2c3d4",
        "owner_thread_id": "thread-1",
        "presentation": "IMAGE",
        "mime_type": "IMAGE/PNG",
        "filename": "pixel.png",
        "size_bytes": 3,
        "sha256": SHA,
        "provenance": {"kind": "local_path"},
    }

    part = attachment_part_from_input_metadata(metadata, options={"detail": "low"})

    assert part == {
        "type": "attachment",
        "input_id": "a1b2c3d4",
        "owner_thread_id": "thread-1",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "pixel.png",
        "size_bytes": 3,
        "sha256": SHA,
        "options": {"detail": "low"},
    }


def test_artifact_part_from_provider_output_metadata_uses_saved_record_shape():
    metadata = {
        "artifact_id": "q7x9p2aa",
        "owner_thread_id": "thread-1",
        "presentation": "IMAGE",
        "mime_type": "IMAGE/PNG",
        "filename": "generated.png",
        "size_bytes": 3,
        "sha256": SHA,
        "provenance": {},
    }

    part = artifact_part_from_provider_output_metadata(metadata, options={"display": "card"})

    assert part == {
        "type": "artifact",
        "artifact_id": "q7x9p2aa",
        "owner_thread_id": "thread-1",
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "generated.png",
        "size_bytes": 3,
        "sha256": SHA,
        "provenance": {},
        "options": {"display": "card"},
    }


def test_append_message_string_compatibility_and_array_snapshot_preservation(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    string_id = ts.append_message(db, tid, "user", "plain message")
    array_id = ts.append_message(db, tid, "user", _content_parts())
    snapshot = ts.create_snapshot(db, tid)

    by_id = {m["msg_id"]: m for m in snapshot["messages"]}
    assert by_id[string_id]["content"] == "plain message"
    assert by_id[array_id]["content"] == validate_message_content(_content_parts())
    assert by_id[array_id]["content"][1]["input_id"] == "a1b2c3d4"

    stored = json.loads(db.get_thread(tid).snapshot_json)
    assert stored["messages"][1]["content"] == validate_message_content(_content_parts())


def test_append_message_rejects_invalid_content_array(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")

    with pytest.raises(ContentPartError):
        ts.append_message(db, tid, "user", [{"type": "attachment", "input_id": "bad"}])


def test_snapshot_word_count_and_token_stats_use_plain_placeholders(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    ts.append_message(db, tid, "user", _content_parts())

    snapshot = ts.create_snapshot(db, tid)
    stats = snapshot["token_stats"]
    per_message = next(iter(stats["per_message"].values()))

    assert ts.word_count_from_snapshot(db, tid) > 0
    assert per_message["content_tokens"] > 0
    assert content_to_plain_text(snapshot["messages"][0]["content"]).startswith("What is wrong")


def test_snapshot_token_stats_and_repl_context_use_provider_artifact_placeholders(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    content = [{"type": "text", "text": "Generated image"}, _artifact_part()]
    msg_id = ts.append_message(db, tid, "assistant", content)

    snapshot = ts.create_snapshot(db, tid)
    stats = snapshot["token_stats"]
    per_message = next(iter(stats["per_message"].values()))
    context = ts.build_repl_thread_context(db, tid)

    assert ts.word_count_from_snapshot(db, tid) > 0
    assert per_message["content_tokens"] > 0
    assert content_to_plain_text(snapshot["messages"][0]["content"]).startswith("Generated image")
    assert "[Provider artifact: image generated.png" in context["messages_by_id"][msg_id]["content_text"]


def test_provider_sanitization_falls_back_to_plain_attachment_placeholders(monkeypatch):
    import eggthreads.runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "get_thread_tools_config",
        lambda _db, _thread_id: type("Cfg", (), {"allow_raw_tool_output": True})(),
    )
    runner = _DummyRunner()

    sanitized = runner._sanitize_messages_for_api([{"role": "user", "content": _content_parts()}])

    assert sanitized == [
        {
            "role": "user",
            "content": "What is wrong with this screenshot?\n[Attachment: image screenshot.png image/png 182 KB sha256:01234567]",
        }
    ]


def test_provider_sanitization_falls_back_to_plain_provider_artifact_placeholders(monkeypatch):
    import eggthreads.runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "get_thread_tools_config",
        lambda _db, _thread_id: type("Cfg", (), {"allow_raw_tool_output": True})(),
    )
    runner = _DummyRunner()

    sanitized = runner._sanitize_messages_for_api([{"role": "user", "content": [{"type": "text", "text": "Generated"}, _artifact_part()]}])

    assert sanitized == [
        {
            "role": "user",
            "content": "Generated\n[Provider artifact: image generated.png image/png 512 KB sha256:01234567 artifact_id:q7x9p2aa]",
        }
    ]


def test_build_repl_thread_context_includes_content_text_for_arrays(tmp_path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    tid = ts.create_root_thread(db, name="root")
    msg_id = ts.append_message(db, tid, "user", _content_parts())

    context = ts.build_repl_thread_context(db, tid)

    message = context["messages_by_id"][msg_id]
    assert message["content"] == validate_message_content(_content_parts())
    assert message["content_text"].startswith("What is wrong")
    assert "[Attachment: image screenshot.png" in message["content_text"]
