from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_get_or_create_runtime_thread_creates_child_and_config(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    runtime = ts.get_or_create_runtime_thread(db, parent, language="python", name="default", reason="test")

    assert runtime in ts.list_children_ids(db, parent)
    row = db.get_thread(runtime)
    assert row is not None
    assert row.name == "@runtime:python"

    cfg = ts.find_runtime_thread(db, parent, language="python", name="default")
    assert cfg is not None
    assert cfg.runtime_thread_id == runtime
    assert cfg.parent_thread_id == parent
    assert cfg.language == "python"
    assert cfg.name == "default"

    tools_cfg = ts.get_thread_tools_config(db, runtime)
    assert tools_cfg.llm_tools_enabled is True

    # Runtime marker exists on the runtime thread itself.
    marker = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='runtime.thread'",
        (runtime,),
    ).fetchone()
    assert marker is not None
    payload = json.loads(marker[0])
    assert payload["parent_thread_id"] == parent
    assert payload["language"] == "python"


def test_get_or_create_runtime_thread_reuses_existing(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")

    first = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")
    second = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")

    assert first == second
    assert ts.list_children_ids(db, parent).count(first) == 1


def test_runtime_thread_label_includes_non_default_name():
    assert ts.runtime_thread_label(language="python", name="default") == "@runtime:python"
    assert ts.runtime_thread_label(language="bash", name="analysis") == "@runtime:bash:analysis"


def test_find_runtime_thread_ignores_stale_deleted_runtime(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")

    ts.delete_thread(db, runtime)
    assert ts.find_runtime_thread(db, parent, language="python", name="default") is None

    replacement = ts.get_or_create_runtime_thread(db, parent, language="python", name="default")
    assert replacement != runtime
    assert replacement in ts.list_children_ids(db, parent)
