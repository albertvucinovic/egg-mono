"""Foreground-safe, bounded reload supervisor used by ``egg.sh``."""
from __future__ import annotations

import argparse
import ctypes
import errno
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

_PR_SET_CHILD_SUBREAPER = 36
_GRACE_SECONDS = 0.5
_POLL_SECONDS = 0.01
_TERMINATING_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
_JOB_CONTROL_SIGNALS = (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)
_FORWARDED_SIGNALS = (*_TERMINATING_SIGNALS, signal.SIGWINCH, *_JOB_CONTROL_SIGNALS)


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("egg launcher supervision currently requires Linux")


def _enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _linux_direct_children() -> list[int]:
    """Return direct children from Linux procfs without reaping them."""

    path = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
    try:
        values = Path(path).read_text(encoding="ascii").split()
    except OSError:
        return []
    children: list[int] = []
    for value in values:
        try:
            children.append(int(value))
        except ValueError:
            continue
    return children


def _reap_linux_adopted(leader_pid: int) -> None:
    for pid in _linux_direct_children():
        if pid == leader_pid:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue


def _signal_pid(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _process_group_has_live_member(pgid: int) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        process_paths = Path("/proc").iterdir()
    except OSError:
        return False
    for path in process_paths:
        if not path.name.isdigit() or path.name == str(pgid):
            continue
        try:
            fields = (
                (path / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
            )
            state = fields[0]
            process_group = int(fields[2])
        except (IndexError, OSError, ValueError):
            continue
        if process_group == pgid and state != "Z":
            return True
    return False


def _quiesce_generation(pgid: int) -> None:
    """Terminate/reap every process left by one completed generation.

    The generation gets its own process group and this process is a subreaper.
    We never start the next generation until that group has no live members and
    all adopted descendants have been reaped. The leader stays a zombie until
    the caller collects its status, preventing PGID reuse while group signalling
    is still possible.
    """

    _signal_group(pgid, signal.SIGCONT)
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        children = _linux_direct_children()
        adopted = [pid for pid in children if pid != pgid]
        group_alive = _process_group_has_live_member(pgid)
        if not adopted and not group_alive:
            return
        for pid in adopted:
            _signal_pid(pid, signal.SIGCONT)
            _signal_pid(pid, signal.SIGTERM)
        _reap_linux_adopted(pgid)
        time.sleep(_POLL_SECONDS)

    _signal_group(pgid, signal.SIGCONT)
    _signal_group(pgid, signal.SIGKILL)
    for pid in _linux_direct_children():
        if pid == pgid:
            continue
        _signal_pid(pid, signal.SIGCONT)
        _signal_pid(pid, signal.SIGKILL)

    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        _reap_linux_adopted(pgid)
        children = _linux_direct_children()
        adopted = [pid for pid in children if pid != pgid]
        group_alive = _process_group_has_live_member(pgid)
        if not adopted and not group_alive:
            return
        time.sleep(_POLL_SECONDS)
    raise RuntimeError(f"generation process group {pgid} did not terminate")


def _wait_exit_code(wait_status: int) -> int:
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status)
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return 1


def _tty_fd() -> int | None:
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                os.tcgetpgrp(fd)
                return fd
        except OSError:
            continue
    return None


def _set_foreground_pgrp(fd: int | None, pgid: int) -> None:
    if fd is None:
        return
    previous = signal.getsignal(signal.SIGTTOU)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, pgid)
    except OSError as exc:
        if exc.errno not in (errno.ENOTTY, errno.ESRCH):
            raise
    finally:
        signal.signal(signal.SIGTTOU, previous)


def _spawn(argv: Sequence[str], cwd: str) -> int:
    # Block forwarded signals across fork/setpgid. The child restores inherited
    # handlers and unmasks before exec; the parent publishes the child PGID
    # before unmasking, so no termination can land in the creation gap.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARDED_SIGNALS)
    try:
        pid = os.fork()
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        raise
    if pid == 0:
        try:
            os.setpgid(0, 0)
            for signum in _FORWARDED_SIGNALS:
                signal.signal(signum, signal.SIG_DFL)
            # Python itself maps SIGINT to KeyboardInterrupt; the shell that
            # execs this supervisor may instead have inherited SIGINT ignored
            # (common for asynchronous or test launchers). Restore Python's
            # ordinary interactive Ctrl-C behavior explicitly.
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            os.chdir(cwd)
            os.execvpe(argv[0], list(argv), os.environ)
        except BaseException as exc:
            print(f"egg launcher: cannot execute {argv[0]}: {exc}", file=sys.stderr)
            os._exit(127)
    try:
        os.setpgid(pid, pid)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.ESRCH):
            raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
    return pid


