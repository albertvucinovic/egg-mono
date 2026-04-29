from __future__ import annotations

import json
from pathlib import Path

import eggthreads as ts


def _make_db(tmp_path: Path) -> ts.ThreadsDB:
    db = ts.ThreadsDB(tmp_path / "threads.sqlite")
    db.init_schema()
    return db


def test_session_config_defaults_disabled(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    cfg = ts.get_thread_session_config(db, tid)
    assert cfg.enabled is False
    assert cfg.provider == "docker"
    assert cfg.session_id is None


def test_enable_thread_session_appends_config_and_stable_id(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")

    sid = ts.enable_thread_session(db, tid, image="custom-image", share_with_children_default=True)
    cfg = ts.get_thread_session_config(db, tid)

    assert cfg.enabled is True
    assert cfg.session_id == sid
    assert sid.startswith("sess_")
    assert cfg.image == "custom-image"
    assert cfg.share_with_children_default is True
    assert cfg.owner_thread_id == tid

    sid2 = ts.enable_thread_session(db, tid, image="custom-image")
    assert sid2 == sid


def test_session_config_inherits_to_runtime_child(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    sid = ts.enable_thread_session(db, parent, share="private")
    runtime = ts.get_or_create_runtime_thread(db, parent, language="python")

    cfg = ts.get_thread_session_config(db, runtime)
    assert cfg.enabled is True
    assert cfg.session_id == sid
    assert cfg.source == f"event:{parent}"


def test_child_can_share_specific_parent_session(tmp_path):
    db = _make_db(tmp_path)
    parent = ts.create_root_thread(db, name="parent")
    parent_sid = ts.enable_thread_session(db, parent)
    child = ts.create_child_thread(db, parent, name="child")

    ts.set_thread_session_config(
        db,
        child,
        enabled=True,
        share="session",
        session_id=parent_sid,
        owner_thread_id=parent,
        reason="test-share",
    )

    cfg = ts.get_thread_session_config(db, child)
    assert cfg.enabled is True
    assert cfg.share == "session"
    assert cfg.session_id == parent_sid
    assert cfg.owner_thread_id == parent


def test_session_lifecycle_event(tmp_path):
    db = _make_db(tmp_path)
    tid = ts.create_root_thread(db, name="root")
    sid = ts.enable_thread_session(db, tid)

    ts.append_session_lifecycle_event(
        db,
        tid,
        action="started",
        session_id=sid,
        payload={"container_name": "egg-rlm-test"},
    )

    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='session.lifecycle' ORDER BY event_seq DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["action"] == "started"
    assert payload["session_id"] == sid
    assert payload["container_name"] == "egg-rlm-test"
