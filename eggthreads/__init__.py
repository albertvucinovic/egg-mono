"""Compatibility shim for importing eggthreads from the monorepo root.

When the repository root is on ``sys.path``, ``import eggthreads`` can resolve
to the outer ``eggthreads/`` directory instead of the actual package living in
``eggthreads/eggthreads``. Re-export the real package API here and extend the
package search path so imports like ``eggthreads.api`` keep working.
"""

from importlib import import_module
from pathlib import Path
import sys

_INNER_PACKAGE_DIR = Path(__file__).resolve().parent / "eggthreads"
if _INNER_PACKAGE_DIR.is_dir():
    _inner = str(_INNER_PACKAGE_DIR)
    if _inner not in __path__:
        __path__.append(_inner)

_pkg = import_module('.eggthreads', __name__)


def _alias_inner_submodules() -> None:
    """Expose already-imported inner submodules under outer package names.

    Without this, importing ``eggthreads`` first loads modules such as
    ``eggthreads.eggthreads.api`` via the inner package, but a later
    ``import eggthreads.api`` loads the same file again under a second module
    name. That splits monkeypatching/state across two module objects and can
    make tests hang indefinitely.
    """

    inner_prefix = f"{__name__}.eggthreads."
    outer_prefix = f"{__name__}."
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith(inner_prefix):
            continue
        outer_name = outer_prefix + mod_name[len(inner_prefix):]
        sys.modules.setdefault(outer_name, mod)


_alias_inner_submodules()

_exports = list(getattr(_pkg, '__all__', [])) or [
    _name for _name in dir(_pkg) if not _name.startswith('_')
]

for _name in _exports:
    globals()[_name] = getattr(_pkg, _name)

__all__ = _exports


def __getattr__(name: str):
    return getattr(_pkg, name)


def __dir__():
    return sorted(set(globals().keys()) | set(dir(_pkg)))