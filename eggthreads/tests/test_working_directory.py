import os
import pytest
import shutil
from pathlib import Path
import eggthreads.db
import eggthreads.sandbox
from eggthreads.db import ThreadsDB
from eggthreads.api import (
    create_root_thread, 
    create_child_thread, 
    set_thread_working_directory, 
    get_thread_working_directory,
)
from eggthreads.tools import create_default_tools

@pytest.fixture(autouse=True)
def disable_sandbox(monkeypatch):
    monkeypatch.setattr(eggthreads.sandbox, "sandbox_available", lambda: False)
    monkeypatch.setattr(eggthreads.sandbox, "sandbox_enabled", lambda: False)

@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "threads.sqlite"
    monkeypatch.setattr(eggthreads.db, "SQLITE_PATH", db_path)
    original_init = ThreadsDB.__init__
    def patched_init(self, db_path_arg=None):
        if db_path_arg is None:
            db_path_arg = db_path
        original_init(self, db_path_arg)
    monkeypatch.setattr(ThreadsDB, "__init__", patched_init)
    db_instance = ThreadsDB(db_path)
    db_instance.init_schema()
    return db_instance

def test_working_dir_inheritance(db):
    root_tid = create_root_thread(db, "root")
    cwd = Path.cwd().resolve()
    test_dir_name = "test_inheritance"
    task_dir = cwd / test_dir_name
    task_dir.mkdir(exist_ok=True)
    try:
        set_thread_working_directory(db, root_tid, test_dir_name)
        assert get_thread_working_directory(db, root_tid) == task_dir
        child_tid = create_child_thread(db, root_tid, "child")
        assert get_thread_working_directory(db, child_tid) == task_dir
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)

def test_isolation_behavior(db):
    root_tid = create_root_thread(db, "root")
    cwd = Path.cwd().resolve()
    parent_dir = cwd / "iso_parent"
    parent_dir.mkdir(exist_ok=True)
    try:
        set_thread_working_directory(db, root_tid, "iso_parent")
        child_tid = create_child_thread(db, root_tid, "child")
        tools = create_default_tools()
        tools.execute("bash", {"script": "echo 'shared' > shared.txt"}, thread_id=child_tid)
        assert (parent_dir / "shared.txt").exists()
        child_dir = parent_dir / "iso_child"
        child_dir.mkdir(exist_ok=True)
        set_thread_working_directory(db, child_tid, "iso_parent/iso_child")
        res = tools.execute("bash", {"script": "pwd"}, thread_id=child_tid)
        assert str(child_dir) in res
        # Sandboxing is disabled via fixture, so we expect unsandboxed behavior
        tools.execute("bash", {"script": "echo 'escape' > ../escape.txt"}, thread_id=child_tid)
        assert (parent_dir / "escape.txt").exists()
    finally:
        shutil.rmtree(parent_dir, ignore_errors=True)

def test_safety_constraints(db):
    root_tid = create_root_thread(db, "root")
    # Use a path guaranteed to be outside CWD (sibling of CWD under its parent)
    cwd = Path.cwd().resolve()
    outside = str(cwd.parent / ("_not_" + cwd.name + "_safety_test"))
    with pytest.raises(ValueError, match="must be a subdirectory"):
        set_thread_working_directory(db, root_tid, outside)
    egg_dir = Path.cwd() / ".egg"
    egg_dir.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="cannot be inside the .egg system folder"):
        set_thread_working_directory(db, root_tid, ".egg")

def test_sandbox_config_generation(db):
    from eggthreads.sandbox import wrap_argv_for_sandbox_with_settings
    import eggthreads.sandbox
    argv = ["ls"]
    settings = {"filesystem": {"allowWrite": ["."]}, "provider": "srt"}
    working_dir = Path.cwd() / "a" / "b"
    
    # We want to test this specific function, so we mock its internal check
    orig_avail = eggthreads.sandbox.sandbox_available
    eggthreads.sandbox.sandbox_available = lambda: True
    
    # Mock srt provider availability
    orig_srt_avail = eggthreads.sandbox._PROVIDERS["srt"].is_available
    eggthreads.sandbox._PROVIDERS["srt"].is_available = lambda: True
    
    orig_eff = eggthreads.sandbox._effective_config_path_from_settings
    captured = []
    def mock_eff(s):
        captured.append(s)
        return Path("/tmp/fake.json")
    eggthreads.sandbox._effective_config_path_from_settings = mock_eff
    try:
        wrap_argv_for_sandbox_with_settings(argv, enabled=True, settings=settings, working_dir=working_dir)
        assert "a/b" in captured[0]["filesystem"]["allowWrite"]
    finally:
        eggthreads.sandbox.sandbox_available = orig_avail
        eggthreads.sandbox._PROVIDERS["srt"].is_available = orig_srt_avail
        eggthreads.sandbox._effective_config_path_from_settings = orig_eff


def test_delete_and_reattach_auto_recreate(db):
    """Verify that if the CWD is deleted, it is automatically recreated as empty upon next use."""
    root_tid = create_root_thread(db, "root")
    cwd = Path.cwd().resolve()
    test_dir = cwd / "auto_recreate_test"
    
    set_thread_working_directory(db, root_tid, "auto_recreate_test")
    (test_dir / "old_file.txt").write_text("content")
    
    # Physically delete
    shutil.rmtree(test_dir)
    assert not test_dir.exists()
    
    tools = create_default_tools()
    # This should now succeed because the tool calls ensure_thread_working_directory
    tools.execute("bash", {"script": "echo 'new' > new_file.txt"}, thread_id=root_tid)
    
    assert test_dir.exists()
    assert (test_dir / "new_file.txt").exists()
    assert not (test_dir / "old_file.txt").exists() # It's empty now (recreated)
    
    shutil.rmtree(test_dir, ignore_errors=True)
