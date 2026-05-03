from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import eggthreads as ts
import eggthreads.sandbox as sandbox
import eggthreads.session as session
from eggthreads.runner import stash_tool_output_and_build_preview
from eggthreads.tools import create_default_tools


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def _docker_mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]


def test_output_paths_are_nested_by_thread_ancestry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    preview, saved = stash_tool_output_and_build_preview(db, grandchild, "tc", "x" * 20, max_chars=1)

    expected_dir = tmp_path / ".egg_outputs" / root / child / grandchild
    assert saved
    assert Path(saved).parent == expected_dir
    assert f".egg_outputs/{root}/{child}/{grandchild}/" in preview


def test_docker_sandbox_mounts_only_thread_output_subtree_rw(tmp_path, monkeypatch):
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
    child_rel = f".egg_outputs/{root}/{child}"
    assert any(spec.endswith(":/workspace/.egg_outputs:ro") for spec in mounts)
    assert f"{tmp_path / child_rel}:/workspace/{child_rel}" in mounts
    assert not any(f".egg_outputs/{root}:/workspace/.egg_outputs/{root}" == spec for spec in mounts)
    assert not any(f".egg_outputs/{root}/{sibling}" in spec and not spec.endswith(":ro") for spec in mounts)


def test_docker_sandbox_parent_gets_output_subtree_rw(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    root = ts.create_root_thread(db, name="root")
    child = ts.create_child_thread(db, root, name="child")
    grandchild = ts.create_child_thread(db, child, name="grandchild")

    settings = {
        "provider": "docker",
        "workspace": "/workspace",
        "network": "none",
        "_egg_thread_context": {"thread_id": child, "db_path": str(db.path)},
    }

    provider = sandbox._PROVIDERS["docker"]
    with patch.object(provider, "is_available", return_value=True):
        argv = provider.wrap_argv(["bash", "-lc", "true"], settings, working_dir=tmp_path)

    mounts = _docker_mount_specs(argv)
    child_rel = f".egg_outputs/{root}/{child}"
    grandchild_rel = f".egg_outputs/{root}/{child}/{grandchild}"
    assert f"{tmp_path / child_rel}:/workspace/{child_rel}" in mounts
    # No separate mount is needed for the grandchild: it lives inside the
    # child's mounted subtree, so the parent can read descendant outputs.
    assert not any(f":/workspace/{grandchild_rel}" in spec for spec in mounts)


def test_docker_repl_mounts_runtime_output_subtree_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.create_child_thread(db, parent, name="@runtime:python")

    args = session._docker_repl_thread_output_mount_args(
        db=db,
        mount_dir=tmp_path,
        workspace="/workspace",
        runtime_thread_id=runtime,
    )

    nested = f".egg_outputs/{parent}/{runtime}"
    assert args == ["-v", f"{tmp_path / nested}:/workspace/{nested}:ro"]


def test_docker_repl_masks_outputs_root_but_leaves_runtime_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.create_child_thread(db, parent, name="@runtime:python")

    mask_dir = session._prepare_outputs_mask_dir(db, "sess_test", runtime)

    assert mask_dir.exists()
    assert (mask_dir / parent / runtime).exists()


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
