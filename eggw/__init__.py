"""Compatibility shim for importing eggw from the monorepo root."""

from pathlib import Path

_INNER_PACKAGE_DIR = Path(__file__).resolve().parent / "eggw"
if _INNER_PACKAGE_DIR.is_dir():
    _inner = str(_INNER_PACKAGE_DIR)
    if _inner not in __path__:
        __path__.append(_inner)

from .eggw import *  # noqa: F401,F403