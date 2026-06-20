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
    promote_provider_output_to_input,
    resolve_provider_output_bytes,
    resolve_provider_output_metadata,
    save_provider_output_bytes,
    thread_provider_output_dir,
    validate_provider_output_artifact_id,
)
from eggthreads.input_artifacts import resolve_input_bytes


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


def test_promote_provider_output_to_input_own_thread_creates_attachment_record(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    data = b"\x89PNG\r\n\x1a\nprovider-image"
    source = save_provider_output_bytes(
        tmp_path,
        tid,
        data,
        filename="generated.png",
        mime_type="image/png",
        presentation="image",
        provenance={"kind": "openai_image_generation", "request_id": "req-123"},
        derived={"width": 1024, "height": 1024},
        provider_refs={"openai": {"response_id": "resp-123"}},
    )

    promoted, attachment_part = promote_provider_output_to_input(tmp_path, db, tid, source.artifact_id)
    source_after = resolve_provider_output_metadata(tmp_path, db, tid, source.artifact_id)
    promoted_metadata, promoted_bytes = resolve_input_bytes(tmp_path, db, tid, promoted.input_id)

    assert source_after == source.metadata
    assert promoted_bytes == data
    assert promoted_metadata == promoted.metadata
    assert promoted.metadata["owner_thread_id"] == tid
    assert promoted.metadata["filename"] == "generated.png"
    assert promoted.metadata["mime_type"] == "image/png"
    assert promoted.metadata["presentation"] == "image"
    assert promoted.metadata["size_bytes"] == source.metadata["size_bytes"]
    assert promoted.metadata["sha256"] == source.metadata["sha256"]
    assert promoted.metadata["derived"] == {"width": 1024, "height": 1024}
    assert promoted.metadata["provenance"] == {
        "kind": "provider_output_promotion",
        "source_artifact_id": source.artifact_id,
        "source_owner_thread_id": tid,
        "source_sha256": source.metadata["sha256"],
        "source_filename": "generated.png",
        "source_mime_type": "image/png",
        "source_presentation": "image",
        "source_provenance": {"kind": "openai_image_generation", "request_id": "req-123"},
        "source_provider_refs": {"openai": {"response_id": "resp-123"}},
    }
    assert promoted.metadata["provider_refs"] == {
        "source_provider_output": {
            "artifact_id": source.artifact_id,
            "owner_thread_id": tid,
            "sha256": source.metadata["sha256"],
        },
        "source_provider_refs": {"openai": {"response_id": "resp-123"}},
    }
    assert attachment_part == {
        "type": "attachment",
        "input_id": promoted.input_id,
        "owner_thread_id": tid,
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "generated.png",
        "size_bytes": len(data),
        "sha256": source.metadata["sha256"],
        "options": {},
    }
    assert promoted.record_dir.parent == tmp_path / ".egg" / "egg_inputs" / tid
    assert promoted.blob_path != source.blob_path
    assert promoted.blob_path.is_relative_to(tmp_path / ".egg" / "egg_inputs" / "_blobs")


def test_promote_provider_output_parent_can_promote_child_with_explicit_selector(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    source = save_provider_output_bytes(tmp_path, child, b"child generated", filename="child.txt", mime_type="text/plain", presentation="file")

    promoted, attachment_part = promote_provider_output_to_input(tmp_path, db, parent, source.artifact_id, descendant_thread_id=child)
    promoted_metadata, data = resolve_input_bytes(tmp_path, db, parent, promoted.input_id)

    assert data == b"child generated"
    assert promoted_metadata["owner_thread_id"] == parent
    assert promoted_metadata["sha256"] == source.metadata["sha256"]
    assert promoted_metadata["provenance"]["source_artifact_id"] == source.artifact_id
    assert promoted_metadata["provenance"]["source_owner_thread_id"] == child
    assert attachment_part["owner_thread_id"] == parent
    assert attachment_part["input_id"] == promoted.input_id


def test_promote_provider_output_parent_without_selector_gets_not_found_and_no_input(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    source = save_provider_output_bytes(tmp_path, child, b"child generated")
    before = list((tmp_path / ".egg" / "egg_inputs" / parent).glob("*")) if (tmp_path / ".egg" / "egg_inputs" / parent).exists() else []

    with pytest.raises(ProviderOutputArtifactNotFoundError):
        promote_provider_output_to_input(tmp_path, db, parent, source.artifact_id)

    after_dir = tmp_path / ".egg" / "egg_inputs" / parent
    after = list(after_dir.glob("*")) if after_dir.exists() else []
    assert after == before


def test_promote_provider_output_descendant_denied_ancestor_and_sibling_denied(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    sibling = ts.create_child_thread(db, parent, name="sibling")
    parent_source = save_provider_output_bytes(tmp_path, parent, b"parent generated")
    sibling_source = save_provider_output_bytes(tmp_path, sibling, b"sibling generated")

    with pytest.raises(ProviderOutputArtifactAccessError, match="access denied"):
        promote_provider_output_to_input(tmp_path, db, child, parent_source.artifact_id, descendant_thread_id=parent)
    with pytest.raises(ProviderOutputArtifactAccessError, match="access denied"):
        promote_provider_output_to_input(tmp_path, db, child, sibling_source.artifact_id, descendant_thread_id=sibling)

    child_inputs = tmp_path / ".egg" / "egg_inputs" / child
    assert not child_inputs.exists()


def test_promote_provider_output_rejects_sha_or_pathlike_id_without_authorization(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    source = save_provider_output_bytes(tmp_path, parent, b"secret generated")

    with pytest.raises(ValueError):
        promote_provider_output_to_input(tmp_path, db, child, source.metadata["sha256"])
    with pytest.raises(ValueError):
        promote_provider_output_to_input(tmp_path, db, child, "../bad1")
    with pytest.raises(ValueError):
        promote_provider_output_to_input(tmp_path, db, child, str(source.blob_path))
    with pytest.raises(ProviderOutputArtifactNotFoundError):
        promote_provider_output_to_input(tmp_path, db, child, source.artifact_id)

    child_inputs = tmp_path / ".egg" / "egg_inputs" / child
    assert not child_inputs.exists()


def test_generate_openai_image_artifacts_stores_b64_outputs_as_provider_artifacts(tmp_path, monkeypatch):
    import base64

    from eggthreads.image_generation import generate_openai_image_artifacts

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.posts = []

        def post(self, url, *, headers, json, timeout):
            self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse(
                {
                    "id": "img-resp-456",
                    "created": 456,
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"generated-one").decode("ascii"),
                            "revised_prompt": "Refined prompt",
                        },
                        {"b64_json": base64.b64encode(b"generated-two").decode("ascii")},
                    ],
                }
            )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path = tmp_path / "models.json"
    all_models_path = tmp_path / "all-models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-images": {
                        "api_base": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "api_type": "openai_images",
                        "model_kind": "image_generation",
                        "models": {
                            "Image Backend": {
                                "model_name": "gpt-image-1",
                                "task_capabilities": ["image_generation"],
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    all_models_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    session = FakeSession()

    result = generate_openai_image_artifacts(
        tmp_path,
        tid,
        "Draw an egg",
        model_key="Image Backend",
        models_path=models_path,
        all_models_path=all_models_path,
        options={"n": 2, "size": "1024x1024", "output_format": "png"},
        timeout=11,
        session=session,
    )

    assert session.posts[0]["url"] == "https://api.openai.com/v1/images/generations"
    assert session.posts[0]["headers"]["Authorization"] == "Bearer test-key"
    assert session.posts[0]["json"] == {
        "model": "gpt-image-1",
        "prompt": "Draw an egg",
        "n": 2,
        "size": "1024x1024",
        "output_format": "png",
    }
    assert result.model_key == "Image Backend"
    assert result.provider_name == "openai-images"
    assert result.response_metadata == {"id": "img-resp-456", "created": 456}
    assert len(result.artifacts) == 2
    assert len(result.content_parts) == 2

    first = result.artifacts[0]
    first_metadata, first_bytes = resolve_provider_output_bytes(tmp_path, db, tid, first.artifact_id)
    assert first_bytes == b"generated-one"
    assert first_metadata == first.metadata
    assert first.metadata["filename"] == "generated-1.png"
    assert first.metadata["mime_type"] == "image/png"
    assert first.metadata["presentation"] == "image"
    assert first.metadata["owner_thread_id"] == tid
    assert first.metadata["provenance"] == {
        "kind": "openai_image_generation",
        "provider": "openai-images",
        "model_key": "Image Backend",
        "model": "gpt-image-1",
        "prompt": "Draw an egg",
        "output_index": 0,
        "revised_prompt": "Refined prompt",
        "response_id": "img-resp-456",
    }
    assert first.metadata["derived"] == {
        "width": 1024,
        "height": 1024,
        "size": "1024x1024",
        "output_format": "png",
        "revised_prompt": "Refined prompt",
    }
    assert first.metadata["provider_refs"] == {
        "openai": {
            "api_type": "openai_images",
            "model": "gpt-image-1",
            "model_key": "Image Backend",
            "output_index": 0,
            "source": "b64_json",
            "response_id": "img-resp-456",
            "response_created": 456,
            "request_options": {"n": 2, "size": "1024x1024", "output_format": "png"},
        }
    }
    assert first.content_part == {
        "type": "artifact",
        "artifact_id": first.artifact_id,
        "owner_thread_id": tid,
        "presentation": "image",
        "mime_type": "image/png",
        "filename": "generated-1.png",
        "size_bytes": len(b"generated-one"),
        "sha256": first.metadata["sha256"],
        "provenance": first.metadata["provenance"],
        "options": {},
    }
    assert "generated-one" not in json.dumps(first.content_part)
    assert "b64_json" not in json.dumps(first.content_part)

    second_metadata, second_bytes = resolve_provider_output_bytes(tmp_path, db, tid, result.artifacts[1].artifact_id)
    assert second_bytes == b"generated-two"
    assert second_metadata["filename"] == "generated-2.png"
    assert second_metadata["provenance"]["output_index"] == 1
