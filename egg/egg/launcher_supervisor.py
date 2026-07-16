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
    def _process_state(pid: int) -> str | None:
        try:
            fields = (
                Path(f"/proc/{pid}/stat")
                .read_text(encoding="ascii")
                .rsplit(") ", 1)[1]
                .split()
            )
        except FileNotFoundError:
            return None
        except (IndexError, OSError) as exc:
            raise RuntimeError(f"cannot inspect Linux process {pid}") from exc
        return fields[0]

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

    def _linux_remaining(
        self, leader_pid: int, *, leader_exited: bool
    ) -> tuple[list[int], bool, bool]:
        self._reap_adopted(leader_pid)
        adopted = [pid for pid in self._direct_children() if pid != leader_pid]
        leader_state = self._process_state(leader_pid)
        leader_alive = not leader_exited and leader_state not in (None, "Z")
        return adopted, self._group_has_live_member(leader_pid), leader_alive

    def quiesce(self, leader_pid: int, *, leader_exited: bool) -> None:
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
            adopted, group_alive, leader_alive = self._linux_remaining(
                leader_pid, leader_exited=leader_exited
            )
            if not adopted and not group_alive and not leader_alive:
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
            if not leader_exited:
                _signal_pid(leader_pid, signal.SIGCONT)
                _signal_pid(leader_pid, signal.SIGKILL)
            adopted, group_alive, leader_alive = self._linux_remaining(
                leader_pid, leader_exited=leader_exited
            )
            if not adopted and not group_alive and not leader_alive:
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


def _kill_and_reap_leader(leader_pid: int) -> bool:
    """Fence one generation group with KILL, then boundedly reap its leader."""

    # The unreaped direct-child leader is the PID/PGID identity fence. Always
    # kill the entire fenced group before waitpid can destroy that identity,
    # even when the leader is already waitable after an earlier TERM.
    _signal_group(leader_pid, signal.SIGCONT)
    _signal_group(leader_pid, signal.SIGKILL)
    _signal_pid(leader_pid, signal.SIGCONT)
    _signal_pid(leader_pid, signal.SIGKILL)

    deadline = time.monotonic() + max(_GRACE_SECONDS, 0.5)
    while True:
        try:
            waited_pid, _status = os.waitpid(leader_pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited_pid == leader_pid:
            # Never signal leader_pid/PGID after this point: it can now be reused.
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


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


class _SignalRelay:
    """One latched termination/deadline authority across the whole lifecycle."""

    def __init__(self) -> None:
        self.child_pid: int | None = None
        self.termination_signal = 0
        self.kill_deadline: float | None = None
        self.previous = {
            signum: signal.getsignal(signum) for signum in _FORWARDED_SIGNALS
        }

    def install(self) -> None:
        for signum in _FORWARDED_SIGNALS:
            signal.signal(signum, self.handle)

    def restore(self) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def finalize_decision(self) -> int:
        """Linearize the final termination decision and restore signal state."""

        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, _TERMINATING_SIGNALS
        )
        try:
            # Briefly restore the caller mask while this relay remains installed.
            # Any signal pending before this lifecycle's final linearization point
            # is therefore delivered here and latched, never by the old handler.
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            signal.pthread_sigmask(signal.SIG_BLOCK, _TERMINATING_SIGNALS)
            decision = self.termination_signal
            self.restore()
            return decision
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def handle(self, signum: int, _frame: object) -> None:
        if signum in _TERMINATING_SIGNALS and not self.termination_signal:
            self.termination_signal = signum
            self.kill_deadline = time.monotonic() + _GRACE_SECONDS
        if self.child_pid is not None:
            _signal_group(self.child_pid, signum)

    def terminating(self) -> int:
        return self.termination_signal

    def attach_child(self, child_pid: int) -> None:
        self.child_pid = child_pid
        # A termination can be latched before a child is published. Never erase
        # it: immediately apply it to any generation that appears afterward.
        if self.termination_signal:
            _signal_group(child_pid, self.termination_signal)

    def detach_child(self) -> None:
        self.child_pid = None

    def escalate_if_due(self) -> None:
        if (
            self.child_pid is not None
            and self.kill_deadline is not None
            and time.monotonic() >= self.kill_deadline
        ):
            _signal_group(self.child_pid, signal.SIGCONT)
            _signal_group(self.child_pid, signal.SIGKILL)
            self.kill_deadline = None


