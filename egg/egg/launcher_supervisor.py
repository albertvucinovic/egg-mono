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


class _ProcessOwner:
    """Generation cleanup, strong on Linux and process-group best effort elsewhere."""

    def __init__(self) -> None:
        self.strong = sys.platform.startswith("linux")
        if not self.strong:
            return
        self._verify_procfs()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, f"cannot enable launcher subreaper: {os.strerror(error)}")

    @staticmethod
    def _verify_procfs() -> None:
        children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
        try:
            children.read_text(encoding="ascii")
            Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
        except OSError as exc:
            raise RuntimeError("Linux launcher cleanup requires readable /proc") from exc

    @staticmethod
    def _direct_children() -> list[int]:
        path = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
        try:
            values = Path(path).read_text(encoding="ascii").split()
        except OSError as exc:
            raise RuntimeError("lost required Linux /proc child ownership") from exc
        try:
            return [int(value) for value in values]
        except ValueError as exc:
            raise RuntimeError("invalid Linux /proc child ownership data") from exc

    @staticmethod
    def _group_has_live_member(pgid: int) -> bool:
        try:
            process_paths = list(Path("/proc").iterdir())
        except OSError as exc:
            raise RuntimeError("lost required Linux /proc process inventory") from exc
        for path in process_paths:
            if not path.name.isdigit() or path.name == str(pgid):
                continue
            try:
                fields = (
                    (path / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
                )
                state = fields[0]
                process_group = int(fields[2])
            except FileNotFoundError:
                continue
            except (IndexError, OSError, ValueError) as exc:
                raise RuntimeError(f"cannot inspect Linux process {path.name}") from exc
            if process_group == pgid and state != "Z":
                return True
        return False

    def _reap_adopted(self, leader_pid: int) -> None:
        if not self.strong:
            return
        for pid in self._direct_children():
            if pid == leader_pid:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue

    def _linux_remaining(self, leader_pid: int) -> tuple[list[int], bool]:
        self._reap_adopted(leader_pid)
        adopted = [pid for pid in self._direct_children() if pid != leader_pid]
        return adopted, self._group_has_live_member(leader_pid)

    def quiesce(self, leader_pid: int) -> None:
        """Boundedly terminate one generation and every observable descendant."""

        _signal_group(leader_pid, signal.SIGCONT)
        _signal_group(leader_pid, signal.SIGTERM)
        if not self.strong:
            # Portable contract: members that remain in the generation's process
            # group are terminated; descendants that create another session are
            # outside the launcher authority on platforms without subreapers.
            time.sleep(_GRACE_SECONDS)
            _signal_group(leader_pid, signal.SIGKILL)
            return

        deadline = time.monotonic() + _GRACE_SECONDS
        while time.monotonic() < deadline:
            adopted, group_alive = self._linux_remaining(leader_pid)
            if not adopted and not group_alive:
                return
            for pid in adopted:
                _signal_pid(pid, signal.SIGCONT)
                _signal_pid(pid, signal.SIGTERM)
            time.sleep(_POLL_SECONDS)

        # Adoption can happen one link at a time. Re-read and signal every newly
        # adopted PID on every pass until the ownership tree converges or the
        # bounded hard-cleanup deadline expires.
        deadline = time.monotonic() + max(_GRACE_SECONDS * 4, 2.0)
        while time.monotonic() < deadline:
            _signal_group(leader_pid, signal.SIGCONT)
            _signal_group(leader_pid, signal.SIGKILL)
            adopted, group_alive = self._linux_remaining(leader_pid)
            if not adopted and not group_alive:
                return
            for pid in adopted:
                _signal_pid(pid, signal.SIGCONT)
                _signal_pid(pid, signal.SIGKILL)
            time.sleep(_POLL_SECONDS)
        raise RuntimeError(f"generation process group {leader_pid} did not terminate")


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


def _wait_exit_code(wait_status: int) -> int:
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status)
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return 1


def _controlling_tty_fd() -> int | None:
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                os.tcgetpgrp(fd)
                return fd
        except OSError:
            continue
    return None


def _foreground_pgrp(fd: int | None) -> int | None:
    if fd is None:
        return None
    try:
        return os.tcgetpgrp(fd)
    except OSError as exc:
        if exc.errno == errno.ENOTTY:
            return None
        raise


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
    pid = os.fork()
    if pid == 0:
        try:
            os.setpgid(0, 0)
            for signum in _FORWARDED_SIGNALS:
                signal.signal(signum, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)
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
    return pid


