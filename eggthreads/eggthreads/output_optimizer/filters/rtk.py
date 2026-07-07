from __future__ import annotations

"""Optional RTK-backed output optimizer adapter.

This filter is intentionally presentation-layer only: it receives already
captured output, pipes that text to ``rtk pipe``, and returns a shorter preview
only when the subprocess succeeds.  Missing RTK, failures, timeouts, or larger
outputs all fall back through the normal optimizer never-worse guards.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from ..config import (
    DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND,
    DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS,
)
from ..core import OptimizeDecision, OptimizeRequest, make_decision
from ..generic import clean_ansi_controls


@dataclass(frozen=True)
class RtkPipeFilter:
    """Run optional ``rtk pipe`` over captured output when explicitly enabled."""

    name: str = "rtk_pipe"
    command: str = DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND
    timeout_seconds: float = DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS
    privacy_opt_in: bool = False
    confidence: float = 0.75

    def optimize(self, request: OptimizeRequest) -> OptimizeDecision | None:
        if not isinstance(request.output, str) or not request.output.strip():
            return None

        argv = self._argv()
        if not argv:
            return None

        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError):
            timeout = DEFAULT_OUTPUT_OPTIMIZER_RTK_TIMEOUT_SECONDS
        timeout = max(0.1, timeout)

        env = self._privacy_safe_env()
        try:
            with tempfile.TemporaryDirectory(prefix="egg-rtk-") as tmpdir:
                if not self.privacy_opt_in:
                    self._apply_isolated_tracking_env(env, tmpdir)
                proc = subprocess.run(
                    [*argv, "pipe"],
                    input=request.output,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                    cwd=tmpdir,
                    check=False,
                )
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
            return None

        if proc.returncode != 0:
            return None

        optimized = clean_ansi_controls(proc.stdout if isinstance(proc.stdout, str) else "")
        if not optimized.strip():
            return None
        if optimized == request.output:
            return None

        return make_decision(
            request,
            optimized,
            filter_name=self.name,
            reason="rtk_pipe_adapter",
            confidence=self.confidence,
            metadata={
                "rtk_command": argv[0],
                "rtk_timeout_seconds": timeout,
                "rtk_privacy_opt_in": bool(self.privacy_opt_in),
                "rtk_telemetry_disabled": not bool(self.privacy_opt_in),
                "rtk_stderr_chars": len(proc.stderr or ""),
            },
        )

    def _argv(self) -> list[str]:
        command = str(self.command or DEFAULT_OUTPUT_OPTIMIZER_RTK_COMMAND).strip()
        if not command:
            return []
        try:
            return shlex.split(command, comments=False, posix=os.name != "nt")
        except ValueError:
            return []

    def _privacy_safe_env(self) -> dict[str, str]:
        env = {str(key): str(value) for key, value in os.environ.items()}
        if not self.privacy_opt_in:
            # RTK's documented privacy switch.  Override any inherited value so
            # enabling the adapter cannot accidentally enable telemetry.
            env["RTK_TELEMETRY_DISABLED"] = "1"
            env.setdefault("DO_NOT_TRACK", "1")
        return env

    @staticmethod
    def _apply_isolated_tracking_env(env: dict[str, str], tmpdir: str) -> None:
        root = Path(tmpdir)
        home = root / "home"
        rtk_home = root / "rtk-home"
        xdg_config = root / "config"
        xdg_state = root / "state"
        xdg_cache = root / "cache"
        for path in (home, rtk_home, xdg_config, xdg_state, xdg_cache):
            path.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["RTK_HOME"] = str(rtk_home)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        env["XDG_STATE_HOME"] = str(xdg_state)
        env["XDG_CACHE_HOME"] = str(xdg_cache)


__all__ = ["RtkPipeFilter"]
