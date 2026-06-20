from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import eggthreads as ts
from eggthreads.attachment_lowering import (
    AttachmentLoweringContext,
    AttachmentLoweringError,
    lower_messages_for_provider,
)
from eggthreads.content_parts import content_to_plain_text
from eggthreads.input_artifacts import save_input_bytes
from eggthreads.runner import ThreadRunner


class _DummyRunner(ThreadRunner):  # type: ignore[misc]
    def __init__(self, db, thread_id, llm):
        self.db = db
        self.thread_id = thread_id
        self.llm = llm

    def _get_tool_call_id_normalization_strategy(self, model_key=None):
        return None


class _DummyRegistry:
    def __init__(self, model_config):
        self._model_config = model_config

    def get_model_config(self, key):
        return self._model_config


class _DummyLLM:
    def __init__(self, model_config, model_key="Vision"):
        self.current_model_key = model_key
        self.registry = _DummyRegistry(model_config)


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _image_part(saved, *, owner_thread_id: str, presentation="image", mime_type="image/png"):
    return {
        "type": "attachment",
        "input_id": saved.input_id,
        "owner_thread_id": owner_thread_id,
        "presentation": presentation,
        "mime_type": mime_type,
        "filename": "pixel.png",
        "size_bytes": saved.metadata["size_bytes"],
        "sha256": saved.metadata["sha256"],
        "options": {"detail": "low"},
    }


def _content(saved, thread_id):
    return [
        {"type": "text", "text": "look"},
        _image_part(saved, owner_thread_id=thread_id),
    ]


def _ctx(tmp_path, db, tid, model_config, api_type="chat_completions"):
    return AttachmentLoweringContext(
        workspace=tmp_path,
        db=db,
        calling_thread_id=tid,
        model_key="Vision",
        model_config=model_config,
        provider_api_type=api_type,
    )


def test_openai_chat_image_attachment_lowers_to_image_url_data_url(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes", filename="pixel.png", mime_type="image/png", presentation="image")
    model_config = {
        "input_modalities": ["text", "image"],
        "attachment_capabilities": {"images": {"mime_types": ["image/png"]}},
    }

    lowered = lower_messages_for_provider(
        [{"msg_id": "u1", "role": "user", "content": _content(saved, tid)}],
        _ctx(tmp_path, db, tid, model_config, "chat_completions"),
        current_msg_id="u1",
    )

    content = lowered[0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    assert content[1]["image_url"]["detail"] == "low"


def test_openai_responses_image_attachment_lowers_to_input_image_data_url(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes", filename="pixel.png", mime_type="image/png", presentation="image")
    model_config = {"input_modalities": ["text", "image"], "attachment_capabilities": {"images": True}}

    lowered = lower_messages_for_provider(
        [{"msg_id": "u1", "role": "user", "content": _content(saved, tid)}],
        _ctx(tmp_path, db, tid, model_config, "responses"),
        current_msg_id="u1",
    )

    content = lowered[0]["content"]
    assert content[0] == {"type": "input_text", "text": "look"}
    assert content[1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii"),
        "detail": "low",
    }


def test_text_only_content_array_lowers_to_plain_string(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    lowered = lower_messages_for_provider(
        [{"msg_id": "u1", "role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}],
        _ctx(tmp_path, db, tid, {"model_name": "text-only"}),
        current_msg_id="u1",
    )

    assert lowered[0]["content"] == "hello\nworld"


def test_current_attachment_without_capability_fails_fast(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes")

    with pytest.raises(AttachmentLoweringError, match="cannot be sent"):
        lower_messages_for_provider(
            [{"msg_id": "u1", "role": "user", "content": _content(saved, tid)}],
            _ctx(tmp_path, db, tid, {"model_name": "text-only"}),
            current_msg_id="u1",
        )


def test_current_attachment_metadata_mismatch_fails_fast(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes", filename="pixel.png", mime_type="image/png", presentation="image")
    model_config = {"input_modalities": ["text", "image"], "attachment_capabilities": {"images": True}}
    content = _content(saved, tid)
    content[1] = {**content[1], "mime_type": "image/jpeg"}

    with pytest.raises(AttachmentLoweringError, match="metadata mismatch"):
        lower_messages_for_provider(
            [{"msg_id": "u1", "role": "user", "content": content}],
            _ctx(tmp_path, db, tid, model_config),
            current_msg_id="u1",
        )


def test_historical_attachment_metadata_mismatch_becomes_placeholder(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes", filename="pixel.png", mime_type="image/png", presentation="image")
    model_config = {"input_modalities": ["text", "image"], "attachment_capabilities": {"images": True}}
    content = _content(saved, tid)
    content[1] = {**content[1], "mime_type": "image/jpeg"}

    lowered = lower_messages_for_provider(
        [
            {"msg_id": "old", "role": "user", "content": content},
            {"msg_id": "new", "role": "user", "content": "next"},
        ],
        _ctx(tmp_path, db, tid, model_config),
        current_msg_id="new",
    )

    assert lowered[0]["content"] == "look\n[Attachment: image pixel.png image/jpeg 9 B sha256:" + saved.metadata["sha256"][:8] + "]"
    assert lowered[1]["content"] == "next"


def test_historical_unsupported_attachment_becomes_placeholder(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes")

    lowered = lower_messages_for_provider(
        [
            {"msg_id": "old", "role": "user", "content": _content(saved, tid)},
            {"msg_id": "new", "role": "user", "content": "next"},
        ],
        _ctx(tmp_path, db, tid, {"model_name": "text-only"}),
        current_msg_id="new",
    )

    assert lowered[0]["content"] == "look\n[Attachment: image pixel.png image/png 9 B sha256:" + saved.metadata["sha256"][:8] + "]"
    assert lowered[1]["content"] == "next"


def test_runner_provider_sanitization_keeps_lowered_image_parts(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    model_config = {"input_modalities": ["text", "image"], "attachment_capabilities": {"images": True}}
    runner = _DummyRunner(db, tid, _DummyLLM(model_config))
    monkeypatch.setattr(
        "eggthreads.runner.get_thread_tools_config",
        lambda _db, _thread_id: MagicMock(allow_raw_tool_output=True),
    )

    messages = [{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}]}]
    sanitized = runner._sanitize_messages_for_api(messages)

    assert sanitized[0]["content"] == messages[0]["content"]


def test_no_raw_or_base64_bytes_in_stored_events(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"png-bytes")
    content = _content(saved, tid)
    msg_id = ts.append_message(db, tid, "user", content)

    row = db.conn.execute("SELECT payload_json FROM events WHERE msg_id=?", (msg_id,)).fetchone()
    payload_text = row[0]

    assert "png-bytes" not in payload_text
    assert base64.b64encode(b"png-bytes").decode("ascii") not in payload_text
    payload = json.loads(payload_text)
    assert payload["content"] == content
    assert content_to_plain_text(payload["content"]).startswith("look")
