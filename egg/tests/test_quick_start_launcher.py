from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


def test_egg_wrapper_preserves_argv_across_reload(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake-python"
    capture = tmp_path / "capture.json"
    fake_python.write_text(
        """#!/usr/bin/env python3
import json, os, sys
capture = os.environ["EGG_TEST_CAPTURE"]
assert "EGG_RELOAD_EXIT_CODE" in os.environ
if not os.environ.get("EGG_RELOAD_THREAD_ID"):
    with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
        handle.write("reload-thread\\n")
    raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
with open(capture, "w", encoding="utf-8") as handle:
    json.dump({"argv": sys.argv[3:], "thread": os.environ["EGG_RELOAD_THREAD_ID"]}, handle)
""",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    launch_args = ["Tell", "me a story", 'quote "inside"']
    env = {
        **os.environ,
        "EGG_PYTHON_BIN": str(fake_python),
        "EGG_TEST_CAPTURE": str(capture),
    }
    env.pop("EGG_RELOAD_THREAD_ID", None)

    result = subprocess.run(
        [str(repo_root / "egg" / "egg.sh"), *launch_args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "argv": launch_args,
        "thread": "reload-thread",
    }
