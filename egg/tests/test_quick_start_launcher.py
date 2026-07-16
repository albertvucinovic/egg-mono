from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import termios
import time
from pathlib import Path

import pytest


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


def _start_pty_process(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], int]:
    master, slave = os.openpty()

    def acquire_controlling_tty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=acquire_controlling_tty,
    )
    os.close(slave)
    return proc, master


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


def test_egg_wrapper_state_file_is_owner_only(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "capture-mode",
        """#!/usr/bin/env python3
import os, stat
path = os.environ["EGG_RELOAD_STATE_FILE"]
with open(os.environ["EGG_TEST_CAPTURE"], "w", encoding="utf-8") as handle:
    handle.write(oct(stat.S_IMODE(os.stat(path).st_mode)))
""",
    )
    capture = tmp_path / "mode"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert capture.read_text(encoding="utf-8") == "0o600"


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


@pytest.mark.parametrize("value", ["0", "00", "100", "000100"])
def test_egg_wrapper_accepts_valid_reload_bounds(tmp_path: Path, value: str):
    marker = tmp_path / "started"
    fake_python = _write_executable(
        tmp_path / "run-once",
        f"#!/bin/sh\ntouch {marker!s}\n",
    )

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_MAX_RELOADS=value),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()


@pytest.mark.parametrize(
    "value",
    ["", "-1", "+1", " 1", "1 ", "1.0", "101", "9" * 10_000],
    ids=[
        "empty",
        "negative",
        "positive-sign",
        "leading-space",
        "trailing-space",
        "decimal",
        "too-large",
        "huge",
    ],
)
def test_egg_wrapper_rejects_invalid_reload_bound_without_starting_child(
    tmp_path: Path, value: str
):
    marker = tmp_path / "started"
    fake_python = _write_executable(
        tmp_path / "must-not-run",
        f"#!/bin/sh\ntouch {marker!s}\n",
    )

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_MAX_RELOADS=value),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "must be a decimal integer from 0 to 100" in result.stderr
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


def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), path


