"""Compatibility shim for importing eggopt from the monorepo root."""

from importlib import import_module
from pathlib import Path

_INNER_PACKAGE_DIR = Path(__file__).resolve().parent / "eggopt"
if _INNER_PACKAGE_DIR.is_dir():
    _inner = str(_INNER_PACKAGE_DIR)
    if _inner not in __path__:
        __path__.append(_inner)

_pkg = import_module(".eggopt", __name__)
_exports = list(getattr(_pkg, "__all__", ()))

for _name in _exports:
    globals()[_name] = getattr(_pkg, _name)

__all__ = _exports


def __getattr__(name: str):
    return getattr(_pkg, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_pkg)))
