from __future__ import annotations

from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.attachment_staging import (
    build_message_content_with_attachments,
    infer_attachment_mime_and_presentation,
    save_local_attachment_for_thread,
)
from eggthreads.content_parts import content_to_plain_text
from eggthreads.input_artifacts import resolve_input_bytes
from eggthreads.sandbox import authorize_thread_path_read


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_infer_attachment_mime_and_presentation_is_conservative():
    assert infer_attachment_mime_and_presentation("pixel.png", b"\x89PNG\r\n\x1a\nbytes") == ("image/png", "image")
    assert infer_attachment_mime_and_presentation("named.png", b"not really png") == ("text/plain", "file")
    assert infer_attachment_mime_and_presentation("data.bin", b"\x00\x01raw") == ("application/octet-stream", "file")


def test_save_local_attachment_authorizes_and_saves_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    source = tmp_path / "pixel.png"
    data = b"\x89PNG\r\n\x1a\nbytes"
    source.write_bytes(data)

    saved, part = save_local_attachment_for_thread(db, tid, "pixel.png")
    metadata, resolved = resolve_input_bytes(tmp_path, db, tid, saved.input_id)

    assert resolved == data
    assert metadata["filename"] == "pixel.png"
    assert metadata["mime_type"] == "image/png"
    assert metadata["presentation"] == "image"
    assert metadata["provenance"] == {"kind": "local_path", "display_name": "pixel.png"}
    assert part["input_id"] == saved.input_id
    assert part["owner_thread_id"] == tid
    assert part["filename"] == "pixel.png"
    assert part["presentation"] == "image"


def test_authorize_thread_path_read_denies_docker_paths_outside_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    ts.set_thread_working_directory(db, tid, "work")
    inside = tmp_path / "work" / "allowed.txt"
    outside = tmp_path / "outside.txt"
    inside.write_text("allowed", encoding="utf-8")
    outside.write_text("blocked", encoding="utf-8")

    assert authorize_thread_path_read(db, tid, "allowed.txt") == inside.resolve()
    with pytest.raises(PermissionError, match="outside Docker sandbox mounts"):
        authorize_thread_path_read(db, tid, outside)


def test_authorize_thread_path_read_respects_deny_read_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    secret = tmp_path / "secrets" / "hidden.txt"
    secret.parent.mkdir()
    secret.write_text("nope", encoding="utf-8")
    ts.set_thread_sandbox_config(
        db,
        tid,
        enabled=True,
        provider="srt",
        settings={"provider": "srt", "filesystem": {"denyRead": ["secrets"], "allowWrite": ["."], "denyWrite": []}},
        reason="test",
    )

    with pytest.raises(PermissionError, match="denyRead"):
        authorize_thread_path_read(db, tid, secret)


def test_build_message_content_with_attachments_orders_text_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    _saved_a, part_a = save_local_attachment_for_thread(db, tid, "a.txt")
    _saved_b, part_b = save_local_attachment_for_thread(db, tid, "b.txt")

    assert build_message_content_with_attachments("plain", []) == "plain"
    content = build_message_content_with_attachments("see", [part_a, part_b])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "see"}
    assert [part["input_id"] for part in content[1:]] == [part_a["input_id"], part_b["input_id"]]
    assert content_to_plain_text(content).startswith("see\n[Attachment:")

    attachment_only = build_message_content_with_attachments("", [part_a])
    assert isinstance(attachment_only, list)
    assert attachment_only == [part_a]
