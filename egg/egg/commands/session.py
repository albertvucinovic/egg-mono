"""Session/REPL command compatibility mixin for the egg application."""
from __future__ import annotations


class SessionCommandsMixin:
    """Compatibility delegates for persistent session and REPL commands."""

    def _dispatch_session_command(self, name: str, arg: str) -> None:
        from eggthreads.command_catalog import CommandContext, create_default_command_registry

        if hasattr(self, "_command_context"):
            context = self._command_context()
        else:
            context = CommandContext(
                db=getattr(self, "db", None),
                current_thread=getattr(self, "current_thread", None),
                log_system=getattr(self, "log_system", None),
                console_print_block=getattr(self, "console_print_block", None),
                start_scheduler=getattr(self, "ensure_scheduler_for", None),
                app=self,
            )
        create_default_command_registry().execute(name, context, arg)

    def cmd_sessionStatus(self, arg: str) -> None:
        self._dispatch_session_command("sessionStatus", arg)

    def cmd_sessionOn(self, arg: str) -> None:
        self._dispatch_session_command("sessionOn", arg)

    def cmd_sessionOff(self, arg: str) -> None:
        self._dispatch_session_command("sessionOff", arg)

    def cmd_sessionStop(self, arg: str) -> None:
        self._dispatch_session_command("sessionStop", arg)

    def cmd_sessionReset(self, arg: str) -> None:
        self._dispatch_session_command("sessionReset", arg)

    def cmd_sessionCleanup(self, arg: str) -> None:
        self._dispatch_session_command("sessionCleanup", arg)

    def cmd_pythonRepl(self, arg: str) -> None:
        self._dispatch_session_command("pythonRepl", arg)

    def cmd_bashRepl(self, arg: str) -> None:
        self._dispatch_session_command("bashRepl", arg)
