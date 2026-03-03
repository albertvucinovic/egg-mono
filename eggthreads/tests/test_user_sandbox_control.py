from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _import_eggthreads(monkeypatch, tmp_path: Path):
    """Import eggthreads from the monorepo checkout, isolated to tmp_path."""
    monkeypatch.chdir(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import eggthreads  # noqa: F401
    return sys.modules["eggthreads"]


def test_user_sandbox_control_default_enabled(tmp_path, monkeypatch):
    eggthreads = _import_eggthreads(monkeypatch, tmp_path)
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    # By default, user sandbox control should be enabled
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True


def test_user_sandbox_control_disable(tmp_path, monkeypatch):
    eggthreads = _import_eggthreads(monkeypatch, tmp_path)
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    # Disable user sandbox control
    eggthreads.disable_user_sandbox_control(db, root, reason="test")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is False
    # Enable again
    eggthreads.enable_user_sandbox_control(db, root, reason="test2")
    assert eggthreads.is_user_sandbox_control_enabled(db, root) is True


def test_user_sandbox_control_events(tmp_path, monkeypatch):
    eggthreads = _import_eggthreads(monkeypatch, tmp_path)
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    # Disable
    eggthreads.disable_user_sandbox_control(db, root, reason="test")
    # Verify event stored as sandbox.config with user_control_enabled=False
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='sandbox.config' ORDER BY event_seq DESC LIMIT 1",
        (root,),
    )
    row = cur.fetchone()
    assert row is not None
    import json
    payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    assert payload.get('user_control_enabled') == False
    assert payload.get('reason') == 'test'
    # Enable
    eggthreads.enable_user_sandbox_control(db, root, reason="test2")
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='sandbox.config' ORDER BY event_seq DESC LIMIT 1",
        (root,),
    )
    row = cur.fetchone()
    payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    assert payload.get('user_control_enabled') == True
    assert payload.get('reason') == 'test2'


def test_user_sandbox_control_inheritance(tmp_path, monkeypatch):
    eggthreads = _import_eggthreads(monkeypatch, tmp_path)
    db = eggthreads.ThreadsDB()
    db.init_schema()
    root = eggthreads.create_root_thread(db, name="root")
    child = eggthreads.create_child_thread(db, root, name="child")
    # Disable on parent
    eggthreads.disable_user_sandbox_control(db, root, reason="parent")
    # Child should inherit the sandbox.config event, thus also disabled
    assert eggthreads.is_user_sandbox_control_enabled(db, child) is False
    # Enable parent
    eggthreads.enable_user_sandbox_control(db, root, reason="parent_enable")
    assert eggthreads.is_user_sandbox_control_enabled(db, child) is True
