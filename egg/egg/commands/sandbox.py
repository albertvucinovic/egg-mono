"""Sandbox command compatibility mixin for the egg application."""
from __future__ import annotations


class SandboxCommandsMixin:
    """Compatibility delegates for sandbox management commands."""

    def _dispatch_sandbox_command(self, name: str, arg: str) -> None:
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

    def cmd_toggleSandboxing(self, arg: str) -> None:
        self._dispatch_sandbox_command("toggleSandboxing", arg)

    def cmd_setSandboxConfiguration(self, arg: str) -> None:
        self._dispatch_sandbox_command("setSandboxConfiguration", arg)

    def cmd_getSandboxingConfig(self, arg: str) -> None:
        self._dispatch_sandbox_command("getSandboxingConfig", arg)
