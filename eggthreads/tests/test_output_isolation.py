from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import eggthreads as ts
import eggthreads.sandbox as sandbox
import eggthreads.session as session
from eggthreads.runner import LONG_OUTPUT_CHUNK_CHARS, LONG_OUTPUT_CHUNK_LINES, stash_tool_output_and_build_preview
from eggthreads.tools import create_default_tools


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _docker_mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]


def _artifact_id(saved: str) -> str:
    return Path(saved).name


def _read_artifact(db: ts.ThreadsDB, caller: str, artifact_id: str, chunk_number: int = 1, descendant_thread_id: str | None = None) -> str:
    args: dict[str, object] = {"artifact_id": artifact_id, "chunk_number": chunk_number}
    if descendant_thread_id is not None:
        args["descendant_thread_id"] = descendant_thread_id
    return create_default_tools().execute("read_long_tool_output", args, thread_id=caller, db=db)


def test_output_artifact_path_is_flat_by_thread_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    preview, saved = stash_tool_output_and_build_preview(db, grandchild, "tc", "x" * 20, max_chars=1)

    artifact_dir = Path(saved)
    assert artifact_dir.parent == tmp_path / ".egg" / "egg_outputs" / grandchild
    assert re.fullmatch(r"[a-z0-9]{8}", artifact_dir.name)
    assert (artifact_dir / "chunk-0001.txt").read_text() == "x" * 20
    assert ".egg_outputs" not in preview
    assert ".egg/egg_outputs" not in preview
    assert f"Artifact id: {artifact_dir.name}" in preview
    assert "read_long_tool_output(" in preview


def test_output_artifact_ids_avoid_collisions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    from eggthreads import output_paths

    ids = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(output_paths, "_random_artifact_id", lambda: next(ids))
    existing = tmp_path / ".egg" / "egg_outputs" / tid / "aaaaaaaa"
    existing.mkdir(parents=True)

    _preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", "x" * 20, max_chars=1)

    assert Path(saved).name == "bbbbbbbb"
    assert Path(saved).is_dir()


def test_output_artifact_chunks_and_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    output = "a" * (LONG_OUTPUT_CHUNK_CHARS + 5)

    preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", output, max_chars=10)

    artifact_dir = Path(saved)
    metadata = json.loads((artifact_dir / "metadata.json").read_text())
    assert metadata["artifact_id"] == artifact_dir.name
    assert metadata["chunk_count"] == 2
    assert metadata["capped"] is False
    assert metadata["stored_char_count"] == len(output)
    assert (artifact_dir / "chunk-0001.txt").read_text() == "a" * LONG_OUTPUT_CHUNK_CHARS
    assert (artifact_dir / "chunk-0002.txt").read_text() == "a" * 5
    assert f"Chunks: {metadata['chunk_count']}" in preview
    assert len((artifact_dir / "chunk-0001.txt").read_text()) < 100_000


def test_output_artifact_chunks_are_below_line_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    output = "\n".join(f"line {i}" for i in range(LONG_OUTPUT_CHUNK_LINES + 10))

    _preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", output, max_lines=1)

    artifact_dir = Path(saved)
    metadata = json.loads((artifact_dir / "metadata.json").read_text())
    assert metadata["chunk_size_lines"] == LONG_OUTPUT_CHUNK_LINES
    assert metadata["chunk_count"] == 2
    assert len((artifact_dir / "chunk-0001.txt").read_text().splitlines()) == LONG_OUTPUT_CHUNK_LINES
    assert len((artifact_dir / "chunk-0002.txt").read_text().splitlines()) == 10


def test_output_artifact_caps_stored_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("eggthreads.runner.MAX_STORED_TOOL_OUTPUT_CHARS", 50)
    monkeypatch.setattr("eggthreads.runner.LONG_OUTPUT_CHUNK_CHARS", 20)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", "z" * 80, max_chars=5)

    artifact_dir = Path(saved)
    metadata = json.loads((artifact_dir / "metadata.json").read_text())
    assert metadata["capped"] is True
    assert metadata["original_char_count"] == 80
    assert metadata["stored_char_count"] == 50
    assert metadata["chunk_count"] == 3
    assert "capped at 50 of 80 chars" in preview
    assert "".join((artifact_dir / f"chunk-{i:04d}.txt").read_text() for i in range(1, 4)) == "z" * 50


