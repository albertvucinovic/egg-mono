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


@pytest.mark.skipif(
    os.environ.get("EGG_SKIP_SRT_TESTS") == "1",
    reason="Sandbox-runtime tests disabled via EGG_SKIP_SRT_TESTS=1",
)
def test_python_tool_is_sandboxed_and_children_inherit_latest_config(tmp_path, monkeypatch):
    eggthreads = _import_eggthreads(monkeypatch, tmp_path)

    # Require srt to be present for this integration-like test.
    if not eggthreads.sandbox.provider_available("srt"):
        pytest.skip("srt CLI not available")

    # Enable sandboxing by default for the process.
    eggthreads.set_sandbox_globally_enabled(True)

    db = eggthreads.ThreadsDB()
    db.init_schema()

    root = eggthreads.create_root_thread(db, name="root")
    eggthreads.append_message(db, root, "system", "test")

    # Create a restrictive config (default) in .egg/sandbox.
    sandbox_dir = Path.cwd() / ".egg" / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    restrictive = {
        "provider": "srt",
        "filesystem": {"denyRead": [], "allowWrite": ["."], "denyWrite": []},
        "network": {"allowedDomains": ["example.com"], "deniedDomains": []},
    }
    (sandbox_dir / "restrict.json").write_text(__import__("json").dumps(restrictive), encoding="utf-8")

    # Apply restrictive config to the root thread. Children without an
    # explicit sandbox.config event should inherit it.
    eggthreads.set_thread_sandbox_config(db, root, enabled=True, config_name="restrict.json", reason="test")

    # Spawn child after setting restrictive config: it should inherit the
    # currently effective config from the nearest ancestor.
    child = eggthreads.create_child_thread(db, root, name="child")

    tools = eggthreads.create_default_tools()

    # Attempt to write outside CWD should fail under the restrictive config.
    out = tools.execute(
        "python",
        {"script": "from pathlib import Path; Path('../blocked.txt').write_text('nope')"},
        thread_id=child,
    )
    assert "Read-only file system" in out or "Permission" in out or "ERROR" in out or "Failed to create bridge sockets" in out
    if "Failed to create bridge sockets" in out:
        pytest.skip("srt bridge socket creation failed")

    # Now switch to a permissive config and ensure a newly spawned child uses it.
    permissive = {
        "provider": "srt",
        "filesystem": {"denyRead": [], "allowWrite": [".", ".."], "denyWrite": []},
        "network": {"allowedDomains": ["example.com"], "deniedDomains": []},
    }
    (sandbox_dir / "allow_parent.json").write_text(__import__("json").dumps(permissive), encoding="utf-8")

    eggthreads.set_thread_sandbox_config(db, root, enabled=True, config_name="allow_parent.json", reason="switch")

    child2 = eggthreads.create_child_thread(db, root, name="child2")

    out2 = tools.execute(
        "python",
        {"script": "from pathlib import Path; Path('../ok.txt').write_text('ok')"},
        thread_id=child2,
    )
    assert Path("../ok.txt").resolve().is_file(), out2