def _assert_pid_gone(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    raise AssertionError(f"launcher leaked process {pid}")


@pytest.mark.parametrize("outcome", ["reload", "exit"])
def test_egg_wrapper_kills_hostile_descendant_before_reload_or_exit(
    tmp_path: Path, outcome: str
):
    fake_python = _write_executable(
        tmp_path / "hostile-generation",
        """#!/usr/bin/env python3
import os, signal, subprocess, sys
capture = os.environ["EGG_TEST_CAPTURE"]
if os.environ.get("EGG_RELOAD_THREAD_ID"):
    with open(capture + ".second", "w", encoding="utf-8") as handle:
        handle.write("started\\n")
    raise SystemExit(0)
helper = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(60)",
])
with open(capture + ".helper", "w", encoding="utf-8") as handle:
    handle.write(str(helper.pid))
if os.environ["EGG_TEST_OUTCOME"] == "reload":
    with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
        handle.write("reload-thread\\n")
    raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
raise SystemExit(0)
""",
    )
    capture = tmp_path / "capture"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(
            tmp_path,
            fake_python,
            EGG_TEST_CAPTURE=str(capture),
            EGG_TEST_OUTCOME=outcome,
        ),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    helper_pid = int(Path(f"{capture}.helper").read_text(encoding="utf-8"))
    _assert_pid_gone(helper_pid)
    assert Path(f"{capture}.second").exists() is (outcome == "reload")


def test_egg_wrapper_escalates_signal_for_hostile_generation_and_descendant(
    tmp_path: Path,
):
    fake_python = _write_executable(
        tmp_path / "hostile-signal",
        """#!/usr/bin/env python3
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
helper = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
with open(os.environ["EGG_TEST_CAPTURE"], "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()}\\n{helper.pid}\\n{os.environ['EGG_RELOAD_STATE_FILE']}\\n")
while True:
    time.sleep(1)
""",
    )
    capture = tmp_path / "capture"
    proc = subprocess.Popen(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_path(capture)
        generation_text, helper_text, state_text = capture.read_text(
            encoding="utf-8"
        ).splitlines()
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 143, (stdout, stderr)
        assert not Path(state_text).exists()
        _assert_pid_gone(int(generation_text))
        _assert_pid_gone(int(helper_text))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_egg_wrapper_signal_between_generations_does_not_start_next_child(
    tmp_path: Path,
):
    fake_python = _write_executable(
        tmp_path / "between-generations",
        """#!/usr/bin/env python3
import os, signal
count_path = os.environ["EGG_TEST_COUNT"]
try:
    count = int(open(count_path, encoding="utf-8").read())
except Exception:
    count = 0
with open(count_path, "w", encoding="utf-8") as handle:
    handle.write(str(count + 1))
if count:
    raise SystemExit(0)
with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
    handle.write("reload-thread\\n")
os.kill(os.getppid(), signal.SIGTERM)
raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
""",
    )
    count = tmp_path / "count"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_COUNT=str(count)),
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 143, result.stdout + result.stderr
    assert count.read_text(encoding="utf-8") == "1"


def test_egg_wrapper_explicit_interpreter_never_runs_repo_provisioning(
    tmp_path: Path,
):
    # A clean checkout has no ignored repo venv. Copy the launcher beside a
    # make executable that would visibly fail if provisioning were attempted.
    checkout = tmp_path / "checkout"
    (checkout / "egg" / "egg").mkdir(parents=True)
    (checkout / "egg" / "egg.sh").write_bytes(_egg_wrapper().read_bytes())
    (checkout / "egg" / "egg.sh").chmod(0o755)
    supervisor = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    (checkout / "egg" / "egg" / "launcher_supervisor.py").write_bytes(
        supervisor.read_bytes()
    )
    marker = tmp_path / "provisioned"
    make = _write_executable(
        tmp_path / "make",
        f"#!/bin/sh\ntouch {marker}\nexit 99\n",
    )
    fake_python = _write_executable(
        tmp_path / "explicit-python",
        "#!/bin/sh\nexit 0\n",
    )
    env = _launcher_env(tmp_path, fake_python)
    env["PATH"] = f"{make.parent}:{env['PATH']}"

    result = subprocess.run(
        [str(checkout / "egg" / "egg.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    assert not (checkout / "venv").exists()


def _read_until(fd: int, marker: bytes, timeout: float = 5.0) -> bytes:
    import select

    data = b""
    deadline = time.monotonic() + timeout
    while marker not in data and time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if readable:
            try:
                data += os.read(fd, 4096)
            except OSError:
                break
    assert marker in data, data
    return data


def test_egg_wrapper_preserves_controlling_tty_and_foreground_input_after_reload(
    tmp_path: Path,
):
    fake_python = _write_executable(
        tmp_path / "pty-child",
        """#!/usr/bin/env python3
import json, os, sys
capture = os.environ["EGG_TEST_CAPTURE"]
generation = "second" if os.environ.get("EGG_RELOAD_THREAD_ID") else "first"
record = {
    "isatty": os.isatty(0),
    "pgrp": os.getpgrp(),
    "foreground": os.tcgetpgrp(0),
    "session": os.getsid(0),
}
print(f"READY-{generation}", flush=True)
record["line"] = sys.stdin.readline()
with open(f"{capture}.{generation}", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
if generation == "first":
    with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
        handle.write("reload-thread\\n")
    raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
""",
    )
    capture = tmp_path / "pty-capture"
    proc, master = _start_pty_process(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
    )
    try:
        _read_until(master, b"READY-first")
        os.write(master, b"first input\n")
        _read_until(master, b"READY-second")
        os.write(master, b"second input\n")
        assert proc.wait(timeout=5) == 0
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    first = json.loads(Path(f"{capture}.first").read_text(encoding="utf-8"))
    second = json.loads(Path(f"{capture}.second").read_text(encoding="utf-8"))
    for record, line in ((first, "first input\n"), (second, "second input\n")):
        assert record["isatty"] is True
        assert record["pgrp"] == record["foreground"]
        assert record["line"] == line
    assert first["session"] == second["session"]


def test_egg_wrapper_pty_mirrors_stop_and_restores_foreground_input(
    tmp_path: Path,
):
    fake_python = _write_executable(
        tmp_path / "pty-stop",
        """#!/usr/bin/env python3
import os, signal, sys
print(f"READY {os.getpid()}", flush=True)
os.kill(os.getpid(), signal.SIGTSTP)
print(f"RESUMED {os.getpgrp()} {os.tcgetpgrp(0)}", flush=True)
line = sys.stdin.readline()
with open(os.environ["EGG_TEST_CAPTURE"], "w", encoding="utf-8") as handle:
    handle.write(line)
""",
    )
    capture = tmp_path / "stop-capture"
    proc, master = _start_pty_process(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
    )
    try:
        ready = _read_until(master, b"READY")
        child_pid = int(ready.split(b"READY ", 1)[1].split()[0])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                status = Path(f"/proc/{proc.pid}/stat").read_text(
                    encoding="ascii"
                ).split()[2]
            except FileNotFoundError:
                status = ""
            if status in {"T", "t"}:
                break
            time.sleep(0.01)
        assert status in {"T", "t"}
        os.kill(proc.pid, signal.SIGCONT)
        resumed = _read_until(master, b"RESUMED")
        resumed_pgrp, resumed_foreground = map(
            int, resumed.split(b"RESUMED ", 1)[1].split()[:2]
        )
        assert resumed_pgrp == child_pid
        assert resumed_foreground == child_pid
        os.write(master, b"after resume\n")
        assert proc.wait(timeout=5) == 0
        assert capture.read_text(encoding="utf-8") == "after resume\n"
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_egg_wrapper_pty_forwards_sigwinch_and_ctrl_c(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "pty-signals",
        """#!/usr/bin/env python3
import os, signal, time
path = os.environ["EGG_TEST_SIGNALS"]
def note(name):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(name + "\\n")
signal.signal(signal.SIGWINCH, lambda *_: note("WINCH"))
print("READY", flush=True)
while True:
    time.sleep(0.05)
""",
    )
    signals = tmp_path / "signals"
    proc, master = _start_pty_process(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_SIGNALS=str(signals)),
    )
    try:
        _read_until(master, b"READY")
        proc.send_signal(signal.SIGWINCH)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if signals.exists() and "WINCH" in signals.read_text(encoding="utf-8"):
                break
            time.sleep(0.01)
        assert signals.read_text(encoding="utf-8") == "WINCH\n"
        os.write(master, b"\x03")
        assert proc.wait(timeout=5) == 130
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
