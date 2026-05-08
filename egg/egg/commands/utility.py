"""Utility command mixins for the egg application."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from ..utils import COMMANDS_TEXT, read_clipboard


def _find_searxng_dir() -> Optional[Path]:
    from eggthreads.builtin_plugins.web import find_searxng_dir

    return find_searxng_dir()


def _resolve_compose_cmd() -> Optional[List[str]]:
    from eggthreads.builtin_plugins.web import resolve_compose_cmd

    return resolve_compose_cmd()


class UtilityCommandsMixin:
    """Mixin providing utility commands: /help, /skills, /skill, /cost, /paste, /quit, /enterMode, /startSearxng."""

    def cmd_help(self, arg: str) -> None:
        """Handle /help command - show available commands."""
        # Mirror /threads behaviour: show the full help text in the
        # console (above the live panels) and keep the System panel
        # message short.
        try:
            from eggthreads.command_catalog import CommandContext, _core_help_handler

            _core_help_handler(
                CommandContext(
                    log_system=self.log_system,
                    console_print_block=self.console_print_block,
                    app=self,
                ),
                arg,
            )
        except Exception:
            # Fallback: at least log it.
            self.log_system(COMMANDS_TEXT)

    def _dispatch_utility_command(self, name: str, arg: str) -> None:
        from eggthreads.command_catalog import CommandContext, create_default_command_registry

        if hasattr(self, "_command_context"):
            context = self._command_context()
        else:
            context = CommandContext(
                db=getattr(self, "db", None),
                current_thread=getattr(self, "current_thread", None),
                log_system=getattr(self, "log_system", None),
                console_print_block=getattr(self, "console_print_block", None),
                app=self,
            )
        create_default_command_registry().execute(name, context, arg)

    def cmd_skills(self, arg: str) -> None:
        """List or search packaged skill documents."""
        self._dispatch_utility_command("skills", arg)

    def cmd_skill(self, arg: str) -> None:
        """Show a packaged skill document by name."""
        self._dispatch_utility_command("skill", arg)

    def cmd_quit(self, arg: str) -> None:
        """Handle /quit command - exit the application."""
        try:
            from eggthreads.command_catalog import CommandContext, _core_quit_handler

            _core_quit_handler(CommandContext(app=self), arg)
        except Exception:
            self.running = False

    def cmd_reload(self, arg: str) -> None:
        """Handle /reload command - restart egg.sh and reopen current thread."""
        from eggthreads.command_catalog import CommandContext, _core_reload_handler

        _core_reload_handler(
            CommandContext(
                current_thread=getattr(self, 'current_thread', ''),
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def _run_searxng_compose(
        self,
        compose_args: List[str],
        *,
        action: str,
        starting_msg: str,
        success_summary: str,
        timeout_sec: int = 600,
    ) -> None:
        """Shared helper for /startSearxng and /stopSearxng.

        Runs ``<compose> <compose_args...>`` in the ``searxng/`` directory
        on a background thread, surfacing output both as a concise System
        log line and a full console block (like /help).
        """
        searxng_dir = _find_searxng_dir()
        if searxng_dir is None:
            msg = (
                "could not locate searxng/docker-compose.yml "
                "(expected a 'searxng/' dir near the egg-mono root)."
            )
            self.log_system(f"SearXNG {action}: {msg}")
            try:
                self.console_print_block(
                    f"SearXNG {action}", msg, border_style="red"
                )
            except Exception:
                pass
            return

        compose = _resolve_compose_cmd()
        if compose is None:
            msg = (
                "neither 'docker compose' nor 'docker-compose' is on PATH. "
                "Install Docker Engine + compose, then re-run the command."
            )
            self.log_system(f"SearXNG {action}: {msg}")
            try:
                self.console_print_block(
                    f"SearXNG {action}", msg, border_style="red"
                )
            except Exception:
                pass
            return

        argv = compose + compose_args
        self.log_system(
            f"SearXNG {action}: {starting_msg} (running `{' '.join(argv)}` "
            f"in {searxng_dir}; see console for output)"
        )

        def _runner() -> None:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(searxng_dir),
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                try:
                    self.log_system(
                        f"SearXNG {action}: timed out after {timeout_sec}s."
                    )
                    self.console_print_block(
                        f"SearXNG {action}",
                        f"Timed out after {timeout_sec}s running `{' '.join(argv)}`.",
                        border_style="red",
                    )
                except Exception:
                    pass
                return
            except Exception as e:
                try:
                    self.log_system(f"SearXNG {action}: failed to launch: {e}")
                    self.console_print_block(
                        f"SearXNG {action}",
                        f"Failed to launch `{' '.join(argv)}`:\n{e}",
                        border_style="red",
                    )
                except Exception:
                    pass
                return

            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            combined_parts: List[str] = []
            if stdout:
                combined_parts.append(stdout)
            if stderr:
                combined_parts.append(stderr)
            combined = "\n".join(combined_parts) or "(no output)"
            # Cap very long output so the console block stays readable.
            if len(combined) > 4000:
                combined = combined[:4000] + "\n... (truncated)"

            if proc.returncode == 0:
                try:
                    self.log_system(f"SearXNG {action}: done. {success_summary}")
                    self.console_print_block(
                        f"SearXNG {action}",
                        f"{success_summary}\n\n$ {' '.join(argv)}\n{combined}",
                        border_style="green",
                    )
                except Exception:
                    pass
            else:
                try:
                    self.log_system(
                        f"SearXNG {action}: failed (exit {proc.returncode}). See console."
                    )
                    self.console_print_block(
                        f"SearXNG {action}",
                        f"Exit code: {proc.returncode}\n$ {' '.join(argv)}\n{combined}",
                        border_style="red",
                    )
                except Exception:
                    pass

        threading.Thread(target=_runner, name=f"searxng-{action}", daemon=True).start()

    def cmd_startSearxng(self, arg: str) -> None:
        """Handle /startSearxng - start the SearXNG docker service in the background."""
        from eggthreads.builtin_plugins import web as web_plugin

        original_find = web_plugin.find_searxng_dir
        original_resolve = web_plugin.resolve_compose_cmd
        original_run = web_plugin.subprocess.run
        try:
            web_plugin.find_searxng_dir = _find_searxng_dir
            web_plugin.resolve_compose_cmd = _resolve_compose_cmd
            web_plugin.subprocess.run = subprocess.run
            self._dispatch_utility_command("startSearxng", arg)
        finally:
            web_plugin.find_searxng_dir = original_find
            web_plugin.resolve_compose_cmd = original_resolve
            web_plugin.subprocess.run = original_run

    def cmd_stopSearxng(self, arg: str) -> None:
        """Handle /stopSearxng - stop the SearXNG docker service."""
        from eggthreads.builtin_plugins import web as web_plugin

        original_find = web_plugin.find_searxng_dir
        original_resolve = web_plugin.resolve_compose_cmd
        original_run = web_plugin.subprocess.run
        try:
            web_plugin.find_searxng_dir = _find_searxng_dir
            web_plugin.resolve_compose_cmd = _resolve_compose_cmd
            web_plugin.subprocess.run = subprocess.run
            self._dispatch_utility_command("stopSearxng", arg)
        finally:
            web_plugin.find_searxng_dir = original_find
            web_plugin.resolve_compose_cmd = original_resolve
            web_plugin.subprocess.run = original_run