def _wait_generation(
    child_pid: int,
    *,
    tty_fd: int | None,
    owns_foreground: bool,
    pending_signals: list[int],
) -> bool:
    """Wait for a generation and return current supervisor foreground authority."""

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
                    os.waitid(os.P_PID, child_pid, os.WSTOPPED | os.WNOHANG)
                    if owns_foreground:
                        _set_foreground_pgrp(tty_fd, os.getpgrp())
                    # This also handles an uncatchable child SIGSTOP: unlike the
                    # rejected implementation, no handler is installed for it.
                    os.kill(os.getpid(), signal.SIGSTOP)
                    # A shell `bg` retains terminal authority; `fg` assigns the
                    # wrapper job's group before continuing it.
                    owns_foreground = (
                        tty_fd is not None
                        and _foreground_pgrp(tty_fd) == os.getpgrp()
                    )
                    if owns_foreground:
                        _set_foreground_pgrp(tty_fd, child_pid)
                    # The generation is a separate process group, so explicitly
                    # continue it. A background terminal read then receives
                    # SIGTTIN and is surfaced as another stopped job.
                    _signal_group(child_pid, signal.SIGCONT)
                elif status.si_code in {os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED}:
                    return owns_foreground
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


def _termination_signal(pending_signals: Sequence[int]) -> int:
    return next(
        (signum for signum in pending_signals if signum in _TERMINATING_SIGNALS),
        0,
    )


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
    owner = _ProcessOwner()
    own_pgrp = os.getpgrp()
    tty_fd = _controlling_tty_fd()
    # Respect the invoking shell. Only a foreground job may transfer terminal
    # ownership; a shell-background `egg.sh &` must not steal it.
    owns_foreground = tty_fd is not None and _foreground_pgrp(tty_fd) == own_pgrp
    reload_count = 0
    child_pid: int | None = None
    pending_signals: list[int] = []

    def forward_or_record(signum: int, _frame: object) -> None:
        pending_signals.append(signum)
        if child_pid is not None:
            _signal_group(child_pid, signum)

    previous = {signum: signal.getsignal(signum) for signum in _FORWARDED_SIGNALS}
    for signum in _FORWARDED_SIGNALS:
        signal.signal(signum, forward_or_record)

    try:
        while True:
            terminating = _termination_signal(pending_signals)
            if terminating:
                return 128 + terminating
            pending_signals.clear()
            state_file.write_text("", encoding="utf-8")
            child_pid = _spawn(child_argv, cwd)
            if owns_foreground:
                _set_foreground_pgrp(tty_fd, child_pid)
            terminating = _termination_signal(pending_signals)
            if terminating:
                # A signal may arrive after fork but before the generation wait
                # installs its deadline-owning handler. Forward it now; cleanup
                # below provides bounded TERM-to-KILL escalation.
                _signal_group(child_pid, terminating)
                return 128 + terminating
            # The outer forwarding handler owns the complete spawn/handoff gap;
            # this call replaces it only after child_pid is published.
            owns_foreground = _wait_generation(
                child_pid,
                tty_fd=tty_fd,
                owns_foreground=owns_foreground,
                pending_signals=pending_signals,
            )
            if owns_foreground:
                _set_foreground_pgrp(tty_fd, own_pgrp)
            try:
                owner.quiesce(child_pid)
            except BaseException:
                # Mark this generation as already cleanup-attempted; the outer
                # finalizer still restores handlers and unlinks state without
                # recursively invoking a second failing quiescence pass.
                failed_pid = child_pid
                child_pid = None
                try:
                    os.waitpid(failed_pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                raise
            waited_pid, wait_status = os.waitpid(child_pid, 0)
            if waited_pid != child_pid:
                raise ChildProcessError(f"lost generation leader {child_pid}")
            child_pid = None

            terminating = _termination_signal(pending_signals)
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
        cleanup_error: BaseException | None = None
        if owns_foreground:
            try:
                _set_foreground_pgrp(tty_fd, own_pgrp)
            except BaseException as exc:
                cleanup_error = exc
        if child_pid is not None:
            try:
                owner.quiesce(child_pid)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
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
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


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