def _wait_generation(
    child_pid: int,
    tty_fd: int | None,
    pending_signals: list[int],
) -> None:
    kill_deadline: float | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal kill_deadline
        pending_signals.append(signum)
        _signal_group(child_pid, signum)
        if signum in _TERMINATING_SIGNALS and kill_deadline is None:
            kill_deadline = time.monotonic() + _GRACE_SECONDS

    previous = {signum: signal.getsignal(signum) for signum in _FORWARDED_SIGNALS}
    for signum in _FORWARDED_SIGNALS:
        signal.signal(signum, forward)

    try:
        while True:
            try:
                status = os.waitid(
                    os.P_PID,
                    child_pid,
                    os.WEXITED | os.WSTOPPED | os.WNOHANG | os.WNOWAIT,
                )
            except InterruptedError:
                status = None
            if status is not None and status.si_pid == child_pid:
                if status.si_code in {os.CLD_STOPPED, os.CLD_TRAPPED}:
                    stop_signal = status.si_status
                    # Consume the stop notification while retaining future exit
                    # status and the leader PID as a cleanup identity fence.
                    os.waitid(os.P_PID, child_pid, os.WSTOPPED | os.WNOHANG)
                    _set_foreground_pgrp(tty_fd, os.getpgrp())
                    previous_stop = signal.getsignal(stop_signal)
                    signal.signal(stop_signal, signal.SIG_DFL)
                    # An orphaned process group discards default job-control
                    # stops. SIGSTOP reliably mirrors the child stop; the shell
                    # or embedding resumes this stable supervisor PID.
                    os.kill(os.getpid(), signal.SIGSTOP)
                    signal.signal(stop_signal, previous_stop)
                    _set_foreground_pgrp(tty_fd, child_pid)
                    _signal_group(child_pid, signal.SIGCONT)
                elif status.si_code in {os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED}:
                    return
            if kill_deadline is not None and time.monotonic() >= kill_deadline:
                _signal_group(child_pid, signal.SIGCONT)
                _signal_group(child_pid, signal.SIGKILL)
                kill_deadline = None
            time.sleep(_POLL_SECONDS)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _read_reload_thread(state_file: Path) -> str:
    try:
        return state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def supervise(
    child_argv: Sequence[str],
    *,
    cwd: str,
    state_file: Path,
    reload_exit_code: int,
    max_reloads: int,
) -> int:
    if not child_argv:
        raise ValueError("missing child argv")
    _require_linux()
    _enable_subreaper()
    own_pgrp = os.getpgrp()
    tty_fd = _tty_fd()
    reload_count = 0
    child_pid: int | None = None
    pending_signals: list[int] = []

    def record_signal(signum: int, _frame: object) -> None:
        pending_signals.append(signum)

    previous = {signum: signal.getsignal(signum) for signum in _FORWARDED_SIGNALS}
    for signum in _FORWARDED_SIGNALS:
        signal.signal(signum, record_signal)

    try:
        while True:
            terminating = next(
                (
                    signum
                    for signum in pending_signals
                    if signum in _TERMINATING_SIGNALS
                ),
                0,
            )
            if terminating:
                return 128 + terminating
            # SIGWINCH/job-control notifications received while no generation
            # exists are stale for the next generation; termination is never
            # discarded.
            pending_signals.clear()
            state_file.write_text("", encoding="utf-8")
            child_pid = _spawn(child_argv, cwd)
            _set_foreground_pgrp(tty_fd, child_pid)
            _wait_generation(child_pid, tty_fd, pending_signals)
            _set_foreground_pgrp(tty_fd, own_pgrp)
            _quiesce_generation(child_pid)
            waited_pid, wait_status = os.waitpid(child_pid, 0)
            if waited_pid != child_pid:
                raise ChildProcessError(f"lost generation leader {child_pid}")
            child_pid = None

            terminating = next(
                (
                    signum
                    for signum in pending_signals
                    if signum in _TERMINATING_SIGNALS
                ),
                0,
            )
            pending_signals.clear()
            if terminating:
                return 128 + terminating
            status = _wait_exit_code(wait_status)
            if status != reload_exit_code:
                return status

            thread_id = _read_reload_thread(state_file)
            if not thread_id:
                print("egg.sh: reload requested without a saved thread id", file=sys.stderr)
                return reload_exit_code
            if reload_count >= max_reloads:
                print(f"egg.sh: reload limit ({max_reloads}) exceeded", file=sys.stderr)
                return reload_exit_code
            os.environ["EGG_RELOAD_THREAD_ID"] = thread_id
            reload_count += 1
    finally:
        _set_foreground_pgrp(tty_fd, own_pgrp)
        if child_pid is not None:
            try:
                _quiesce_generation(child_pid)
            finally:
                try:
                    os.waitpid(child_pid, 0)
                except ChildProcessError:
                    pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--reload-exit-code", required=True, type=int)
    parser.add_argument("--max-reloads", required=True, type=int)
    parser.add_argument("child", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)
    child = args.child[1:] if args.child[:1] == ["--"] else args.child
    return supervise(
        child,
        cwd=args.cwd,
        state_file=args.state_file,
        reload_exit_code=args.reload_exit_code,
        max_reloads=args.max_reloads,
    )


if __name__ == "__main__":
    raise SystemExit(main())
