"""Compatibility shim for importing eggconfig from the monorepo root.

When the repository root itself is on ``sys.path``, ``import eggconfig`` would
otherwise resolve to the outer ``eggconfig/`` directory as a namespace package,
which does not expose ``get_models_path`` / ``get_all_models_path``. Re-export
the real package API here so both editable installs and direct monorepo imports
behave the same way.
"""

from .eggconfig import get_all_models_path, get_models_path

__all__ = ["get_models_path", "get_all_models_path"]