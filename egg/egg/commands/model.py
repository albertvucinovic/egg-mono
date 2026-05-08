"""Model command compatibility mixin for the egg application."""
from __future__ import annotations


class ModelCommandsMixin:
    """Model command group has migrated to CommandRegistry plugins."""

    def _dispatch_model_command(self, name: str, arg: str) -> None:
        from eggthreads.command_catalog import create_default_command_registry

        create_default_command_registry().execute(name, self._command_context(), arg)