def test_read_long_tool_output_own_thread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", "own artifact", max_chars=1)

    result = _read_artifact(db, tid, _artifact_id(saved))

    assert f"artifact_id: {_artifact_id(saved)}" in result
    assert f"owner_thread_id: {tid}" in result
    assert "chunk_number: 1" in result
    assert "total_chunks: 1" in result
    assert "capped: False" in result
    assert result.endswith("own artifact")


def test_read_long_tool_output_ancestor_can_read_descendant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _preview, saved = stash_tool_output_and_build_preview(db, child, "tc", "child artifact", max_chars=1)

    result = _read_artifact(db, parent, _artifact_id(saved), descendant_thread_id=child)

    assert f"owner_thread_id: {child}" in result
    assert result.endswith("child artifact")


def test_read_long_tool_output_uses_db_workspace_outside_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    elsewhere = tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(workspace)
    db = ts.ThreadsDB(workspace / ".egg" / "threads.sqlite")
    db.init_schema()
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")
    _preview, saved = stash_tool_output_and_build_preview(
        db, grandchild, "tc", "deep descendant artifact", max_chars=1
    )
    assert Path(saved).parent == workspace / ".egg" / "egg_outputs" / grandchild

    monkeypatch.chdir(elsewhere)
    result = _read_artifact(
        db,
        root,
        _artifact_id(saved),
        descendant_thread_id=grandchild,
    )

    assert f"owner_thread_id: {grandchild}" in result
    assert result.endswith("deep descendant artifact")


def test_read_long_tool_output_descendant_denied_ancestor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    _preview, saved = stash_tool_output_and_build_preview(db, parent, "tc", "parent artifact", max_chars=1)

    result = _read_artifact(db, child, _artifact_id(saved), descendant_thread_id=parent)

    assert result.startswith("Error: access denied")


def test_read_long_tool_output_sibling_denied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    child = ts.create_child_thread(db, parent, name="child")
    sibling = ts.create_child_thread(db, parent, name="sibling")
    _preview, saved = stash_tool_output_and_build_preview(db, sibling, "tc", "sibling artifact", max_chars=1)

    result = _read_artifact(db, child, _artifact_id(saved), descendant_thread_id=sibling)

    assert result.startswith("Error: access denied")


def test_read_long_tool_output_missing_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    result = _read_artifact(db, tid, "missing1")

    assert result == "Error: artifact not found."


def test_read_long_tool_output_bad_chunk_number(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    _preview, saved = stash_tool_output_and_build_preview(db, tid, "tc", "one chunk", max_chars=1)

    result = _read_artifact(db, tid, _artifact_id(saved), chunk_number=2)

    assert result == "Error: bad chunk number: requested 2, but artifact has 1 chunks."


def test_docker_sandbox_masks_egg_without_output_mounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    sibling = ts.create_child_thread(db, root, name="sibling")
    stash_tool_output_and_build_preview(db, root, "tc_root", "root" * 100, max_chars=1)
    stash_tool_output_and_build_preview(db, sibling, "tc_sibling", "sibling" * 100, max_chars=1)

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
    tmpfs_mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--mount"]
    assert "type=tmpfs,dst=/workspace/.egg,readonly" in tmpfs_mounts
    # The project root already has the real Egg database. The sandbox must not
    # bind it into the container or add another host path for the mask.
    assert not any(str(tmp_path / ".egg") in spec for spec in mounts)
    assert not any(".egg_outputs" in spec for spec in mounts)
    assert not any(".egg/egg_outputs" in spec for spec in mounts)
def test_tool_context_passes_output_subtree_to_docker_sandbox(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = ts.ThreadsDB()
    db.init_schema()
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    ts.set_thread_sandbox_config(
        db,
        child,
        enabled=True,
        settings={"provider": "docker", "image": "python:3.12-slim", "network": "none"},
    )

    captured: dict[str, object] = {}

    def fake_wrap(argv, *, enabled, settings, working_dir=None, provider=None, container_name=None):
        captured["settings"] = settings
        captured["working_dir"] = working_dir
        return ["/bin/echo", "wrapped"]

    monkeypatch.setattr(sandbox, "wrap_argv_for_sandbox_with_settings", fake_wrap)

    create_default_tools().execute("bash", {"script": "echo hi"}, thread_id=child)

    ctx = captured["settings"]["_egg_thread_context"]
    assert ctx["thread_id"] == child
    assert ctx["db_path"] == str(db.path)
