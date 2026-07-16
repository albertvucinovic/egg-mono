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


def test_egg_wrapper_recreated_state_file_remains_owner_only(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "recreate-state",
        """#!/usr/bin/env python3
import os, stat
path = os.environ["EGG_RELOAD_STATE_FILE"]
os.unlink(path)
with open(path, "w", encoding="utf-8") as handle:
    handle.write("")
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


def _copy_launcher_tree(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "egg" / "egg").mkdir(parents=True)
    wrapper = _egg_wrapper()
    (checkout / "egg" / "egg.sh").write_bytes(wrapper.read_bytes())
    (checkout / "egg" / "egg.sh").chmod(0o755)
    (checkout / "egg" / "egg" / "launcher_supervisor.py").write_bytes(
        (wrapper.parent / "egg" / "launcher_supervisor.py").read_bytes()
    )
    return checkout


def test_egg_wrapper_dotenv_controls_interpreter_supervisor_and_reload_bound(
    tmp_path: Path,
):
    checkout = _copy_launcher_tree(tmp_path)
    capture = tmp_path / "capture"
    app = _write_executable(
        tmp_path / "dotenv-app",
        """#!/usr/bin/env python3
import os
capture = os.environ["EGG_TEST_CAPTURE"]
try:
    count = int(open(capture, encoding="utf-8").read())
except Exception:
    count = 0
with open(capture, "w", encoding="utf-8") as handle:
    handle.write(str(count + 1))
with open(os.environ["EGG_RELOAD_STATE_FILE"], "w", encoding="utf-8") as handle:
    handle.write("dotenv-thread\\n")
raise SystemExit(int(os.environ["EGG_RELOAD_EXIT_CODE"]))
""",
    )
    supervisor_log = tmp_path / "supervisor-log"
    supervisor = _write_executable(
        tmp_path / "dotenv-supervisor",
        f"#!/bin/sh\necho used > {supervisor_log}\nexec {os.fsencode(os.sys.executable).decode()} \"$@\"\n",
    )
    (checkout / "egg" / ".env").write_text(
        f"EGG_PYTHON_BIN={app}\n"
        f"EGG_SUPERVISOR_PYTHON={supervisor}\n"
        "EGG_MAX_RELOADS=0\n"
        f"EGG_TEST_CAPTURE={capture}\n",
        encoding="utf-8",
    )
    env = {**os.environ}
    for key in (
        "EGG_PYTHON_BIN",
        "EGG_SUPERVISOR_PYTHON",
        "EGG_MAX_RELOADS",
        "EGG_TEST_CAPTURE",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [str(checkout / "egg" / "egg.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == RELOAD_EXIT_CODE
    assert capture.read_text(encoding="utf-8") == "1"
    assert supervisor_log.read_text(encoding="utf-8") == "used\n"
    assert "reload limit (0) exceeded" in result.stderr
    assert not (checkout / "venv").exists()


def test_egg_wrapper_term_during_provisioning_stops_setup_and_never_launches(
    tmp_path: Path,
):
    checkout = _copy_launcher_tree(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    setup_info = tmp_path / "setup-info"
    app_started = tmp_path / "app-started"
    fake_python3 = _write_executable(
        bin_dir / "python3",
        """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    cat > "$3/bin/activate" <<'ACTIVATE'
PATH="$VIRTUAL_ENV/bin:$PATH"
export PATH
ACTIVATE
    echo "$$ $EGG_TEST_SETUP_INFO" > "$EGG_TEST_SETUP_INFO"
    trap '' TERM
    sleep 60 &
    wait
fi
exit 99
""",
    )
    _write_executable(
        tmp_path / "must-not-start",
        f"#!/bin/sh\ntouch {app_started}\n",
    )
    env = {**os.environ}
    env.pop("EGG_PYTHON_BIN", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["EGG_TEST_SETUP_INFO"] = str(setup_info)
    proc = subprocess.Popen(
        [str(checkout / "egg" / "egg.sh")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_path(setup_info)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 143, (stdout, stderr)
        assert not app_started.exists()
        assert not list(tmp_path.glob("egg-reload.*"))
        setup_pid = int(setup_info.read_text(encoding="utf-8").split()[0])
        _assert_pid_gone(setup_pid)
        live_group_members = []
        for stat_path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat_path.read_text(encoding="ascii").rsplit(") ", 1)[1].split()
            except (FileNotFoundError, IndexError):
                continue
            if int(fields[2]) == setup_pid and fields[0] != "Z":
                live_group_members.append(int(stat_path.parent.name))
        assert live_group_members == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        # The fake setup deliberately leaves a partial tree; it must not contain
        # launcher state and a future run must not treat mode-unsafe state as valid.
        assert not app_started.exists()


def test_process_owner_non_linux_uses_best_effort_group_cleanup(monkeypatch):
    import importlib.util

    path = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    spec = importlib.util.spec_from_file_location("test_launcher_supervisor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        module,
        "_signal_group",
        lambda pgid, signum: calls.append((pgid, signum)),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    owner = module._ProcessOwner()
    owner.quiesce(123, leader_exited=False)

    assert owner.strong is False
    assert calls == [
        (123, signal.SIGCONT),
        (123, signal.SIGTERM),
        (123, signal.SIGKILL),
    ]


def test_process_owner_linux_procfs_failure_is_closed(monkeypatch):
    import importlib.util

    path = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    spec = importlib.util.spec_from_file_location("test_launcher_supervisor_proc", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(
        module._ProcessOwner,
        "_verify_procfs",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no proc"))),
    )

    with pytest.raises(RuntimeError, match="no proc"):
        module._ProcessOwner()


def test_supervisor_cleanup_error_still_restores_handlers_and_unlinks_state(
    monkeypatch, tmp_path: Path
):
    import importlib.util

    path = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    spec = importlib.util.spec_from_file_location("test_launcher_cleanup", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    old_handler = signal.getsignal(signal.SIGTERM)

    class Owner:
        def quiesce(self, _pid: int, *, leader_exited: bool) -> None:
            raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(module, "_ProcessOwner", Owner)
    monkeypatch.setattr(module, "_spawn", lambda *_args: 99_999_999)
    monkeypatch.setattr(
        module,
        "_wait_generation",
        lambda *_args, **kwargs: kwargs["owns_foreground"],
    )
    monkeypatch.setattr(module, "_foreground_pgrp", lambda _fd: None)
    monkeypatch.setattr(module, "_controlling_tty_fd", lambda: None)
    monkeypatch.setattr(module.os, "waitpid", lambda *_args: (0, 0))

    with pytest.raises(RuntimeError, match="cleanup exploded"):
        module.supervise(
            ["child"],
            cwd=str(tmp_path),
            state_file=state,
            reload_exit_code=75,
            max_reloads=1,
        )

    assert not state.exists()
    assert signal.getsignal(signal.SIGTERM) is old_handler


def _start_interactive_shell(
    tmp_path: Path, fake_python: Path
) -> tuple[subprocess.Popen[bytes], int]:
    master, slave = os.openpty()

    def acquire_controlling_tty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = _launcher_env(tmp_path, fake_python)
    env.update({"PS1": "EGG-TEST-PROMPT> ", "PS2": ""})
    proc = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-i"],
        cwd=tmp_path,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=acquire_controlling_tty,
    )
    os.close(slave)
    return proc, master


def _shell_write(master: int, command: str) -> None:
    os.write(master, command.encode("utf-8") + b"\n")


def test_egg_wrapper_interactive_shell_background_job_never_steals_tty(
    tmp_path: Path,
):
    fake_python = _write_executable(
        tmp_path / "background-child",
        """#!/usr/bin/env python3
import os, time
print(f"BACKGROUND {os.getpgrp()} {os.tcgetpgrp(0)}", flush=True)
time.sleep(0.2)
""",
    )
    shell, master = _start_interactive_shell(tmp_path, fake_python)
    try:
        _read_until(master, b"EGG-TEST-PROMPT>")
        _shell_write(master, f"{_egg_wrapper()} &")
        launched = _read_until(master, b"EGG-TEST-PROMPT>")
        job_pgid = int(launched.split(b"[1] ", 1)[1].split()[0])
        output = _read_until(master, b"BACKGROUND")
        child_pgrp, foreground = map(
            int, output.split(b"BACKGROUND ", 1)[1].split()[:2]
        )
        assert child_pgrp != job_pgid
        assert foreground != job_pgid
        assert foreground != child_pgrp
        _shell_write(master, "wait %1")
        _read_until(master, b"EGG-TEST-PROMPT>")
        _shell_write(master, "echo SHELL-ALIVE")
        _read_until(master, b"SHELL-ALIVE")
    finally:
        _shell_write(master, "exit")
        shell.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("resume", ["bg", "fg"])
def test_egg_wrapper_interactive_shell_stop_respects_bg_and_fg(
    tmp_path: Path, resume: str
):
    fake_python = _write_executable(
        tmp_path / "stopping-child",
        """#!/usr/bin/env python3
import os, signal, sys
print(f"STOPPING {os.getpid()} {os.getpgrp()} {os.tcgetpgrp(0)}", flush=True)
os.kill(os.getpid(), signal.SIGTSTP)
print(f"RESUMED {os.getpid()} {os.getpgrp()} {os.tcgetpgrp(0)}", flush=True)
line = sys.stdin.readline()
print(f"GOT {line.strip()}", flush=True)
""",
    )
    shell, master = _start_interactive_shell(tmp_path, fake_python)
    try:
        _read_until(master, b"EGG-TEST-PROMPT>")
        _shell_write(master, str(_egg_wrapper()))
        stopped = _read_until(master, b"STOPPING")
        child_pid = int(stopped.split(b"STOPPING ", 1)[1].split()[0])
        _read_until(master, b"EGG-TEST-PROMPT>")
        _shell_write(master, f"{resume} %1")
        if resume == "bg":
            background = _read_until(master, b"RESUMED")
            background_pgrp, background_foreground = map(
                int, background.split(b"RESUMED ", 1)[1].split()[1:3]
            )
            assert background_pgrp == child_pid
            assert background_foreground != child_pid
            _shell_write(master, "fg %1")
            # The first background read stopped the child with SIGTTIN. Bash
            # may report that stop before applying this fg; a second fg then
            # resumes the complete job in the foreground.
            foreground_output = _read_until(master, b"EGG-TEST-PROMPT>")
            if b"Stopped" in foreground_output:
                _shell_write(master, "fg %1")
            else:
                # The shell can print the prompt before its asynchronous job
                # notification. Wait for that notification before the retry.
                notification = _read_until(master, b"Stopped")
                assert b"Stopped" in notification
                _shell_write(master, "fg %1")
            resumed = background
        else:
            resumed = _read_until(master, b"RESUMED")
        child_pgrp, foreground = map(
            int, resumed.split(b"RESUMED ", 1)[1].split()[1:3]
        )
        assert child_pgrp == child_pid
        if resume == "fg":
            assert foreground == child_pgrp
        os.write(master, b"shell input\n")
        _read_until(master, b"GOT shell input")
        _read_until(master, b"EGG-TEST-PROMPT>")
    finally:
        if shell.poll() is None:
            _shell_write(master, "exit")
            try:
                shell.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(shell.pid, signal.SIGKILL)
                shell.wait()
        os.close(master)


def test_egg_wrapper_child_sigstop_is_mirrored_without_crash(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "sigstop-child",
        """#!/usr/bin/env python3
import os, signal
print(f"READY {os.getpid()}", flush=True)
os.kill(os.getpid(), signal.SIGSTOP)
print("CONTINUED", flush=True)
""",
    )
    proc, master = _start_pty_process(
        [str(_egg_wrapper())], cwd=tmp_path, env=_launcher_env(tmp_path, fake_python)
    )
    try:
        _read_until(master, b"READY")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = Path(f"/proc/{proc.pid}/stat").read_text(encoding="ascii").split()[2]
            if status in {"T", "t"}:
                break
            time.sleep(0.01)
        assert status in {"T", "t"}
        os.kill(proc.pid, signal.SIGCONT)
        _read_until(master, b"CONTINUED")
        assert proc.wait(timeout=5) == 0
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()




def test_egg_wrapper_kills_sequential_reparent_setsid_cascade(tmp_path: Path):
    fake_python = _write_executable(
        tmp_path / "cascade-generation",
        """#!/usr/bin/env python3
import os, signal, subprocess, sys
capture = os.environ["EGG_TEST_CAPTURE"]
code_c = "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(os.environ['EGG_TEST_CAPTURE'] + '.c', 'w').write(str(os.getpid())); time.sleep(60)"
code_b = "import os,signal,subprocess,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(os.environ['EGG_TEST_CAPTURE'] + '.b', 'w').write(str(os.getpid())); subprocess.Popen([sys.executable, '-c', os.environ['EGG_TEST_CODE_C']]); time.sleep(60)"
code_a = "import os,signal,subprocess,sys,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); open(os.environ['EGG_TEST_CAPTURE'] + '.a', 'w').write(str(os.getpid())); subprocess.Popen([sys.executable, '-c', os.environ['EGG_TEST_CODE_B']]); time.sleep(60)"
os.environ["EGG_TEST_CODE_B"] = code_b
os.environ["EGG_TEST_CODE_C"] = code_c
subprocess.Popen([sys.executable, "-c", code_a])
import time
for suffix in (".a", ".b", ".c"):
    deadline = time.monotonic() + 3
    while not os.path.exists(capture + suffix) and time.monotonic() < deadline:
        time.sleep(0.01)
with open(capture + ".state", "w", encoding="utf-8") as handle:
    handle.write(os.environ["EGG_RELOAD_STATE_FILE"])
raise SystemExit(0)
""",
    )
    capture = tmp_path / "cascade"

    result = subprocess.run(
        [str(_egg_wrapper())],
        cwd=tmp_path,
        env=_launcher_env(tmp_path, fake_python, EGG_TEST_CAPTURE=str(capture)),
        text=True,
        capture_output=True,
        timeout=7,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for suffix in ("a", "b", "c"):
        path = Path(f"{capture}.{suffix}")
        _wait_for_path(path)
        _assert_pid_gone(int(path.read_text(encoding="utf-8")))
    state = Path(Path(f"{capture}.state").read_text(encoding="utf-8"))
    assert not state.exists()






def test_supervisor_pending_term_at_wait_entry_kills_ignoring_child(
    monkeypatch, tmp_path: Path
):
    import importlib.util

    path = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    spec = importlib.util.spec_from_file_location("test_launcher_signal_gap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    ready = tmp_path / "ready"
    child = _write_executable(
        tmp_path / "term-ignoring-child",
        """#!/usr/bin/env python3
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["EGG_TEST_READY"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(1)
""",
    )
    monkeypatch.setenv("EGG_TEST_READY", str(ready))
    original_wait = module._wait_generation
    entered = False

    def inject_at_wait_entry(*args, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            _wait_for_path(ready)
            os.kill(os.getpid(), signal.SIGTERM)
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(module, "_wait_generation", inject_at_wait_entry)
    started = time.monotonic()

    status = module.supervise(
        [str(child)],
        cwd=str(tmp_path),
        state_file=state,
        reload_exit_code=75,
        max_reloads=1,
    )

    elapsed = time.monotonic() - started
    assert status == 143
    assert elapsed < 1.5
    assert not state.exists()
    _assert_pid_gone(int(ready.read_text(encoding="utf-8")))


def _load_launcher_supervisor(name: str):
    import importlib.util

    path = _egg_wrapper().parent / "egg" / "launcher_supervisor.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("boundary", ["generation-start", "post-wait"])
def test_supervisor_latched_term_cannot_be_erased_at_old_clear_boundaries(
    monkeypatch, tmp_path: Path, boundary: str
):
    module = _load_launcher_supervisor(f"test_launcher_latched_{boundary}")
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    ready = tmp_path / "ready"
    child = _write_executable(
        tmp_path / "term-ignoring-child",
        """#!/usr/bin/env python3
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["EGG_TEST_READY"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(1)
""",
    )
    monkeypatch.setenv("EGG_TEST_READY", str(ready))

    if boundary == "generation-start":
        original_spawn = module._spawn

        def inject_after_initial_check(*args, **kwargs):
            # In the rejected list implementation this signal landed after the
            # top-of-loop check and was erased by the immediately following clear.
            os.kill(os.getpid(), signal.SIGTERM)
            return original_spawn(*args, **kwargs)

        monkeypatch.setattr(module, "_spawn", inject_after_initial_check)
    else:
        original_waitpid = module.os.waitpid

        def inject_after_post_wait_check(pid, options):
            result = original_waitpid(pid, options)
            if options == 0:
                # Use an already exited first generation and latch TERM at the
                # old post-wait check/clear boundary before another can spawn.
                os.kill(os.getpid(), signal.SIGTERM)
            return result

        monkeypatch.setattr(module.os, "waitpid", inject_after_post_wait_check)
        # The post-wait boundary does not need an ignoring child; generation one
        # exits normally and the assertion is that no generation two is started.
        child = _write_executable(
            tmp_path / "one-shot-child",
            """#!/usr/bin/env python3
import os
with open(os.environ["EGG_TEST_READY"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
""",
        )

    started = time.monotonic()
    status = module.supervise(
        [str(child)],
        cwd=str(tmp_path),
        state_file=state,
        reload_exit_code=75,
        max_reloads=1,
    )
    elapsed = time.monotonic() - started

    assert status == 143
    assert elapsed < 1.5
    assert not state.exists()
    _assert_pid_gone(int(ready.read_text(encoding="utf-8")))


def test_process_owner_finalizer_kills_live_term_ignoring_leader(tmp_path: Path):
    module = _load_launcher_supervisor("test_launcher_live_leader")
    owner = module._ProcessOwner()
    ready = tmp_path / "ready"
    child = _write_executable(
        tmp_path / "live-leader",
        """#!/usr/bin/env python3
import os, signal, time
os.setpgid(0, 0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["EGG_TEST_READY"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(1)
""",
    )
    env = {**os.environ, "EGG_TEST_READY": str(ready)}
    proc = subprocess.Popen([str(child)], env=env)
    try:
        _wait_for_path(ready)
        started = time.monotonic()
        owner.quiesce(proc.pid, leader_exited=False)
        assert time.monotonic() - started < 1.5
        assert proc.wait(timeout=2) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_supervisor_owner_initialization_failure_unlinks_state_and_restores_handlers(
    monkeypatch, tmp_path: Path
):
    module = _load_launcher_supervisor("test_launcher_owner_init")
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    old_handler = signal.getsignal(signal.SIGTERM)

    class BrokenOwner:
        def __init__(self) -> None:
            raise RuntimeError("owner init failed")

    monkeypatch.setattr(module, "_ProcessOwner", BrokenOwner)

    with pytest.raises(RuntimeError, match="owner init failed"):
        module.supervise(
            ["child"],
            cwd=str(tmp_path),
            state_file=state,
            reload_exit_code=75,
            max_reloads=1,
        )

    assert not state.exists()
    assert signal.getsignal(signal.SIGTERM) is old_handler


def test_supervisor_cleanup_failure_fallback_kills_live_ignoring_leader(
    monkeypatch, tmp_path: Path
):
    module = _load_launcher_supervisor("test_launcher_cleanup_fallback")
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    ready = tmp_path / "ready"
    child = _write_executable(
        tmp_path / "cleanup-failure-child",
        """#!/usr/bin/env python3
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["EGG_TEST_READY"], "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(1)
""",
    )
    monkeypatch.setenv("EGG_TEST_READY", str(ready))
    old_handler = signal.getsignal(signal.SIGTERM)

    class BrokenOwner:
        def quiesce(self, _pid: int, *, leader_exited: bool) -> None:
            raise RuntimeError("procfs disappeared")

    monkeypatch.setattr(module, "_ProcessOwner", BrokenOwner)
    original_wait = module._wait_generation

    def fail_after_child_is_ready(*args, **kwargs):
        _wait_for_path(ready)
        raise RuntimeError("trigger finalizer")

    monkeypatch.setattr(module, "_wait_generation", fail_after_child_is_ready)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="trigger finalizer") as raised:
        module.supervise(
            [str(child)],
            cwd=str(tmp_path),
            state_file=state,
            reload_exit_code=75,
            max_reloads=1,
        )

    assert any("procfs disappeared" in note for note in raised.value.__notes__)
    assert time.monotonic() - started < 1.5
    child_pid = int(ready.read_text(encoding="utf-8"))
    _assert_pid_gone(child_pid)
    assert not state.exists()
    assert signal.getsignal(signal.SIGTERM) is old_handler
    # Keep a reference so static checkers/reviewers can see only the wait
    # boundary—not production cleanup—is replaced by this regression.
    assert original_wait is not fail_after_child_is_ready


def test_supervisor_term_after_final_sample_overrides_normal_status(
    monkeypatch, tmp_path: Path
):
    module = _load_launcher_supervisor("test_launcher_final_sample")
    state = tmp_path / "state"
    state.write_text("", encoding="utf-8")
    child = _write_executable(tmp_path / "exit-zero", "#!/bin/sh\nexit 0\n")
    original_terminating = module._SignalRelay.terminating
    samples = 0

    def inject_after_final_sample(relay):
        nonlocal samples
        value = original_terminating(relay)
        samples += 1
        if samples == 2:
            # This is after the post-wait sample has returned 0. The rejected
            # code immediately returned child status 0 and omitted this TERM.
            os.kill(os.getpid(), signal.SIGTERM)
        return value

    monkeypatch.setattr(module._SignalRelay, "terminating", inject_after_final_sample)

    status = module.supervise(
        [str(child)],
        cwd=str(tmp_path),
        state_file=state,
        reload_exit_code=75,
        max_reloads=1,
    )

    assert samples == 2
    assert status == 143
    assert not state.exists()
