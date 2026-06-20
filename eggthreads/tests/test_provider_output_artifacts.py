from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import eggthreads as ts
import eggthreads.sandbox as sandbox
from eggthreads.provider_output_artifacts import (
    PROVIDER_OUTPUT_METADATA_SCHEMA_VERSION,
    ProviderOutputArtifactAccessError,
    ProviderOutputArtifactNotFoundError,
    provider_output_root_dir,
    resolve_provider_output_bytes,
    resolve_provider_output_metadata,
    save_provider_output_bytes,
    thread_provider_output_dir,
    validate_provider_output_artifact_id,
)


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _docker_mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]


def test_provider_output_layout_metadata_and_permissions(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    saved = save_provider_output_bytes(
        tmp_path,
        tid,
        b"\x89PNG\r\n\x1a\nprovider-image",
        filename="/tmp/generated/final.png",
        mime_type="IMAGE/PNG",
        presentation="image",
        provenance={"kind": "openai_image_generation", "provider": "openai", "request_id": "req-123"},
        derived={"width": 1024, "height": 1024},
        provider_refs={"openai": {"response_id": "resp-123", "output_index": 0}},
    )

    assert re.fullmatch(r"[a-z0-9]{8}", saved.artifact_id)
    assert saved.record_dir == tmp_path / ".egg" / "egg_provider_output" / tid / saved.artifact_id
    assert saved.metadata_path == saved.record_dir / "metadata.json"
    assert saved.blob_path == tmp_path / ".egg" / "egg_provider_output" / "_blobs" / "sha256" / saved.metadata["sha256"][:2] / saved.metadata["sha256"]
    assert saved.blob_path.read_bytes() == b"\x89PNG\r\n\x1a\nprovider-image"

    metadata = json.loads(saved.metadata_path.read_text(encoding="utf-8"))
    assert metadata == saved.metadata
    assert metadata["schema_version"] == PROVIDER_OUTPUT_METADATA_SCHEMA_VERSION
    assert metadata["artifact_id"] == saved.artifact_id
    assert metadata["owner_thread_id"] == tid
    assert metadata["filename"] == "final.png"
    assert metadata["mime_type"] == "image/png"
    assert metadata["presentation"] == "image"
    assert metadata["size_bytes"] == len(b"\x89PNG\r\n\x1a\nprovider-image")
    assert metadata["blob_relpath"] == f"../../_blobs/sha256/{metadata['sha256'][:2]}/{metadata['sha256']}"
    assert metadata["provenance"] == {"kind": "openai_image_generation", "provider": "openai", "request_id": "req-123"}
    assert metadata["derived"] == {"width": 1024, "height": 1024}
    assert metadata["provider_refs"] == {"openai": {"response_id": "resp-123", "output_index": 0}}
    assert provider_output_root_dir(tmp_path) == tmp_path / ".egg" / "egg_provider_output"

    if os.name != "nt":
        assert _mode(tmp_path / ".egg" / "egg_provider_output") == 0o700
        assert _mode(saved.record_dir.parent) == 0o700
        assert _mode(saved.record_dir) == 0o700
        assert _mode(saved.metadata_path) == 0o600
        assert _mode(saved.blob_path) == 0o600


def test_provider_output_artifact_ids_avoid_collisions(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    from eggthreads import provider_output_artifacts

    ids = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(provider_output_artifacts, "_random_provider_output_artifact_id", lambda: next(ids))
    existing = tmp_path / ".egg" / "egg_provider_output" / tid / "aaaaaaaa"
    existing.mkdir(parents=True)

    saved = save_provider_output_bytes(tmp_path, tid, b"collision")

    assert saved.artifact_id == "bbbbbbbb"
    assert saved.record_dir.is_dir()


def test_provider_output_artifacts_deduplicate_blobs_but_keep_thread_records(tmp_path):
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")

    root_saved = save_provider_output_bytes(tmp_path, root, b"same bytes", filename="root.png")
    child_saved = save_provider_output_bytes(tmp_path, child, b"same bytes", filename="child.png")

    assert root_saved.blob_path == child_saved.blob_path
    assert root_saved.record_dir != child_saved.record_dir
    assert root_saved.metadata["owner_thread_id"] == root
    assert child_saved.metadata["owner_thread_id"] == child
    assert root_saved.metadata["filename"] == "root.png"
    assert child_saved.metadata["filename"] == "child.png"


def test_resolve_provider_output_own_thread_metadata_and_bytes(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_provider_output_bytes(tmp_path, tid, b"own provider output", filename="own.png", mime_type="image/png", presentation="image")

    metadata = resolve_provider_output_metadata(tmp_path, db, tid, saved.artifact_id)
    resolved_metadata, data = resolve_provider_output_bytes(tmp_path, db, tid, saved.artifact_id)

    assert metadata == saved.metadata
    assert resolved_metadata == saved.metadata
    assert data == b"own provider output"


def test_resolve_provider_output_ancestor_can_read_descendant_with_explicit_selector(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_provider_output_bytes(tmp_path, child, b"child provider output")

    metadata, data = resolve_provider_output_bytes(tmp_path, db, parent, saved.artifact_id, descendant_thread_id=child)

    assert metadata["owner_thread_id"] == child
    assert data == b"child provider output"


def test_resolve_provider_output_ancestor_cannot_read_descendant_without_selector(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_provider_output_bytes(tmp_path, child, b"child provider output")

    with pytest.raises(ProviderOutputArtifactNotFoundError):
        resolve_provider_output_bytes(tmp_path, db, parent, saved.artifact_id)


def test_resolve_provider_output_descendant_denied_ancestor(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_provider_output_bytes(tmp_path, parent, b"parent provider output")

    with pytest.raises(ProviderOutputArtifactAccessError, match="access denied"):
        resolve_provider_output_bytes(tmp_path, db, child, saved.artifact_id, descendant_thread_id=parent)


def test_resolve_provider_output_sibling_denied(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    sibling = ts.create_child_thread(db, parent, name="sibling")
    saved = save_provider_output_bytes(tmp_path, sibling, b"sibling provider output")

    with pytest.raises(ProviderOutputArtifactAccessError, match="access denied"):
        resolve_provider_output_bytes(tmp_path, db, child, saved.artifact_id, descendant_thread_id=sibling)


@pytest.mark.parametrize("bad_artifact_id", ["../abcde", "abc/defg", "ABCDEF12", "........", "abcd", "abcdefghi", ""])
def test_provider_output_artifact_id_validation_rejects_pathlike_or_unsafe_ids(tmp_path, bad_artifact_id):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    with pytest.raises(ValueError):
        validate_provider_output_artifact_id(bad_artifact_id)
    with pytest.raises(ValueError):
        resolve_provider_output_metadata(tmp_path, db, tid, bad_artifact_id)

    assert thread_provider_output_dir(tmp_path, tid).resolve().is_relative_to((tmp_path / ".egg" / "egg_provider_output").resolve())


def test_knowing_sha256_does_not_authorize_provider_output_blob_read(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    saved = save_provider_output_bytes(tmp_path, parent, b"secret provider bytes")

    assert saved.blob_path.is_file()
    known_sha = saved.metadata["sha256"]
    assert saved.blob_path.name == known_sha

    with pytest.raises(ValueError):
        resolve_provider_output_bytes(tmp_path, db, child, known_sha)
    with pytest.raises(ProviderOutputArtifactNotFoundError):
        resolve_provider_output_bytes(tmp_path, db, child, saved.artifact_id)


def test_provider_output_metadata_blob_relpath_tampering_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    saved = save_provider_output_bytes(tmp_path, tid, b"safe provider bytes")
    metadata = dict(saved.metadata)
    metadata["blob_relpath"] = "../../../../etc/passwd"
    saved.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(Exception, match="blob_relpath"):
        resolve_provider_output_bytes(tmp_path, db, tid, saved.artifact_id)


def test_docker_sandbox_does_not_mount_provider_output_namespace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    save_provider_output_bytes(tmp_path, root, b"root provider output")

    settings = {
        "provider": "docker",
        "workspace": "/workspace",
        "network": "none",
        "filesystem": {"allowWrite": ["."], "denyRead": [], "denyWrite": []},
        "_egg_thread_context": {"thread_id": child, "db_path": str(db.path)},
    }

    provider = sandbox._PROVIDERS["docker"]
    with patch.object(provider, "is_available", return_value=True):
        argv = provider.wrap_argv(["bash", "-lc", "true"], settings, working_dir=tmp_path)

    mounts = _docker_mount_specs(argv)
    assert not any("egg_provider_output" in spec for spec in mounts)