def _wait_generation(
    child_pid: int,
    *,
    tty_fd: int | None,
    owns_foreground: bool,
    relay: _SignalRelay,
) -> bool:
    """Wait for a generation under the already-installed signal relay."""

    if relay.child_pid != child_pid:
        raise RuntimeError("signal relay does not own generation")
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
        relay.escalate_if_due()
        time.sleep(_POLL_SECONDS)


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
    owner: _ProcessOwner | None = None
    own_pgrp = os.getpgrp()
    tty_fd = _controlling_tty_fd()
    # Respect the invoking shell. Only a foreground job may transfer terminal
    # ownership; a shell-background `egg.sh &` must not steal it.
    owns_foreground = tty_fd is not None and _foreground_pgrp(tty_fd) == own_pgrp
    reload_count = 0
    child_pid: int | None = None
    relay = _SignalRelay()
    result: int | None = None
    final_termination = 0

    try:
        relay.install()
        # Initialization belongs inside state-file finalization. Linux procfs or
        # prctl failure therefore restores handlers and unlinks shell state.
        owner = _ProcessOwner()
        while result is None:
            terminating = relay.terminating()
            if terminating:
                result = 128 + terminating
                break
            state_file.write_text("", encoding="utf-8")
            child_pid = _spawn(child_argv, cwd)
            relay.attach_child(child_pid)
            if owns_foreground:
                _set_foreground_pgrp(tty_fd, child_pid)
            owns_foreground = _wait_generation(
                child_pid,
                tty_fd=tty_fd,
                owns_foreground=owns_foreground,
                relay=relay,
            )
            if owns_foreground:
                _set_foreground_pgrp(tty_fd, own_pgrp)
            if owner is None:
                raise RuntimeError("process owner was not initialized")
            owner.quiesce(child_pid, leader_exited=True)
            waited_pid, wait_status = os.waitpid(child_pid, 0)
            if waited_pid != child_pid:
                raise ChildProcessError(f"lost generation leader {child_pid}")
            child_pid = None
            relay.detach_child()

            terminating = relay.terminating()
            if terminating:
                result = 128 + terminating
                break
            status = _wait_exit_code(wait_status)
            if status != reload_exit_code:
                result = status
                break

            thread_id = _read_reload_thread(state_file)
            if not thread_id:
                print("egg.sh: reload requested without a saved thread id", file=sys.stderr)
                result = reload_exit_code
                break
            if reload_count >= max_reloads:
                print(f"egg.sh: reload limit ({max_reloads}) exceeded", file=sys.stderr)
                result = reload_exit_code
                break
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
            if owner is not None:
                try:
                    owner.quiesce(child_pid, leader_exited=False)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            try:
                reaped = _kill_and_reap_leader(child_pid)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                reaped = False
            if not reaped and cleanup_error is None:
                cleanup_error = RuntimeError(
                    f"could not reap generation leader {child_pid} after SIGKILL"
                )
        relay.detach_child()
        try:
            state_file.unlink()
        except FileNotFoundError:
            pass
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        final_termination = relay.finalize_decision()
        if cleanup_error is not None:
            active_error = sys.exc_info()[1]
            if active_error is None:
                raise cleanup_error
            active_error.add_note(f"launcher cleanup also failed: {cleanup_error!r}")

    if final_termination:
        return 128 + final_termination
    if result is None:
        raise RuntimeError("launcher supervision ended without a status")
    return result


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
