from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts
import pytest
from eggthreads.editor_sources import (
    EDITOR_DRAFT_MAX_BYTES,
    read_editor_draft_file,
    resolve_editor_source,
)


def _db(tmp_path: Path):
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_editor_source_resolves_show_records_and_pretty_tool_json(tmp_path: Path) -> None:
    db = _db(tmp_path)
    tid = ts.create_root_thread(db, "editor")
    msg_id = ts.append_message(db, tid, "user", "raw prompt")
    call_id = "call-editor-pretty-json"
    ts.append_message(
        db,
        tid,
        "assistant",
        "",
        extra={"tool_calls": [{"id": call_id, "type": "function", "function": {"name": "bash", "arguments": '{"script":"echo hi"}'}}]},
    )
    ts.create_snapshot(db, tid)

    message = resolve_editor_source(db, tid, msg_id[-8:], tmp_path)
    declaration = resolve_editor_source(db, tid, call_id[-10:], tmp_path)

    assert message.mode == "record" and message.draft == "raw prompt"
    assert declaration.mode == "record"
    parsed = json.loads(declaration.draft)
    assert parsed["id"] == call_id
    assert parsed["function"]["name"] == "bash"


def test_editor_source_path_modes_and_literal_compatibility(tmp_path: Path) -> None:
    db = _db(tmp_path)
    tid = ts.create_root_thread(db, "editor")
    source = tmp_path / "my file.md"
    source.write_text("file body", encoding="utf-8")

    in_place = resolve_editor_source(db, tid, '"my file.md"', tmp_path)
    copied = resolve_editor_source(db, tid, '@"my file.md"', tmp_path)
    created = resolve_editor_source(db, tid, "./new.md", tmp_path)
    quoted_created = resolve_editor_source(db, tid, '"new file.md"', tmp_path)
    escaped_created = resolve_editor_source(db, tid, r"escaped\ file.md", tmp_path)
    windowsish_literal = resolve_editor_source(db, tid, r"new\file", tmp_path)
    literal = resolve_editor_source(db, tid, "newfile", tmp_path)
    forced = resolve_editor_source(db, tid, "-- README.md", tmp_path)

    assert in_place.mode == "file" and in_place.path == source
    assert copied.mode == "file_draft" and copied.path == source
    assert created.mode == "file" and created.path == tmp_path / "new.md"
    assert quoted_created.mode == "file" and quoted_created.path == tmp_path / "new file.md"
    assert escaped_created.mode == "file" and escaped_created.path == tmp_path / "escaped file.md"
    assert windowsish_literal.mode == "draft" and windowsish_literal.draft == r"new\file"
    assert literal.mode == "draft" and literal.draft == "newfile"
    assert forced.mode == "draft" and forced.draft == "README.md"


def test_editor_source_ambiguous_show_hint_does_not_fall_through(tmp_path: Path) -> None:
    db = _db(tmp_path)
    tid = ts.create_root_thread(db, "editor")
    for prefix in ("alpha", "beta"):
        db.append_event(
            event_id=f"event-{prefix}",
            thread_id=tid,
            type_="msg.create",
            msg_id=f"{prefix}-shared-tail",
            payload={"role": "assistant", "content": prefix},
        )
    ts.create_snapshot(db, tid)

    source = resolve_editor_source(db, tid, "shared-tail", tmp_path)

    assert source.mode == "ambiguous"
    assert source.resolution is not None and source.resolution.total_matches == 2


def test_editor_draft_file_reader_rejects_binary_and_oversized_sources(tmp_path: Path) -> None:
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"before\x00after")
    with pytest.raises(ValueError, match="binary"):
        read_editor_draft_file(binary)

    oversized = tmp_path / "oversized.txt"
    with oversized.open("wb") as handle:
        handle.truncate(EDITOR_DRAFT_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="too large"):
        read_editor_draft_file(oversized)


def test_editor_source_rejects_directories_and_missing_parents(tmp_path: Path) -> None:
    db = _db(tmp_path)
    thread = ts.create_root_thread(db, "editor")
    with pytest.raises(ValueError, match="not a regular file"):
        resolve_editor_source(db, thread, str(tmp_path), tmp_path)
    assert resolve_editor_source(db, thread, "missing", tmp_path).mode == "draft"
    with pytest.raises(ValueError, match="parent directory"):
        resolve_editor_source(db, thread, "missing/new.txt", tmp_path)
