from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path


RELOAD_EXIT_CODE = 75


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _launcher_env(tmp_path: Path, fake_python: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "EGG_PYTHON_BIN": str(fake_python),
        **extra,
    }
    # A fresh launcher owns these values; ambient state from a running Egg must
    # not make a new shell invocation look like an already-reloaded child.
    env.pop("EGG_RELOAD_THREAD_ID", None)
    env.pop("EGG_RELOAD_STATE_FILE", None)
    env.pop("EGG_RELOAD_EXIT_CODE", None)
    return env


def _egg_wrapper() -> Path:
    return Path(__file__).resolve().parents[2] / "egg" / "egg.sh"


def test_egg_wrapper_preserves_argv_cwd_and_state_file_across_reload(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "fake-python",
        """#!/usr/bin/env python3
import json, os, sys
capture = os.environ["EGG_TEST_CAPTURE"]
state_file = os.environ["EGG_RELOAD_STATE_FILE"]
assert os.path.exists(state_file)
if not os.environ.get("EGG_RELOAD_THREAD_ID"):
    with open(state_file, "w", encoding="utf-8") as handle:
        handle.write("reload-thread\\n")
    raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
with open(capture, "w", encoding="utf-8") as handle:
    json.dump({
        "argv": sys.argv[3:],
        "thread": os.environ["EGG_RELOAD_THREAD_ID"],
        "cwd": os.getcwd(),
        "state_file": state_file,
    }, handle)
""",
    )
    capture = tmp_path / "capture.json"
    launch_args = ["Tell", "me a story", 'quote "inside"', "line\nbreak", ""]

    result = subprocess.run(
        [str(_egg_wrapper()), *launch_args],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed == {
        "argv": launch_args,
        "thread": "reload-thread",
        "cwd": str(tmp_path),
        "state_file": observed["state_file"],
    }
    assert not Path(observed["state_file"]).exists()


def test_egg_wrapper_preserves_direct_restart_thread_but_owns_new_state_file(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "capture-env",
        """#!/usr/bin/env python3
import json, os
with open(os.environ["EGG_TEST_CAPTURE"], "w", encoding="utf-8") as handle:
    json.dump({
        "thread": os.environ.get("EGG_RELOAD_THREAD_ID"),
        "state_file": os.environ["EGG_RELOAD_STATE_FILE"],
    }, handle)
""",
    )
    capture = tmp_path / "capture.json"
    inherited_state = tmp_path / "outer-state"
    inherited_state.write_text("outer-thread\n", encoding="utf-8")
    env = _launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture))
    env.update({
        "EGG_RELOAD_THREAD_ID": "outer-thread",
        "EGG_RELOAD_STATE_FILE": str(inherited_state),
        "EGG_RELOAD_EXIT_CODE": "99",
    })

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed["thread"] == "outer-thread"
    assert observed["state_file"] != str(inherited_state)
    assert inherited_state.read_text(encoding="utf-8") == "outer-thread\n"
    assert not Path(observed["state_file"]).exists()


def test_egg_wrapper_reload_loop_is_bounded_and_cleans_state(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "always-reload",
        """#!/usr/bin/env python3
import json, os
count_file = os.environ["EGG_TEST_COUNT"]
try:
    count = int(open(count_file, encoding="utf-8").read())
except Exception:
    count = 0
with open(count_file, "w", encoding="utf-8") as handle:
    handle.write(str(count + 1))
with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
    handle.write("reload-thread\\n")
raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
""",
    )
    count_file = tmp_path / "count"

    result = subprocess.run(
        [str(_egg_wrapper()), "quoted arg"],
        cwd=tmp_path,
        env=_launcher_env(
            tmp_path,
            fake_python,
            EGG_TEST_COUNT=str(count_file),
            EGG_MAX_RELOADS="2",
            TMPDIR=str(tmp_path),
        ),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == RELOAD_EXIT_CODE
    assert count_file.read_text(encoding="utf-8") == "3"
    assert "reload limit (2) exceeded" in result.stderr
    assert not list(tmp_path.glob("egg-reload.*"))


def test_egg_wrapper_non_reload_failure_preserves_status_and_cleans_file(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "fail",
        """#!/usr/bin/env python3
import os
with open(os.environ["EGG_TEST_STATE_PATH"], "w", encoding="utf-8") as handle:
    handle.write(os.environ["EGG_RELOAD_STATE_FILE"])
raise SystemExit(23)
""",
    )
    state_path = tmp_path / "state-path"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(
            tmp_path,
            fake_python,
            EGG_TEST_STATE_PATH=str(state_path),
            TMPDIR=str(tmp_path),
        ),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 23
    assert not Path(state_path.read_text(encoding="utf-8")).exists()


def test_egg_wrapper_rejects_reload_without_state_and_cleans_file(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "missing-state",
        """#!/usr/bin/env python3
import os
with open(os.environ["EGG_TEST_STATE_PATH"], "w", encoding="utf-8") as handle:
    handle.write(os.environ["EGG_RELOAD_STATE_FILE"])
raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
""",
    )
    state_path = tmp_path / "state-path"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(
            tmp_path,
            fake_python,
            EGG_TEST_STATE_PATH=str(state_path),
            TMPDIR=str(tmp_path),
        ),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == RELOAD_EXIT_CODE
    assert "reload requested without a saved thread id" in result.stderr
    assert not Path(state_path.read_text(encoding="utf-8")).exists()


def test_egg_wrapper_rejects_invalid_reload_bound_without_starting_child(tmp_path: Path):
    marker = tmp_path / "started"
    fake_python = _write_executable(
        tmp_path / "must-not-run",
        f"#!/bin/sh\ntouch {marker!s}\n",
    )

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_MAX_RELOADS="unbounded"),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "must be a non-negative integer" in result.stderr
    assert not marker.exists()


def test_egg_wrapper_cleans_state_and_process_group_on_signal(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "blocking-child",
        """#!/usr/bin/env python3
import os, subprocess, sys, time
helper = subprocess.Popen([
    sys.executable,
    "-c",
    "import time; time.sleep(60)",
])
with open(os.environ["EGG_TEST_CHILD"], "w", encoding="utf-8") as handle:
    handle.write(
        f"{os.getpid()}\\n{helper.pid}\\n{os.environ['EGG_RELOAD_STATE_FILE']}\\n"
    )
while True:
    time.sleep(1)
""",
    )
    child_info = tmp_path / "child-info"
    proc = subprocess.Popen(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CHILD=str(child_info)),
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not child_info.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_info.exists()
    child_pid_text, helper_pid_text, state_file_text = child_info.read_text(
        encoding="utf-8"
    ).splitlines()
    child_pid = int(child_pid_text)
    helper_pid = int(helper_pid_text)

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=3)

    assert proc.returncode == 143
    assert not Path(state_file_text).exists()
    for pid in (child_pid, helper_pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            raise AssertionError(f"launcher leaked process {pid}")
