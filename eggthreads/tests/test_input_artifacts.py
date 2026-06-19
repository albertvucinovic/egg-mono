from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import eggthreads as ts
from eggthreads.input_artifacts import (
    INPUT_METADATA_SCHEMA_VERSION,
    InputArtifactAccessError,
    InputArtifactNotFoundError,
    resolve_input_bytes,
    resolve_input_metadata,
    save_input_bytes,
    thread_input_dir,
    validate_input_id,
)


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_input_artifact_layout_metadata_and_permissions(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    saved = save_input_bytes(
        tmp_path,
        tid,
        b"hello attachment",
        filename="/tmp/screenshots/example.png",
        mime_type="IMAGE/PNG",
        presentation="image",
        provenance={"kind": "local_path", "display_name": "example.png"},
        derived={"width": 2, "height": 1},
        provider_refs={"openai:user_data": {"file_id": "file-123"}},
    )

    assert re.fullmatch(r"[a-z0-9]{8}", saved.input_id)
    assert saved.record_dir == tmp_path / ".egg" / "egg_inputs" / tid / saved.input_id
    assert saved.metadata_path == saved.record_dir / "metadata.json"
    assert saved.blob_path == tmp_path / ".egg" / "egg_inputs" / "_blobs" / "sha256" / saved.metadata["sha256"][:2] / saved.metadata["sha256"]
    assert saved.blob_path.read_bytes() == b"hello attachment"

    metadata = json.loads(saved.metadata_path.read_text(encoding="utf-8"))
    assert metadata == saved.metadata
    assert metadata["schema_version"] == INPUT_METADATA_SCHEMA_VERSION
    assert metadata["input_id"] == saved.input_id
    assert metadata["owner_thread_id"] == tid
    assert metadata["filename"] == "example.png"
    assert metadata["mime_type"] == "image/png"
    assert metadata["presentation"] == "image"
    assert metadata["size_bytes"] == len(b"hello attachment")
    assert metadata["blob_relpath"] == f"../../_blobs/sha256/{metadata['sha256'][:2]}/{metadata['sha256']}"
    assert metadata["provenance"] == {"kind": "local_path", "display_name": "example.png"}
    assert metadata["derived"] == {"width": 2, "height": 1}
    assert metadata["provider_refs"] == {"openai:user_data": {"file_id": "file-123"}}

    if os.name != "nt":
        assert _mode(tmp_path / ".egg" / "egg_inputs") == 0o700
        assert _mode(saved.record_dir.parent) == 0o700
        assert _mode(saved.record_dir) == 0o700
        assert _mode(saved.metadata_path) == 0o600
        assert _mode(saved.blob_path) == 0o600


def test_input_artifact_ids_avoid_collisions(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    from eggthreads import input_artifacts

    ids = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(input_artifacts, "_random_input_id", lambda: next(ids))
    existing = tmp_path / ".egg" / "egg_inputs" / tid / "aaaaaaaa"
    existing.mkdir(parents=True)

    saved = save_input_bytes(tmp_path, tid, b"collision")

    assert saved.input_id == "bbbbbbbb"
    assert saved.record_dir.is_dir()


def test_input_artifacts_deduplicate_blobs_but_keep_thread_records(tmp_path):
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")

    root_saved = save_input_bytes(tmp_path, root, b"same bytes", filename="root.txt")
    child_saved = save_input_bytes(tmp_path, child, b"same bytes", filename="child.txt")

    assert root_saved.blob_path == child_saved.blob_path
    assert root_saved.record_dir != child_saved.record_dir
    assert root_saved.metadata["owner_thread_id"] == root
    assert child_saved.metadata["owner_thread_id"] == child
    assert root_saved.metadata["filename"] == "root.txt"
    assert child_saved.metadata["filename"] == "child.txt"


def test_resolve_input_own_thread_metadata_and_bytes(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"own input", filename="own.txt", mime_type="text/plain")

    metadata = resolve_input_metadata(tmp_path, db, tid, saved.input_id)
    resolved_metadata, data = resolve_input_bytes(tmp_path, db, tid, saved.input_id)

    assert metadata == saved.metadata
    assert resolved_metadata == saved.metadata
    assert data == b"own input"


def test_resolve_input_ancestor_can_read_descendant_with_explicit_selector(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_input_bytes(tmp_path, child, b"child input")

    metadata, data = resolve_input_bytes(tmp_path, db, parent, saved.input_id, descendant_thread_id=child)

    assert metadata["owner_thread_id"] == child
    assert data == b"child input"


def test_resolve_input_ancestor_cannot_read_descendant_without_selector(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_input_bytes(tmp_path, child, b"child input")

    with pytest.raises(InputArtifactNotFoundError):
        resolve_input_bytes(tmp_path, db, parent, saved.input_id)


def test_resolve_input_descendant_denied_ancestor(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_input_bytes(tmp_path, parent, b"parent input")

    with pytest.raises(InputArtifactAccessError, match="access denied"):
        resolve_input_bytes(tmp_path, db, child, saved.input_id, descendant_thread_id=parent)


def test_resolve_input_sibling_denied(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    sibling = ts.create_child_thread(db, parent, name="sibling")
    saved = save_input_bytes(tmp_path, sibling, b"sibling input")

    with pytest.raises(InputArtifactAccessError, match="access denied"):
        resolve_input_bytes(tmp_path, db, child, saved.input_id, descendant_thread_id=sibling)


@pytest.mark.parametrize("bad_input_id", ["../abcde", "abc/defg", "ABCDEF12", "........", "abcd", "abcdefghi", ""])
def test_input_id_validation_rejects_pathlike_or_unsafe_ids(tmp_path, bad_input_id):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    with pytest.raises(ValueError):
        validate_input_id(bad_input_id)
    with pytest.raises(ValueError):
        resolve_input_metadata(tmp_path, db, tid, bad_input_id)

    assert thread_input_dir(tmp_path, tid).resolve().is_relative_to((tmp_path / ".egg" / "egg_inputs").resolve())


def test_knowing_sha256_does_not_authorize_blob_read(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_input_bytes(tmp_path, parent, b"secret bytes")

    assert saved.blob_path.is_file()
    known_sha = saved.metadata["sha256"]
    assert saved.blob_path.name == known_sha

    with pytest.raises(ValueError):
        resolve_input_bytes(tmp_path, db, child, known_sha)
    with pytest.raises(InputArtifactNotFoundError):
        resolve_input_bytes(tmp_path, db, child, saved.input_id)


def test_metadata_blob_relpath_tampering_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_input_bytes(tmp_path, tid, b"safe bytes")
    metadata = dict(saved.metadata)
    metadata["blob_relpath"] = "../../../../etc/passwd"
    saved.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(Exception, match="blob_relpath"):
        resolve_input_bytes(tmp_path, db, tid, saved.input_id)
